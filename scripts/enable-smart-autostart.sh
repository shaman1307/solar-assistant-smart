#!/usr/bin/env bash
# Enable Solar Smart autostart on boot (systemd).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNITS_DIR="${PROJECT_DIR}/systemd"
UNIT=smart
GUARD=smart-boot-guard
LEGACY=solar-smart.service
LEGACY_PATH="/etc/systemd/system/${LEGACY}"
WANTS="/etc/systemd/system/multi-user.target.wants/${UNIT}.service"
TIMER_WANTS="/etc/systemd/system/timers.target.wants/${GUARD}.timer"

install_unit() {
  local name="$1"
  local src="${UNITS_DIR}/${name}.service"
  if [ ! -f "${src}" ]; then
    echo "[enable-smart-autostart] ERROR: missing ${src}" >&2
    exit 1
  fi
  sudo cp "${src}" "/etc/systemd/system/${name}.service"
}

echo "[enable-smart-autostart] installing ${UNIT}.service ..."
install_unit "${UNIT}"

if [ -f "${UNITS_DIR}/${GUARD}.service" ]; then
  install_unit "${GUARD}"
  sudo cp "${UNITS_DIR}/${GUARD}.timer" "/etc/systemd/system/${GUARD}.timer"
fi
rm -f "${PROJECT_DIR}/smart.service" "${PROJECT_DIR}/smart-boot-guard.service" "${PROJECT_DIR}/smart-boot-guard.timer"

if [ -f "${LEGACY_PATH}" ]; then
  echo "[enable-smart-autostart] removing ${LEGACY} ..."
  sudo systemctl disable --now "${LEGACY}" 2>/dev/null || true
  sudo rm -f "${LEGACY_PATH}"
fi

if systemctl is-masked --quiet "${UNIT}.service" 2>/dev/null; then
  echo "[enable-smart-autostart] unmasking ${UNIT}.service ..."
  sudo systemctl unmask "${UNIT}.service"
fi

sudo systemctl daemon-reload
sudo systemctl enable --now "${UNIT}.service"
sudo systemctl reset-failed "${UNIT}.service" 2>/dev/null || true

if [ -f "/etc/systemd/system/${GUARD}.timer" ]; then
  sudo systemctl enable --now "${GUARD}.timer"
fi

ENABLED="$(systemctl is-enabled "${UNIT}.service" 2>/dev/null || echo unknown)"
if [ "${ENABLED}" != "enabled" ]; then
  echo "[enable-smart-autostart] ERROR: ${UNIT}.service is not enabled (got ${ENABLED})" >&2
  exit 1
fi

if [ ! -L "${WANTS}" ]; then
  echo "[enable-smart-autostart] ERROR: boot symlink missing: ${WANTS}" >&2
  exit 1
fi

if [ -f "/etc/systemd/system/${GUARD}.timer" ]; then
  GUARD_STATE="$(systemctl is-enabled "${GUARD}.timer" 2>/dev/null || echo unknown)"
  if [ "${GUARD_STATE}" != "enabled" ]; then
    echo "[enable-smart-autostart] ERROR: ${GUARD}.timer is not enabled (got ${GUARD_STATE})" >&2
    exit 1
  fi
  if [ ! -L "${TIMER_WANTS}" ]; then
    echo "[enable-smart-autostart] ERROR: timer symlink missing: ${TIMER_WANTS}" >&2
    exit 1
  fi
fi

ACTIVE="$(systemctl is-active "${UNIT}.service" 2>/dev/null || echo unknown)"
if [ "${ACTIVE}" != "active" ]; then
  echo "[enable-smart-autostart] starting ${UNIT} ..."
  sudo systemctl start "${UNIT}.service"
  ACTIVE="$(systemctl is-active "${UNIT}.service" 2>/dev/null || echo unknown)"
fi

echo "[enable-smart-autostart] enabled=${ENABLED} active=${ACTIVE}"
if [ -f "/etc/systemd/system/${GUARD}.timer" ]; then
  echo "[enable-smart-autostart] boot-guard=$(systemctl is-enabled ${GUARD}.timer) next=$(systemctl show ${GUARD}.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo n/a)"
fi

if [ "${ACTIVE}" != "active" ]; then
  echo "[enable-smart-autostart] WARN: ${UNIT} not active — last boot log:" >&2
  journalctl -u "${UNIT}.service" -b --no-pager -n 15 >&2 || true
  exit 1
fi
