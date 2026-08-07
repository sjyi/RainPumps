#!/usr/bin/env bash
# start_system.sh — build and start the pumpd stack (Docker Compose)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUMPD_DIR="${PUMPD_DIR:-${SCRIPT_DIR}/pumpd}"
COMPOSE_FILE="${COMPOSE_FILE:-${PUMPD_DIR}/docker-compose.yml}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
BUILD="${BUILD:-1}"

log() { printf '[start_system] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

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
    log "Dashboard: http://localhost:8080/user"
    log "Admin:     http://localhost:8080/admin"
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
