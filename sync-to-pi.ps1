# Deploy Smart app to Pi - one tarball, one SSH session, graceful reload.
#
# Usage:
#   .\sync-to-pi.ps1              # sync + graceful reload (default)
#   .\sync-to-pi.ps1 -NoRestart   # sync files only (restart manually later)
#   .\sync-to-pi.ps1 -InstallService  # also install smart.service unit file

param(
    [switch]$NoRestart,
    [switch]$InstallService
)

$LOCAL  = $PSScriptRoot
$REMOTE = "solar-assistant"
$REMOTE_DIR = "/home/solar-assistant/project"
$KEY    = "$env:USERPROFILE\.ssh\id_ed25519"
$PI_URL = "http://192.168.8.57:8000"

$files = @(
    "src\main.py",
    "src\config.py",
    "src\config_templates.py",
    "src\cache_registry.py",
    "src\routes\__init__.py",
    "src\routes\ui.py",
    "src\routes\data.py",
    "src\routes\rules.py",
    "src\routes\config.py",
    "src\routes\ev.py",
    "src\routes\debug.py",
    "src\debug_smart_plan.py",
    "src\plan_q15.py",
    "src\plan_physics.py",
    "src\plan_types.py",
    "src\plan_orchestrator.py",
    "src\debug_plan.py",
    "src\app_logging.py",
    "src\inverter_sim.py",
    "src\influxdb.py",
    "src\forecast.py",
    "src\forecast_cache.py",
    "src\ev_charging.py",
    "src\rce.py",
    "src\timer_plan.py",
    "src\simulation.py",
    "src\plan_optimizer.py",
    "src\plan_dp.py",
    "src\plan_charge.py",
    "src\plan_export.py",
    "src\plan_reserve.py",
    "src\plan_spill.py",
    "src\plan_hourly_actuals.py",
    "src\plan_monthly_history.py",
    "src\plan_monthly_refresh.py",
    "src\plan_baseline.py",
    "src\json_store.py",
    "src\plan_cost.py",
    "src\plan_deposits.py",
    "src\g12_pricing.py",
    "src\grid_config.py",
    "src\sqlite_store.py",
    "src\simulation_config.py",
    "src\plan_simulation.py",
    "src\plan_cache_merge.py",
    "src\plan_timer_override.py",
    "src\scheduler.py",
    "src\hour_boundary_scheduler.py",
    "src\work_mode_scheduler.py",
    "src\sa_client.py",
    "src\templates\index.html",
    "src\static\app-icon.png",
    "src\static\favicon.ico",
    "src\static\favicon.png",
    "src\static\favicon-16.png",
    "src\static\favicon-32.png",
    "src\static\apple-touch-icon.png",
    "src\static\flow\solar.png",
    "src\static\flow\battery.png",
    "src\static\flow\grid.png",
    "src\static\flow\house.png",
    "src\static\flow\inverter.png",
    "scripts\reload-smart.sh",
    "scripts\restore-sa-defaults.sh",
    "scripts\enable-smart-autostart.sh",
    "systemd\smart.service",
    "systemd\smart-boot-guard.service",
    "systemd\smart-boot-guard.timer",
    # sa-config.yaml is Pi-local state (overrides, passwords) — never overwrite on deploy.
    "requirements.txt",
    "config-templates.yaml"
)

if ($InstallService) {
    $files += @("install.sh")
}

$stage = Join-Path $env:TEMP "smart-deploy-stage"
$tarPath = Join-Path $env:TEMP "smart-deploy.tgz"

Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$missing = 0
foreach ($file in $files) {
    $localPath = Join-Path $LOCAL $file
    if (-not (Test-Path $localPath)) {
        Write-Host "  [SKIP] $file (not found)"
        $missing++
        continue
    }
    $parts = $file -split '\\'
    $dest = $stage
    foreach ($p in $parts) {
        $dest = Join-Path $dest $p
    }
    $destDir = Split-Path $dest -Parent
    if ($destDir -and -not (Test-Path $destDir)) {
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    }
    if ($file -like "*.sh") {
        $text = [System.IO.File]::ReadAllText($localPath) -replace "`r`n", "`n" -replace "`r", "`n"
        [System.IO.File]::WriteAllText($dest, $text)
    } else {
        Copy-Item $localPath $dest -Force
    }
    Write-Host "  [pack] $file"
}

if ($missing -gt 0) {
    Write-Host "Warning: ${missing} file(s) missing - archive may be incomplete."
}

Remove-Item $tarPath -ErrorAction SilentlyContinue
Push-Location $stage
tar -czf $tarPath .
Pop-Location

$tarListStr = (tar -tzf $tarPath 2>&1 | Out-String)
if ($tarListStr -notmatch 'reload-smart\.sh') {
    Write-Host "Archive missing scripts/reload-smart.sh:"
    Write-Host $tarListStr
    exit 1
}
if ($tarListStr -notmatch 'src/plan_baseline\.py') {
    Write-Host "Archive missing src/plan_baseline.py"
    exit 1
}

Write-Host "Uploading single archive ..."
$scpResult = scp -i $KEY -o StrictHostKeyChecking=accept-new $tarPath "${REMOTE}:${REMOTE_DIR}/smart-deploy.tgz" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Upload failed: $scpResult"
    exit 1
}

$remoteParts = @(
    "cd $REMOTE_DIR",
    "tar -xzf smart-deploy.tgz",
    "test -f scripts/reload-smart.sh",
    "test -f systemd/smart.service",
    "test -f systemd/smart-boot-guard.timer",
    "rm -f smart-deploy.tgz",
    "chmod +x scripts/reload-smart.sh scripts/enable-smart-autostart.sh",
    "bash scripts/enable-smart-autostart.sh"
)

if ($InstallService) {
    $remoteParts += @(
        "chmod +x scripts/restore-sa-defaults.sh",
        "bash scripts/restore-sa-defaults.sh"
    )
}

if ($NoRestart) {
    $remoteParts += 'echo sync_ok'
} else {
    $remoteParts += "bash scripts/reload-smart.sh"
}

$remoteCmd = $remoteParts -join "; "

Write-Host "Applying on Pi ..."
$deployOut = ssh -i $KEY -o ConnectTimeout=30 -o ServerAliveInterval=15 $REMOTE $remoteCmd 2>&1
Write-Host $deployOut

if ($LASTEXITCODE -ne 0) {
    Write-Host "Deploy failed (SSH exit $LASTEXITCODE)."
    exit 1
}

Write-Host "Deploy complete. Refresh browser: $PI_URL/"
