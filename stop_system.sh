#!/usr/bin/env bash
# stop_system.sh — gracefully shut down the pumpd stack
#
# Sends SIGTERM to containers so pumpd can run its shutdown handler (pumps off
# except manual_on, MQTT disconnect, DB flush) before containers are removed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUMPD_DIR="${PUMPD_DIR:-${SCRIPT_DIR}/pumpd}"
COMPOSE_FILE="${COMPOSE_FILE:-${PUMPD_DIR}/docker-compose.yml}"
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"

log() { printf '[stop_system] %s\n' "$*"; }
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

if ! "${COMPOSE[@]}" ps -q 2>/dev/null | grep -q .; then
  ORPHAN_HELPER="${SCRIPT_DIR}/pumpd_port_orphans.sh"
  if [[ -x "${ORPHAN_HELPER}" ]]; then
    "${ORPHAN_HELPER}"
  elif ss -tln 2>/dev/null | grep -q ':8080 '; then
    log "No compose containers running, but port 8080 is still in use"
    log "Inspect with: sudo lsof -i:8080"
  else
    log "No running containers for this stack — nothing to stop"
  fi
  exit 0
fi

log "Stopping pumpd stack (grace period ${STOP_TIMEOUT}s for clean pump shutdown)"
"${COMPOSE[@]}" ps

# Graceful stop: SIGTERM → pumpd lifespan shutdown → then remove containers/network
"${COMPOSE[@]}" down --timeout "${STOP_TIMEOUT}" --remove-orphans

log "Stack stopped"
log "SQLite data is preserved in the pumpd-data Docker volume (or ./data for local dev)"
log "Start again with: ${SCRIPT_DIR}/start_system.sh"
