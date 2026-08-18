#!/usr/bin/env bash
# start_service.sh — install Rain Pumps as a persistent service (systemd + watchdog cron)
#
# Installs:
#   • systemd unit (rainpumps.service) — starts on boot
#   • cron watchdog — rechecks /health every 5 minutes and restarts if down
#
# Requires: docker, curl, systemd (recommended). Run once to enable auto-start.
# For system-wide boot without login: sudo ./start_service.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUMPD_DIR="${PUMPD_DIR:-${SCRIPT_DIR}/pumpd}"
SERVICE_NAME="${SERVICE_NAME:-rainpumps}"
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-5}"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/health}"

log() { printf '[start_service] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

chmod +x "${SCRIPT_DIR}/start_system.sh" \
         "${SCRIPT_DIR}/stop_system.sh" \
         "${SCRIPT_DIR}/rainpumps_watchdog.sh" 2>/dev/null || true

[[ -d "${PUMPD_DIR}" ]] || die "pumpd directory not found: ${PUMPD_DIR}"
command -v docker >/dev/null 2>&1 || die "docker is not installed"
command -v curl >/dev/null 2>&1 || die "curl is not installed (needed for health checks)"

mkdir -p "${LOG_DIR}"

generate_unit() {
  cat <<EOF
[Unit]
Description=Rain Roof Pumps (pumpd Docker stack)
Documentation=file://${SCRIPT_DIR}/startup.md
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${PUMPD_DIR}
Environment=SCRIPT_DIR=${SCRIPT_DIR}
Environment=PUMPD_DIR=${PUMPD_DIR}
Environment=HEALTH_URL=${HEALTH_URL}
Environment=BUILD=0
ExecStart=${SCRIPT_DIR}/start_system.sh
ExecStop=${SCRIPT_DIR}/stop_system.sh
TimeoutStartSec=300
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
}

install_systemd_system() {
  local unit_file="/etc/systemd/system/${SERVICE_NAME}.service"
  log "Installing systemd unit: ${unit_file}"
  generate_unit | tee "${unit_file}" >/dev/null
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"
  systemctl start "${SERVICE_NAME}.service"
  log "systemd service enabled and started: ${SERVICE_NAME}.service"
}

install_systemd_user() {
  local unit_dir="${HOME}/.config/systemd/user"
  local unit_file="${unit_dir}/${SERVICE_NAME}.service"
  mkdir -p "${unit_dir}"
  log "Installing user systemd unit: ${unit_file}"
  generate_unit | sed 's/WantedBy=multi-user.target/WantedBy=default.target/' >"${unit_file}"
  systemctl --user daemon-reload
  systemctl --user enable "${SERVICE_NAME}.service"
  systemctl --user start "${SERVICE_NAME}.service"
  log "User systemd service enabled and started"
  enable_user_linger
}

enable_user_linger() {
  local user linger_marker="${LOG_DIR}/.user-linger-enabled"
  if ! command -v loginctl >/dev/null 2>&1; then
    log "warning: loginctl not found — cannot enable user linger"
    return 0
  fi
  user="$(id -un)"
  if loginctl show-user "${user}" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    log "User linger already enabled for ${user}"
  elif loginctl enable-linger "${user}" 2>/dev/null; then
    log "Enabled user linger for ${user} (starts at boot without login)"
  else
    log "warning: could not enable user linger — try: loginctl enable-linger ${user}"
    return 0
  fi
  date -Iseconds >"${linger_marker}" 2>/dev/null || true
}

install_watchdog_cron() {
  local watchdog="${SCRIPT_DIR}/rainpumps_watchdog.sh"
  local cron_log="${LOG_DIR}/watchdog.log"
  local cron_line="*/${WATCHDOG_INTERVAL} * * * * HEALTH_URL=${HEALTH_URL} ${watchdog} >> ${cron_log} 2>&1"

  if [[ ${EUID} -eq 0 ]] && [[ -d /etc/cron.d ]]; then
    local cron_file="/etc/cron.d/${SERVICE_NAME}"
    log "Installing watchdog cron: ${cron_file} (every ${WATCHDOG_INTERVAL} min)"
    cat >"${cron_file}" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
${cron_line}
EOF
    chmod 644 "${cron_file}"
    return 0
  fi

  log "Installing watchdog in user crontab (every ${WATCHDOG_INTERVAL} min)"
  local existing=""
  existing="$(crontab -l 2>/dev/null || true)"
  printf '%s\n' "${existing}" \
    | grep -v 'rainpumps_watchdog.sh' \
    | grep -v '# rainpumps-watchdog' \
    | { cat; echo "# rainpumps-watchdog"; echo "${cron_line}"; } \
    | crontab -
}

install_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    log "warning: systemd not found — skipping unit install (cron watchdog only)"
    return 0
  fi

  if [[ ${EUID} -eq 0 ]]; then
    install_systemd_system
    return 0
  fi

  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    log "Installing system-wide service with sudo"
    generate_unit | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}.service"
    sudo systemctl start "${SERVICE_NAME}.service"
    log "systemd service enabled and started: ${SERVICE_NAME}.service"
    return 0
  fi

  install_systemd_user
}

log "Rain Pumps service installer"
log "Project: ${SCRIPT_DIR}"

install_systemd
install_watchdog_cron

if curl -sf --max-time 10 "${HEALTH_URL}" >/dev/null 2>&1; then
  log "Health check OK: ${HEALTH_URL}"
else
  log "warning: health check not yet OK — watchdog will retry"
fi

log ""
log "Service installed."
log "  Dashboard: http://localhost:8080/user"
log "  Status:    systemctl status ${SERVICE_NAME}  (or systemctl --user status ${SERVICE_NAME})"
log "  Logs:      ${LOG_DIR}/watchdog.log"
log "  Disable:   ${SCRIPT_DIR}/stop_service.sh"
