#!/usr/bin/env bash
# rainpumps_watchdog.sh — restart pumpd if the health check fails
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/health}"
START_SCRIPT="${START_SCRIPT:-${SCRIPT_DIR}/start_system.sh}"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
STAMP_FILE="${LOG_DIR}/watchdog.last"

log() { printf '[rainpumps_watchdog %(%Y-%m-%d %H:%M:%S)T] %s\n' -1 "$*"; }

mkdir -p "${LOG_DIR}"

if curl -sf --max-time 10 "${HEALTH_URL}" >/dev/null 2>&1; then
  date -Iseconds >"${STAMP_FILE}" 2>/dev/null || true
  exit 0
fi

log "Health check failed at ${HEALTH_URL} — restarting stack"
if [[ ! -x "${START_SCRIPT}" ]]; then
  log "ERROR: start script not executable: ${START_SCRIPT}"
  exit 1
fi

BUILD=0 "${START_SCRIPT}" >>"${LOG_DIR}/watchdog.log" 2>&1 || {
  log "ERROR: restart failed — see ${LOG_DIR}/watchdog.log"
  exit 1
}

log "Restart completed"
date -Iseconds >"${STAMP_FILE}" 2>/dev/null || true
