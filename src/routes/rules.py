"""SA rules, smart mode, plan apply, hourly sync."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import sa_client
from ..config import load_config, save_config
from ..sqlite_store import read_plan
from ..timer_plan import build_sa_schedule_from_hour_row, hour_has_timer_schedule
from ..scheduler import get_last_hourly_sync, run_hourly_plan_sync
from ..hour_boundary_scheduler import (
    get_last_hour_boundary_sync,
    run_hour_boundary_limit_home,
    run_hour_boundary_start,
)
from ..work_mode_scheduler import get_last_work_mode_sync
from ..influxdb import now_warsaw

router = APIRouter()


@router.get("/api/rules")
async def api_rules(fresh: bool = False) -> dict[str, Any]:
    cfg = load_config()
    return await sa_client.get_rules(cfg, fresh=fresh)


@router.post("/api/rules/grid-charge")
async def api_set_grid_charge(body: dict) -> dict[str, Any]:
    cfg = load_config()
    enabled: bool = bool(body.get("enabled", False))
    power_kw: float = float(body.get("power_kw") or 0.0)
    current_raw = body.get("current_a")
    current_a: int | None = None
    if current_raw is not None and str(current_raw).strip() != "":
        current_a = int(float(current_raw))
    ok = await sa_client.set_grid_charging(
        cfg, enabled=enabled, power_kw=power_kw, current_a=current_a,
    )
    return {"ok": ok}


@router.post("/api/rules/grid-export")
async def api_set_grid_export(body: dict) -> dict[str, Any]:
    cfg = load_config()
    enabled: bool = bool(body.get("enabled", False))
    ok = await sa_client.set_grid_export(cfg, enabled=enabled)
    return {"ok": ok}


@router.post("/api/rules/timer-schedule")
async def api_set_timer_schedule(body: dict) -> dict[str, Any]:
    cfg = load_config()
    ok = await sa_client.set_timer_schedule(cfg, body)
    return {"ok": ok}


@router.post("/api/rules/work-mode")
async def api_set_work_mode(body: dict) -> dict[str, Any]:
    cfg = load_config()
    mode = str(body.get("work_mode", "")).strip()
    if not mode:
        return {"ok": False, "error": "work_mode required"}
    ok = await sa_client.set_work_mode(cfg, mode)
    result: dict[str, Any] = {"ok": ok, "work_mode": mode if ok else None}
    if ok:
        rules = await sa_client.get_rules(cfg, fresh=True)
        result["battery_discharge_mode"] = rules.get("battery_discharge_mode")
    return result


@router.post("/api/rules/battery-discharge-mode")
async def api_set_battery_discharge_mode(body: dict) -> dict[str, Any]:
    cfg = load_config()
    mode = str(body.get("battery_discharge_mode", "")).strip()
    if not mode:
        return {"ok": False, "error": "battery_discharge_mode required"}
    ok = await sa_client.set_battery_discharge_mode(cfg, mode)
    result: dict[str, Any] = {"ok": ok, "battery_discharge_mode": mode if ok else None}
    if ok:
        rules = await sa_client.get_rules(cfg, fresh=True)
        result["work_mode"] = rules.get("work_mode")
        result["battery_discharge_mode"] = rules.get("battery_discharge_mode")
    return result


@router.post("/api/rules/solar-power-priority")
async def api_set_solar_power_priority(body: dict) -> dict[str, Any]:
    cfg = load_config()
    priority = str(body.get("solar_power_priority", "")).strip()
    if not priority:
        return {"ok": False, "error": "solar_power_priority required"}
    ok = await sa_client.set_solar_power_priority(cfg, priority)
    return {"ok": ok, "solar_power_priority": priority if ok else None}


@router.post("/api/rules/timed-power")
async def api_set_timed_power(body: dict) -> dict[str, Any]:
    """SA Power tab: timed charge / timed discharge toggles."""
    cfg = load_config()
    charge = body.get("timed_charge_enabled")
    discharge = body.get("timed_discharge_enabled")
    if charge is None and discharge is None:
        return {"ok": False, "error": "timed_charge_enabled or timed_discharge_enabled required"}
    ok = await sa_client.set_timed_power_flags(
        cfg,
        timed_charge_enabled=bool(charge) if charge is not None else None,
        timed_discharge_enabled=bool(discharge) if discharge is not None else None,
    )
    return {
        "ok": ok,
        "timed_charge_enabled": bool(charge) if charge is not None else None,
        "timed_discharge_enabled": bool(discharge) if discharge is not None else None,
    }


@router.post("/api/smart-mode")
async def api_set_smart_mode(body: dict) -> dict[str, Any]:
    cfg = load_config()
    was_enabled = bool(cfg.get("smart_mode_enabled", False))
    enabled = bool(body.get("enabled", False))
    cfg["smart_mode_enabled"] = enabled
    save_config(cfg)
    result: dict[str, Any] = {"ok": True, "enabled": enabled}
    if enabled and not was_enabled:
        result["initial_sync"] = await run_hourly_plan_sync()
    return result


@router.get("/api/smart-mode")
async def api_get_smart_mode() -> dict[str, Any]:
    cfg = load_config()
    return {"enabled": bool(cfg.get("smart_mode_enabled", False))}


@router.post("/api/rules/apply-plan")
async def api_apply_plan() -> dict[str, Any]:
    cfg = load_config()
    sim_result = read_plan()
    if not sim_result:
        return {"ok": False, "skipped": True, "error": "no_plan_in_sqlite",
                "next_hour": None, "planned_action": None}
    now = now_warsaw()
    hour = now.hour
    rows = sim_result.get("rows") or []
    if not hour_has_timer_schedule(rows, hour):
        return {
            "ok": True,
            "skipped": True,
            "error": None,
            "next_hour": sim_result["next_hour"],
            "planned_action": None,
        }
    rules = await sa_client.get_rules(cfg)
    metrics = await sa_client.get_live_metrics(cfg)
    live_soc = metrics.get("battery_soc")
    try:
        live_soc_pct = float(live_soc) if live_soc is not None else None
    except (TypeError, ValueError):
        live_soc_pct = None
    schedule = build_sa_schedule_from_hour_row(
        rows,
        hour,
        cfg,
        existing=rules,
        live_soc_pct=live_soc_pct,
    )
    if not schedule:
        return {
            "ok": False,
            "skipped": True,
            "error": "Could not parse current hour Timer Schedule",
            "next_hour": sim_result["next_hour"],
            "planned_action": None,
        }
    ok = await sa_client.apply_hourly_schedule_to_sa(cfg, schedule)
    return {
        "ok": ok,
        "error": None if ok else (
            "SolarAssistant rejected the timer write. "
            "Check SA → Configuration → Timer schedule manually, or reboot SolarAssistant if writes fail."
        ),
        "next_hour": sim_result["next_hour"],
        "next_hour_schedule": schedule,
        "planned_action": schedule.get("planned_action"),
    }


@router.get("/api/hourly-sync/status")
async def api_hourly_sync_status() -> dict[str, Any]:
    """Last backend hourly job result (plan refresh + optional SA timer write)."""
    cfg = load_config()
    status = get_last_hourly_sync()
    return {
        **status,
        "smart_mode_enabled_now": bool(cfg.get("smart_mode_enabled", False)),
        "scheduler_active": True,
    }


@router.post("/api/rules/sync-hour")
async def api_sync_hour() -> dict[str, Any]:
    """Run hourly plan→SA sync immediately (same as the :00 cron job)."""
    return await run_hourly_plan_sync()


@router.get("/api/work-mode-sync/status")
async def api_work_mode_sync_status() -> dict[str, Any]:
    """Last work-mode job result (On-grid / Limit home)."""
    cfg = load_config()
    return {
        **get_last_work_mode_sync(),
        "smart_mode_enabled_now": bool(cfg.get("smart_mode_enabled", False)),
    }


@router.get("/api/hour-boundary-sync/status")
async def api_hour_boundary_sync_status() -> dict[str, Any]:
    """Last hour-boundary SA sync (timer row + work mode)."""
    cfg = load_config()
    return {
        **get_last_hour_boundary_sync(),
        "smart_mode_enabled_now": bool(cfg.get("smart_mode_enabled", False)),
    }


@router.post("/api/rules/work-mode-start")
async def api_work_mode_start() -> dict[str, Any]:
    """Manual trigger — same as :00 hour-boundary start."""
    return await run_hour_boundary_start()


@router.post("/api/rules/work-mode-end")
async def api_work_mode_end() -> dict[str, Any]:
    """Manual trigger — same as :15/:30/:45 Limit home boundary job."""
    return await run_hour_boundary_limit_home()
