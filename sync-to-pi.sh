#!/usr/bin/env bash
# Deploy Solar Smart to Pi — one tarball, one SSH session, graceful reload.
#
# Usage (Git Bash / WSL / Linux on PC with SSH to Pi):
#   ./sync-to-pi.sh              # sync + graceful reload (default)
#   ./sync-to-pi.sh --no-restart # sync files only
#   ./sync-to-pi.sh --install-service

set -euo pipefail

LOCAL="$(cd "$(dirname "$0")" && pwd)"
REMOTE="${SMART_PI_SSH:-solar-assistant}"
REMOTE_DIR="${SMART_PI_DIR:-/home/solar-assistant/project}"
KEY="${SMART_PI_KEY:-$HOME/.ssh/id_ed25519}"
PI_URL="${SMART_PI_URL:-http://192.168.8.57:8000}"

NO_RESTART=0
INSTALL_SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --no-restart) NO_RESTART=1 ;;
    --install-service) INSTALL_SERVICE=1 ;;
    -h|--help)
      echo "Usage: $0 [--no-restart] [--install-service]"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

files=(
  src/main.py
  src/config.py
  src/config_templates.py
  src/cache_registry.py
  src/routes/__init__.py
  src/routes/ui.py
  src/routes/data.py
  src/routes/rules.py
  src/routes/config.py
  src/routes/ev.py
  src/routes/debug.py
  src/debug_smart_plan.py
  src/plan_q15.py
  src/plan_physics.py
  src/plan_types.py
  src/plan_orchestrator.py
  src/inverter_sim.py
  src/influxdb.py
  src/forecast.py
  src/forecast_cache.py
  src/ev_charging.py
  src/rce.py
  src/timer_plan.py
  src/simulation.py
  src/plan_optimizer.py
  src/plan_dp.py
  src/plan_charge.py
  src/plan_export.py
  src/plan_reserve.py
  src/plan_spill.py
  src/plan_hourly_actuals.py
  src/plan_monthly_history.py
  src/plan_baseline.py
  src/json_store.py
  src/plan_cost.py
  src/plan_deposits.py
  src/g12_pricing.py
  src/simulation_config.py
  src/plan_simulation.py
  src/scheduler.py
  src/hour_boundary_scheduler.py
  src/work_mode_scheduler.py
  src/sa_client.py
  src/templates/index.html
  src/static/app-icon.png
  src/static/favicon.ico
  src/static/favicon.png
  src/static/favicon-16.png
  src/static/favicon-32.png
  src/static/apple-touch-icon.png
  src/static/flow/solar.png
  src/static/flow/battery.png
  src/static/flow/grid.png
  src/static/flow/house.png
  src/static/flow/inverter.png
  scripts/reload-smart.sh
  scripts/restore-sa-defaults.sh
  scripts/enable-smart-autostart.sh
  systemd/smart.service
  systemd/smart-boot-guard.service
  systemd/smart-boot-guard.timer
  requirements.txt
  config-templates.yaml
)

if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  files+=(install.sh)
fi

stage="$(mktemp -d)"
tar_path="$(mktemp -t smart-deploy.XXXXXX.tgz)"
trap 'rm -rf "$stage" "$tar_path"' EXIT

missing=0
for file in "${files[@]}"; do
  local_path="$LOCAL/$file"
  if [[ ! -f "$local_path" ]]; then
    echo "  [SKIP] $file (not found)"
    missing=$((missing + 1))
    continue
  fi
  dest="$stage/$file"
  mkdir -p "$(dirname "$dest")"
  cp "$local_path" "$dest"
  echo "  [pack] $file"
done

if [[ "$missing" -gt 0 ]]; then
  echo "Warning: $missing file(s) missing — archive may be incomplete."
fi

tar -czf "$tar_path" -C "$stage" .

if ! tar -tzf "$tar_path" | grep -q 'scripts/reload-smart.sh'; then
  echo "Archive missing scripts/reload-smart.sh" >&2
  exit 1
fi
if ! tar -tzf "$tar_path" | grep -q 'src/plan_baseline.py'; then
  echo "Archive missing src/plan_baseline.py" >&2
  exit 1
fi

ssh_opts=(-o ConnectTimeout=30 -o ServerAliveInterval=15)
scp_opts=(-o StrictHostKeyChecking=accept-new)
if [[ -f "$KEY" ]]; then
  ssh_opts+=(-i "$KEY")
  scp_opts+=(-i "$KEY")
fi

echo "Uploading single archive ..."
scp "${scp_opts[@]}" "$tar_path" "${REMOTE}:${REMOTE_DIR}/smart-deploy.tgz"

remote_cmd="cd $REMOTE_DIR"
remote_cmd+="; tar -xzf smart-deploy.tgz"
remote_cmd+="; test -f scripts/reload-smart.sh"
remote_cmd+="; test -f systemd/smart.service"
remote_cmd+="; test -f systemd/smart-boot-guard.timer"
remote_cmd+="; rm -f smart-deploy.tgz"
remote_cmd+="; chmod +x scripts/reload-smart.sh scripts/enable-smart-autostart.sh"
remote_cmd+="; bash scripts/enable-smart-autostart.sh"

if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  remote_cmd+="; chmod +x scripts/restore-sa-defaults.sh"
  remote_cmd+="; bash scripts/restore-sa-defaults.sh"
fi

if [[ "$NO_RESTART" -eq 1 ]]; then
  remote_cmd+='; echo sync_ok'
else
  remote_cmd+="; bash scripts/reload-smart.sh"
fi

echo "Applying on Pi ..."
ssh "${ssh_opts[@]}" "$REMOTE" "$remote_cmd"

echo "Deploy complete. Refresh browser: $PI_URL/"
