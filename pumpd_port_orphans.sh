#!/usr/bin/env bash
# pumpd_port_orphans.sh — find and stop ghost pumpd containers on port 8080
set -euo pipefail

PUMPD_PORT="${PUMPD_PORT:-8080}"

log() { printf '[pumpd_port_orphans] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

ghost_shim_pid() {
  local shim
  for shim in $(pgrep -f 'containerd-shim-runc-v2' 2>/dev/null || true); do
    if pgrep -P "${shim}" -f "uvicorn app.main:app --host 0.0.0.0 --port ${PUMPD_PORT}" >/dev/null 2>&1; then
      echo "${shim}"
      return 0
    fi
  done
}

ghost_container_id() {
  local shim
  shim=$(ghost_shim_pid || true)
  [[ -n "${shim}" ]] || return 0
  ps -p "${shim}" -o args= 2>/dev/null | grep -oE '\-id [a-f0-9]{64}' | awk '{print $2}' | head -1
}

orphan_pumpd_pids() {
  local pids=""
  if pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port ${PUMPD_PORT}" >/dev/null 2>&1; then
    pids=$(pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port ${PUMPD_PORT}" 2>/dev/null || true)
  fi
  if pgrep -f "docker-proxy.*host-port ${PUMPD_PORT}" >/dev/null 2>&1; then
    pids=$(printf '%s\n%s' "${pids}" "$(pgrep -f "docker-proxy.*host-port ${PUMPD_PORT}" 2>/dev/null || true)")
  fi
  local shim
  shim=$(ghost_shim_pid || true)
  if [[ -n "${shim}" ]]; then
    pids=$(printf '%s\n%s' "${pids}" "${shim}")
  fi
  printf '%s\n' "${pids}" | sed '/^$/d' | sort -u
}

port_in_use() {
  ss -tln 2>/dev/null | grep -q ":${PUMPD_PORT} "
}

print_nuclear_fix() {
  local cid="${1:-}"
  echo "Ghost Docker/containerd state — kill/restart alone respawns processes."
  echo ""
  echo "Run these commands in order:"
  echo ""
  if [[ -n "${cid}" ]]; then
    echo "  docker rm -f ${cid:0:12}    # may no-op if already removed"
  fi
  echo "  sudo snap stop docker       # full stop (NOT restart)"
  echo "  sudo kill -9 \$(sudo lsof -t -i:${PUMPD_PORT}) 2>/dev/null || true"
  if [[ -n "${cid}" ]]; then
    echo "  sudo pkill -9 -f 'containerd-shim.*${cid:0:12}' || true"
  fi
  echo "  sudo snap start docker"
  echo "  ./pumpd_port_orphans.sh     # should report port free"
  echo "  ./start_system.sh"
}

show_status() {
  mapfile -t pids < <(orphan_pumpd_pids)
  local cid
  cid=$(ghost_container_id || true)

  if ((${#pids[@]} == 0)); then
    echo "No orphaned pumpd processes on port ${PUMPD_PORT}"
    exit 0
  fi

  echo "Orphaned processes on port ${PUMPD_PORT}:"
  for pid in "${pids[@]}"; do
    ps -p "${pid}" -o pid=,user=,args= 2>/dev/null || echo "  ${pid} (gone)"
  done
  echo ""

  if [[ -n "${cid}" ]]; then
    echo "Ghost Docker container: ${cid:0:12} (containerd-shim still running)"
    echo "Docker keeps restoring it from disk on every snap start (restart: unless-stopped)."
    echo ""
    echo "Fix:"
    echo "  sudo ./fix_ghost_pumpd.sh"
    echo ""
    print_nuclear_fix "${cid}"
  else
    echo "Stop them with:"
    echo "  sudo kill -9 ${pids[*]}"
  fi
}

try_remove() {
  local cid
  cid=$(ghost_container_id || true)

  if [[ -n "${cid}" ]]; then
    log "Ghost container ${cid:0:12} — trying docker rm..."
    docker rm -f "${cid}" 2>/dev/null || docker rm -f "${cid:0:12}" 2>/dev/null || true
  fi

  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    log "Stopping Docker (snap)..."
    if command -v snap >/dev/null 2>&1; then
      snap stop docker || true
    fi
    sleep 2
    mapfile -t pids < <(orphan_pumpd_pids)
    if ((${#pids[@]} > 0)); then
      log "Killing leftover PIDs: ${pids[*]}"
      kill -9 "${pids[@]}" 2>/dev/null || true
    fi
    if command -v lsof >/dev/null 2>&1; then
      mapfile -t port_pids < <(lsof -tiTCP:"${PUMPD_PORT}" -sTCP:LISTEN 2>/dev/null || true)
      ((${#port_pids[@]})) && kill -9 "${port_pids[@]}" 2>/dev/null || true
    fi
    if command -v snap >/dev/null 2>&1; then
      snap start docker || true
    fi
    sleep 3
    if ! port_in_use; then
      log "Port ${PUMPD_PORT} is free"
      exit 0
    fi
    die "Port ${PUMPD_PORT} still in use after cleanup"
  fi

  if port_in_use; then
    echo ""
    print_nuclear_fix "${cid}"
    exit 1
  fi
  log "Port ${PUMPD_PORT} is free"
}

case "${1:-}" in
  --remove|--fix)
    try_remove
    ;;
  --kill-cmd)
    cid=$(ghost_container_id || true)
    if [[ -n "${cid}" ]]; then
      echo "sudo snap stop docker && sudo kill -9 \$(sudo lsof -t -i:${PUMPD_PORT}) 2>/dev/null; sudo pkill -9 -f 'containerd-shim.*${cid:0:12}'; sudo snap start docker"
    else
      mapfile -t pids < <(orphan_pumpd_pids)
      ((${#pids[@]})) && echo "sudo kill -9 ${pids[*]}"
    fi
    ;;
  *)
    show_status
    ;;
esac
