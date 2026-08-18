#!/usr/bin/env bash
# start_system.sh — build and start the pumpd stack (Docker Compose)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUMPD_DIR="${PUMPD_DIR:-${SCRIPT_DIR}/pumpd}"
COMPOSE_FILE="${COMPOSE_FILE:-${PUMPD_DIR}/docker-compose.yml}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
BUILD="${BUILD:-1}"

log() { printf '[start_system] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

PUMPD_PORT="${PUMPD_PORT:-8080}"
ORPHAN_HELPER="${SCRIPT_DIR}/pumpd_port_orphans.sh"

port_in_use() {
  ss -tln 2>/dev/null | grep -q ":${PUMPD_PORT} "
}

orphan_pumpd_pids() {
  local pids=""
  if pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port ${PUMPD_PORT}" >/dev/null 2>&1; then
    pids=$(pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port ${PUMPD_PORT}" 2>/dev/null || true)
  fi
  if pgrep -f "docker-proxy.*host-port ${PUMPD_PORT}" >/dev/null 2>&1; then
    pids=$(printf '%s\n%s' "${pids}" "$(pgrep -f "docker-proxy.*host-port ${PUMPD_PORT}" 2>/dev/null || true)")
  fi
  printf '%s\n' "${pids}" | sed '/^$/d' | sort -u
}

pumpd_pids_on_port() {
  local pids=""
  if command -v ss >/dev/null 2>&1; then
    pids=$(ss -tlnp 2>/dev/null | grep ":${PUMPD_PORT} " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)
  fi
  if [[ -z "${pids}" ]] && command -v lsof >/dev/null 2>&1; then
    pids=$(lsof -tiTCP:"${PUMPD_PORT}" -sTCP:LISTEN 2>/dev/null | sort -u || true)
  fi
  pids=$(printf '%s\n%s' "${pids}" "$(orphan_pumpd_pids)" | sed '/^$/d' | sort -u)
  printf '%s\n' "${pids}"
}

orphan_kill_hint() {
  local pids=("$@")
  local cid=""
  cid=$(ps aux 2>/dev/null | grep 'containerd-shim-runc-v2.*-id' | grep -oE '\-id [a-f0-9]{64}' | head -1 | awk '{print $2}' || true)
  if [[ -n "${cid}" ]]; then
    die "Port ${PUMPD_PORT} blocked by a ghost Docker container (${cid:0:12}). Killing PIDs respawns them. Run: docker rm -f ${cid:0:12}  ||  sudo snap restart docker"
  fi
  ((${#pids[@]})) || return 0
  die "Port ${PUMPD_PORT} blocked by orphaned pumpd (PIDs: ${pids[*]}). Run: sudo kill -9 ${pids[*]}"
}

free_pumpd_port() {
  log "Ensuring port ${PUMPD_PORT} is free"
  "${COMPOSE[@]}" down --remove-orphans 2>/dev/null || true

  local ids
  ids=$(docker ps -q --filter "publish=${PUMPD_PORT}" 2>/dev/null || true)
  if [[ -n "${ids}" ]]; then
    log "Stopping other containers using port ${PUMPD_PORT}: ${ids}"
    docker stop ${ids} 2>/dev/null || true
  fi

  if ! port_in_use; then
    return 0
  fi

  mapfile -t orphan_pids < <(orphan_pumpd_pids)
  local pid cmd need_root=()
  while IFS= read -r pid; do
    [[ -n "${pid}" ]] || continue
    cmd=$(ps -p "${pid}" -o args= 2>/dev/null || true)
    if [[ "${cmd}" == *"uvicorn app.main:app"* ]] || [[ "${cmd}" == *"docker-proxy"* && "${cmd}" == *"${PUMPD_PORT}"* ]]; then
      if kill "${pid}" 2>/dev/null; then
        log "Stopped leftover pumpd-related process ${pid}"
        sleep 1
      else
        need_root+=("${pid}")
      fi
    fi
  done < <(pumpd_pids_on_port)

  if ! port_in_use; then
    return 0
  fi

  mapfile -t orphan_pids < <(orphan_pumpd_pids)
  if ((${#orphan_pids[@]} > 0)); then
    orphan_kill_hint "${orphan_pids[@]}"
  fi

  if ((${#need_root[@]} > 0)); then
    orphan_kill_hint "${need_root[@]}"
  fi

  die "Port ${PUMPD_PORT} is already in use. Run: sudo lsof -i:${PUMPD_PORT}"
}

command -v docker >/dev/null 2>&1 || die "docker is not installed or not in PATH"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose -f "${COMPOSE_FILE}")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose -f "${COMPOSE_FILE}")
else
  die "docker compose is not available (install Docker Compose v2 or docker-compose)"
fi

[[ -d "${PUMPD_DIR}" ]] || die "pumpd directory not found: ${PUMPD_DIR}"
[[ -f "${COMPOSE_FILE}" ]] || die "compose file not found: ${COMPOSE_FILE}"

ensure_docker_daemon() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  die "Docker daemon is not running (no /var/run/docker.sock). Start it with: sudo snap start docker"
}

ensure_docker_daemon

cd "${PUMPD_DIR}"

if [[ ! -f config.yaml ]]; then
  if [[ -f config.example.yaml ]]; then
    log "config.yaml missing — copying from config.example.yaml"
    cp config.example.yaml config.yaml
    log "Edit ${PUMPD_DIR}/config.yaml before relying on automatic pump control"
  else
    die "config.yaml not found and no config.example.yaml to copy"
  fi
fi

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    log ".env missing — copying from .env.example"
    cp .env.example .env
    log "Edit ${PUMPD_DIR}/.env with SMARTTHINGS_PAT and Tuya keys"
  else
    log "warning: .env not found; secrets must be set in config.yaml"
  fi
fi

mkdir -p data credentials

free_pumpd_port

log "Starting pumpd stack from ${PUMPD_DIR}"
if [[ "${BUILD}" == "1" ]]; then
  "${COMPOSE[@]}" up -d --build --remove-orphans
else
  "${COMPOSE[@]}" up -d --remove-orphans
fi

log "Waiting for health check at ${HEALTH_URL} (timeout ${HEALTH_TIMEOUT}s)"
deadline=$((SECONDS + HEALTH_TIMEOUT))
while (( SECONDS < deadline )); do
  if response="$(curl -sf "${HEALTH_URL}" 2>/dev/null)"; then
    log "Service is responding"
    if command -v jq >/dev/null 2>&1; then
      echo "${response}" | jq .
    else
      echo "${response}"
    fi
    log "Dashboard: http://localhost:${PUMPD_PORT}/user"
    log "Admin:     http://localhost:${PUMPD_PORT}/admin"
    log "Health:    ${HEALTH_URL}"
    log "Logs:      cd ${PUMPD_DIR} && ${COMPOSE[*]} logs -f"
    exit 0
  fi
  sleep 2
done

log "warning: health check did not succeed within ${HEALTH_TIMEOUT}s"
log "Container may still be starting — check logs:"
log "  cd ${PUMPD_DIR} && ${COMPOSE[*]} logs -f"
"${COMPOSE[@]}" ps
exit 1
