"""
Balance automation scheduler.

  - Nightly at 23:59 Europe/Warsaw: build Load+PV day cache for tomorrow and day-after,
    then refresh charge-rate estimate in config (Δ).
  - Every :00:02/:15:02/:31:02/:45:02: refresh Open-Meteo PV, then Plan Simulation, then SA.
  - One tick snapshot per job (floor to :00/:15/:30/:45) is shared by sim, merge, write, SA.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import forecast as forecast_mod
from . import rce as rce_mod
from .config import load_config, save_config
from .hour_boundary_scheduler import (
    register_hour_boundary_jobs,
    run_hour_boundary_limit_home,
    run_hour_boundary_start,
)
from .plan_monthly_refresh import maybe_run_daily_month_history
from .plan_cache_merge import quarter_tick_now
from .plan_simulation import fetch_plan_inputs, hourly_plan_refresh
from .simulation import compute_balance_delta

log = logging.getLogger(__name__)

CHARGE_SPREAD_HOURS = 8

# Last quarter plan refresh outcome — GET /api/hourly-sync/status.
_last_hourly_sync: dict[str, Any] = {
    "ran_at": None,
    "plan_cache_refreshed": False,
    "plan_computed_at": None,
    "smart_mode_enabled": False,
    "next_hour": None,
    "planned_action": None,
    "error": None,
}


def get_last_hourly_sync() -> dict[str, Any]:
    return dict(_last_hourly_sync)


def _smart_mode_enabled(cfg: dict) -> bool:
    return bool(cfg.get("smart_mode_enabled", False))


async def _refresh_stored_plan(
    cfg: dict,
    *,
    now=None,
) -> dict[str, Any]:
    """Recompute Energy arbitrage plan and persist to SQLite plan_latest."""
    log.info("Plan refresh — recompute simulation, RCE, forecast, buy tariff …")
    return await hourly_plan_refresh(cfg, now=now)


async def run_daily_month_history() -> dict[str, Any]:
    """00:05 — refresh open month in SQLite and cached deposit total."""
    cfg = load_config()
    try:
        await maybe_run_daily_month_history(cfg)
        log.info("Daily month_history refresh OK")
        return {"ok": True}
    except Exception as exc:
        log.exception("Daily month_history refresh failed")
        return {"ok": False, "error": str(exc)}


async def run_nightly_forecast_cache() -> dict[str, Any]:
    """23:59 — weekday Load + Open-Meteo PV for D+1/D+2; then balance Δ."""
    cfg = load_config()
    try:
        result = await forecast_mod.run_nightly_forecast_cache(cfg)
        log.info("Nightly forecast cache OK at %s", result.get("computed_at"))
        await _balance_job()
        return {"ok": True, "computed_at": result.get("computed_at")}
    except Exception as exc:
        log.exception("Nightly job failed (forecast cache or balance Δ)")
        return {"ok": False, "error": str(exc)}


async def run_quarter_plan_refresh(*, sync_sa: bool | None = None) -> dict[str, Any]:
    """:00:02/:15:02/:31:02/:45:02 — refresh SQLite plan; SA sync when smart mode enabled."""
    del sync_sa
    global _last_hourly_sync

    from .influxdb import now_warsaw

    now = now_warsaw()
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "quarter_minute": now.minute,
        "om_pv_refreshed": False,
        "last_om_refresh": None,
        "plan_cache_refreshed": False,
        "plan_computed_at": None,
        "smart_mode_enabled": False,
        "next_hour": None,
        "planned_action": None,
        "next_hour_schedule": None,
        "error": None,
    }

    try:
        cfg = load_config()
        # Floor to the scheduled :00/:15/:30/:45 tick before long OM/sim work so
        # merge still finalizes the just-completed tick (esp. previous-hour q3).
        # The :31 cron floors to :30.
        tick_now = quarter_tick_now(now)
        next_hour = (tick_now.hour + 1) % 24
        status["smart_mode_enabled"] = _smart_mode_enabled(cfg)
        status["next_hour"] = next_hour
        status["quarter_tick"] = tick_now.strftime("%Y-%m-%d %H:%M")

        try:
            om_cache = await forecast_mod.run_hourly_pv_refresh(cfg)
            status["om_pv_refreshed"] = True
            status["last_om_refresh"] = om_cache.get("last_om_refresh")
        except Exception as exc:
            status["om_pv_refreshed"] = False
            status["last_om_refresh"] = None
            log.warning("Open-Meteo PV refresh failed: %s", exc)

        sim_result = await _refresh_stored_plan(cfg, now=tick_now)
        status["plan_cache_refreshed"] = True
        status["plan_computed_at"] = sim_result.get("computed_at")
        schedule = sim_result["next_hour_schedule"]
        status["planned_action"] = schedule.get("planned_action")
        status["next_hour_schedule"] = schedule

        if _smart_mode_enabled(cfg):
            if tick_now.minute == 0:
                status["hour_boundary"] = await run_hour_boundary_start(now=tick_now)
            elif tick_now.minute in (15, 30, 45):
                status["hour_boundary"] = await run_hour_boundary_limit_home(now=tick_now)

        _last_hourly_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        _last_hourly_sync = status
        log.exception("Quarter plan refresh failed")
        return status


async def run_hourly_plan_sync() -> dict[str, Any]:
    """Manual :00-style SA sync (hour row timer + On-grid) plus plan refresh."""
    plan_status = await run_quarter_plan_refresh()
    boundary_status = await run_hour_boundary_start()
    return {**plan_status, "hour_boundary": boundary_status}


async def _balance_job() -> None:
    """After nightly forecast cache: update grid-charge rate from energy balance Δ."""
    log.info("Balance job starting …")
    cfg = load_config()

    forecast_mod.invalidate_cache()
    rce_mod.invalidate_cache()

    forecast, metrics, _, _ = await fetch_plan_inputs(cfg)
    delta = compute_balance_delta(forecast, metrics, cfg)
    log.info(
        "Balance Δ = %.2f kWh  (PV_tomorrow=%.2f  SOC_now=%.2f  Load_tomorrow=%.2f)",
        delta,
        forecast["tomorrow"]["pv_total"],
        float(metrics.get("battery_soc", 50)) / 100 * cfg["battery"]["capacity_kwh"],
        forecast["tomorrow"]["load_total"],
    )

    if delta < 0:
        cfg["_charge_rate_kw"] = round(abs(delta) / max(CHARGE_SPREAD_HOURS, 1), 2)
        save_config(cfg)
        log.info("Charge rate set to %.2f kW", cfg["_charge_rate_kw"])
    else:
        cfg.pop("_charge_rate_kw", None)
        save_config(cfg)
        log.info("Energy balance positive — charge rate cleared")

    log.info("Balance job complete (hour boundary sync handles SA timer + work mode).")


def create_scheduler(cfg: dict) -> AsyncIOScheduler:
    del cfg
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_daily_month_history,
        trigger=CronTrigger(hour=0, minute=5, timezone="Europe/Warsaw"),
        id="daily_month_history",
        replace_existing=True,
        misfire_grace_time=300,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_nightly_forecast_cache,
        trigger=CronTrigger(hour=23, minute=59, timezone="Europe/Warsaw"),
        id="nightly_forecast_cache",
        replace_existing=True,
        misfire_grace_time=300,
        max_instances=1,
        coalesce=True,
    )
    register_hour_boundary_jobs(scheduler)
    scheduler.add_job(
        run_quarter_plan_refresh,
        trigger=CronTrigger(minute="0,15,31,45", second=2, timezone="Europe/Warsaw"),
        id="quarter_plan_refresh",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
        coalesce=True,
    )
    log.info(
        "Scheduler: month_history at 00:05; forecast cache + balance Δ at 23:59; plan refresh + SA sync at "
        ":00:02/:15:02/:31:02/:45:02 — Europe/Warsaw.",
    )
    return scheduler
