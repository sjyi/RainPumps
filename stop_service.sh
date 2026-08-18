#!/usr/bin/env bash
# stop_service.sh — disable Rain Pumps auto-start and remove the watchdog cron
#
# Stops the systemd unit (if installed), removes the cron watchdog, and runs
# stop_system.sh so containers shut down cleanly. After this, nothing will
# restart the stack until you run start_service.sh again.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUMPD_DIR="${PUMPD_DIR:-${SCRIPT_DIR}/pumpd}"
SERVICE_NAME="${SERVICE_NAME:-rainpumps}"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"

log() { printf '[stop_service] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

disable_user_linger() {
  local user linger_marker="${LOG_DIR}/.user-linger-enabled"
  if [[ ! -f "${linger_marker}" ]]; then
    return 0
  fi
  if ! command -v loginctl >/dev/null 2>&1; then
    rm -f "${linger_marker}"
    return 0
  fi
  user="$(id -un)"
  if loginctl disable-linger "${user}" 2>/dev/null; then
    log "Disabled user linger for ${user}"
  else
    log "warning: could not disable user linger — try: loginctl disable-linger ${user}"
  fi
  rm -f "${linger_marker}"
}

remove_watchdog_cron() {
  local removed=0

  if [[ -f "/etc/cron.d/${SERVICE_NAME}" ]]; then
    if [[ ${EUID} -eq 0 ]]; then
      rm -f "/etc/cron.d/${SERVICE_NAME}"
      log "Removed /etc/cron.d/${SERVICE_NAME}"
      removed=1
    elif command -v sudo >/dev/null 2>&1; then
      sudo rm -f "/etc/cron.d/${SERVICE_NAME}"
      log "Removed /etc/cron.d/${SERVICE_NAME}"
      removed=1
    else
      log "warning: /etc/cron.d/${SERVICE_NAME} exists but needs root to remove"
    fi
  fi

  if crontab -l 2>/dev/null | grep -q 'rainpumps_watchdog.sh'; then
    crontab -l 2>/dev/null \
      | grep -v 'rainpumps_watchdog.sh' \
      | grep -v '# rainpumps-watchdog' \
      | crontab -
    log "Removed rainpumps watchdog from user crontab"
    removed=1
  fi

  if [[ ${removed} -eq 0 ]]; then
    log "No watchdog cron entry found"
  fi
}

stop_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    return 0
  fi

  local stopped=0

  if systemctl list-unit-files "${SERVICE_NAME}.service" 2>/dev/null | grep -q "${SERVICE_NAME}.service"; then
    if [[ ${EUID} -eq 0 ]]; then
      systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
      rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
      systemctl daemon-reload
      log "Stopped and disabled system unit ${SERVICE_NAME}.service"
      stopped=1
    elif sudo -n systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null; then
      sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
      sudo systemctl daemon-reload
      log "Stopped and disabled system unit ${SERVICE_NAME}.service"
      stopped=1
    fi
  fi

  if systemctl --user list-unit-files "${SERVICE_NAME}.service" 2>/dev/null | grep -q "${SERVICE_NAME}.service"; then
    systemctl --user disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
    rm -f "${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
    systemctl --user daemon-reload
    log "Stopped and disabled user unit ${SERVICE_NAME}.service"
    stopped=1
  fi

  if [[ ${stopped} -eq 0 ]]; then
    log "No systemd unit found for ${SERVICE_NAME}"
  fi
}

log "Disabling Rain Pumps service"

# Remove cron first so watchdog cannot restart while we shut down
remove_watchdog_cron
stop_systemd
disable_user_linger

if [[ -x "${SCRIPT_DIR}/stop_system.sh" ]]; then
  log "Stopping Docker stack"
  "${SCRIPT_DIR}/stop_system.sh"
else
  die "stop_system.sh not found"
fi

log "Service disabled — stack stopped, auto-restart removed"
log "Start again with: ${SCRIPT_DIR}/start_service.sh"
