#!/usr/bin/env bash
# fix_ghost_pumpd.sh — remove persisted ghost pumpd container (run with sudo)
#
# Docker Snap keeps restarting container 22b6f4e0c320 on port 8080 because its
# state is saved on disk (restart: unless-stopped). kill/restart only respawns it.
#
# Usage: sudo ./fix_ghost_pumpd.sh
set -euo pipefail

PUMPD_PORT="${PUMPD_PORT:-8080}"
DOCKER_ROOT="${DOCKER_ROOT:-/var/snap/docker/common/var-lib-docker}"

log() { printf '[fix_ghost_pumpd] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ ${EUID:-0} -eq 0 ]] || die "Run as root: sudo $0"

detect_container_id() {
  local cid=""
  cid=$(pgrep -af 'containerd-shim-runc-v2' 2>/dev/null | grep -oE '\-id [a-f0-9]{64}' | head -1 | awk '{print $2}' || true)
  if [[ -z "${cid}" ]]; then
    cid=$(find "${DOCKER_ROOT}/containers" -maxdepth 1 -type d -name '22b6f4e0c320*' 2>/dev/null | head -1 | xargs basename 2>/dev/null || true)
  fi
  if [[ -z "${cid}" ]]; then
    cid="22b6f4e0c32053cdf28be32ba3724a4fa925d9620fff7914386b7d433752d130"
  fi
  echo "${cid}"
}

port_in_use() {
  ss -tln | grep -q ":${PUMPD_PORT} "
}

CID=$(detect_container_id)
CID_SHORT="${CID:0:12}"
log "Target ghost container: ${CID_SHORT} (${CID})"

if ! port_in_use && ! pgrep -f 'uvicorn app.main:app' >/dev/null 2>&1; then
  log "Port ${PUMPD_PORT} already free — nothing to do"
  exit 0
fi

log "Stopping Docker (snap)..."
if command -v snap >/dev/null 2>&1; then
  snap stop docker
else
  systemctl stop snap.docker.dockerd.service 2>/dev/null || systemctl stop docker.service 2>/dev/null || true
fi
sleep 2

log "Killing surviving processes on port ${PUMPD_PORT}..."
pkill -9 -f "containerd-shim.*${CID_SHORT}" 2>/dev/null || true
pkill -9 -f 'uvicorn app.main:app' 2>/dev/null || true
pkill -9 -f "docker-proxy.*host-port ${PUMPD_PORT}" 2>/dev/null || true
if command -v lsof >/dev/null 2>&1; then
  kill -9 $(lsof -t -iTCP:"${PUMPD_PORT}" -sTCP:LISTEN 2>/dev/null || true) 2>/dev/null || true
fi

log "Removing persisted container state from disk..."
removed=0
if [[ -d "${DOCKER_ROOT}/containers/${CID}" ]]; then
  rm -rf "${DOCKER_ROOT}/containers/${CID}"
  log "Removed ${DOCKER_ROOT}/containers/${CID}"
  removed=1
fi
while IFS= read -r dir; do
  [[ -n "${dir}" ]] || continue
  rm -rf "${dir}"
  log "Removed ${dir}"
  removed=1
done < <(find "${DOCKER_ROOT}/containers" -maxdepth 1 -type d -name "${CID_SHORT}*" 2>/dev/null || true)

CRI="${DOCKER_ROOT}/containerd/daemon/io.containerd.grpc.v1.cri/containers"
if [[ -d "${CRI}" ]]; then
  while IFS= read -r meta; do
    [[ -f "${meta}" ]] || continue
    if grep -q "${CID_SHORT}" "${meta}" 2>/dev/null; then
      rm -f "${meta}"
      log "Removed containerd CRI metadata ${meta}"
      removed=1
    fi
  done < <(find "${CRI}" -maxdepth 1 -type f 2>/dev/null || true)
fi

TASKS="${DOCKER_ROOT}/containerd/daemon/io.containerd.grpc.v1.cri/sandboxes"
# sandboxes usually not the issue for this container type

if [[ "${removed}" -eq 0 ]]; then
  log "warning: no container directories found under ${DOCKER_ROOT}/containers/"
  log "Listing containers dir:"
  ls -la "${DOCKER_ROOT}/containers/" 2>/dev/null | tail -10 || true
fi

log "Starting Docker (snap)..."
if command -v snap >/dev/null 2>&1; then
  snap start docker
else
  systemctl start snap.docker.dockerd.service 2>/dev/null || systemctl start docker.service 2>/dev/null || true
fi
sleep 4

if port_in_use || pgrep -f 'uvicorn app.main:app' >/dev/null 2>&1; then
  log "FAILED — port ${PUMPD_PORT} still blocked:"
  lsof -i:"${PUMPD_PORT}" 2>/dev/null || ss -tln | grep "${PUMPD_PORT}" || true
  log ""
  log "Last resort: sudo snap remove docker --purge  (removes ALL containers/images)"
  log "Or change pumpd port in pumpd/docker-compose.yml to 8081:8080 temporarily"
  exit 1
fi

log "SUCCESS — port ${PUMPD_PORT} is free"
log "Run: ./start_system.sh"
