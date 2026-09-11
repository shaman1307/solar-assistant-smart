"""Data API: metrics, forecast, RCE, buy tariff, simulation."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from .. import forecast as forecast_mod
from .. import influxdb as influxdb_mod
from .. import rce as rce_mod
from .. import sa_client
from ..config import load_config, save_config
from ..influxdb import now_warsaw
from ..plan_deposits import open_month_id, run_deposit_cascade
from ..plan_monthly_history import build_month_history
from ..plan_monthly_refresh import (
    ensure_deposit_total_current,
    maybe_run_daily_month_history,
    rebuild_all_month_history,
)
from ..sqlite_store import (
    load_all_deposits,
    load_month_history,
    read_cached_deposit_total,
    read_plan,
    read_plan_buy_tariff,
    read_plan_rce,
    save_month_history,
)
from ..plan_simulation import (
    build_buy_tariff_payload,
    build_plan_simulation,
    extract_actual_soc_q15,
    get_simulation_for_date,
    hourly_plan_refresh,
)
from ..plan_timer_override import is_timer_schedule_hour_editable, set_timer_schedule_override
from ..timer_plan import parse_timer_schedule_segments
from .. import forecast_cache as fc

router = APIRouter()

_SA_DAILY_ENERGY_KEYS = (
    "pv_energy_today",
    "load_energy_today",
    "grid_buy_energy",
    "grid_sell_energy",
    "battery_charged_today",
    "battery_discharged_today",
)


def _deposit_total_payload() -> dict[str, Any]:
    cached = read_cached_deposit_total()
    if cached is None:
        return {"deposit_total": None, "as_of_month": None, "updated_at": None}
    return dict(cached)


_DEPOSIT_CHART_MAX_MONTHS = 24


@router.get("/api/history-deposit-total")
async def api_history_deposit_total() -> dict[str, Any]:
    """Cached deposit pool total — updated daily or via rebuild, not on month pick."""
    cfg = load_config()
    await maybe_run_daily_month_history(cfg)
    total = await ensure_deposit_total_current(cfg)
    out = _deposit_total_payload()
    out["deposit_total"] = total
    deposits = load_all_deposits()
    month_rows = [
        {
            "month": month_id,
            "credited": round(float(row["initial"]), 4),
            "remaining": round(float(row["current"]), 4),
        }
        for month_id, row in sorted(deposits.items())
    ]
    out["months"] = month_rows[-_DEPOSIT_CHART_MAX_MONTHS:]
    return out


@router.post("/api/history-rebuild")
async def api_history_rebuild() -> dict[str, Any]:
    """Full month_history rebuild from Influx + deposit cascade (after manual SQLite edits)."""
    cfg = load_config()
    return await rebuild_all_month_history(cfg)


@router.get("/api/live")
async def api_live() -> dict[str, Any]:
    """Live System powers / SOC from SA only (no Influx wait)."""
    cfg = load_config()
    return await sa_client.get_live_metrics(cfg)


@router.get("/api/metrics")
async def api_metrics() -> dict[str, Any]:
    """Combined live + today accruals (compat). Prefer /api/live + /api/accruals for UI."""
    cfg = load_config()
    live, accruals = await asyncio.gather(
        sa_client.get_live_metrics(cfg),
        influxdb_mod.get_accruals(),
    )
    if live.get("sa_online"):
        for key in _SA_DAILY_ENERGY_KEYS:
            if key in live:
                accruals[key] = live[key]
    live.update(accruals)
    return live


@router.get("/api/forecast")
async def api_forecast() -> dict[str, Any]:
    """Forecast today/tomorrow from day cache; actuals for charts from Influx."""
    cfg = load_config()
    today_str = now_warsaw().strftime("%Y-%m-%d")
    day_accruals = await influxdb_mod.get_accruals_for_date(today_str)
    hourly = (day_accruals.get("hourly") or {}) if isinstance(day_accruals, dict) else {}
    forecast = await forecast_mod.get_forecast(
        cfg, today_pv_actual=hourly.get("pv"),
    )
    if isinstance(day_accruals, dict):
        forecast["series_10min"] = day_accruals.get("series_10min")
        series_10 = day_accruals.get("series_10min") or {}
        if series_10.get("pv"):
            forecast["pv_actual_q15"] = fc.ten_min_kw_to_q15_kw(series_10["pv"])
            forecast["pv_actual_q15_is_kw"] = True
        if series_10.get("load"):
            forecast["load_actual_q15"] = fc.ten_min_kw_to_q15_kw(series_10["load"])
            forecast["load_actual_q15_is_kw"] = True
    if hourly.get("pv") and "pv_actual_q15" not in forecast:
        forecast["pv_actual_q15"] = fc.hourly_actual_to_q15(hourly.get("pv") or [])
        forecast["pv_actual_q15_is_kw"] = False
    if hourly.get("load") and "load_actual_q15" not in forecast:
        forecast["load_actual_q15"] = fc.hourly_actual_to_q15(hourly.get("load") or [])
        forecast["load_actual_q15_is_kw"] = False
    forecast["load_actual_hourly"] = hourly.get("load")
    forecast["pv_actual_hourly"] = hourly.get("pv")
    plan = read_plan() or {}
    plan_soc = plan.get("plan_soc_q15")
    if isinstance(plan_soc, dict) and (
        (isinstance(plan_soc.get("today"), list) and any(v is not None for v in plan_soc["today"]))
        or (isinstance(plan_soc.get("tomorrow"), list) and any(v is not None for v in plan_soc["tomorrow"]))
    ):
        forecast["plan_soc_q15"] = plan_soc
    else:
        # Empty plan_soc_q15: leave blanks; EA-blended SOC lives in actual_soc_q15.
        forecast["plan_soc_q15"] = {"today": [None] * 96, "tomorrow": [None] * 96}
    forecast["actual_soc_q15"] = extract_actual_soc_q15(plan)
    return forecast


@router.get("/api/rce")
async def api_rce() -> dict[str, Any]:
    """Return hourly RCE prices for today and tomorrow from PSE."""
    stored_rce = read_plan_rce()
    if stored_rce is not None:
        return rce_mod.prepare_rce_payload(dict(stored_rce))
    return await rce_mod.get_rce_prices()


@router.get("/api/buy-tariff")
async def api_buy_tariff() -> dict[str, Any]:
    """Return rolling hourly buy-tariff prices from config (G12 zones)."""
    stored_tariff = read_plan_buy_tariff()
    if stored_tariff is not None:
        return stored_tariff
    return build_buy_tariff_payload(load_config())


@router.get("/api/history-month")
async def api_history_month(month: str, refresh: bool = False) -> dict[str, Any]:
    """Daily history totals for a calendar month (YYYY-MM). SQLite-backed; no cascade on read."""
    if refresh:
        cfg = load_config()
        built = await build_month_history(month, cfg)
        if built.get("error"):
            return built
        save_month_history(month, built)
        open_month = open_month_id()
        open_payload = load_month_history(open_month)
        if open_payload is None:
            open_payload = await build_month_history(open_month, cfg)
        if not open_payload.get("error"):
            run_deposit_cascade(open_month, open_payload)
        cached = load_month_history(month)
        return cached if cached is not None else built

    cached = load_month_history(month)
    if cached is not None:
        cached.pop("_cached_at", None)
        return cached

    cfg = load_config()
    built = await build_month_history(month, cfg)
    if built.get("error"):
        return built
    save_month_history(month, built)
    return built


@router.get("/api/simulation")
async def api_simulation(
    refresh: bool = False,
    unlock_plan_soc: bool = False,
    date: str | None = None,
) -> dict[str, Any]:
    """Return EA plan for today, or a past day history view.

    ``?refresh=1`` — same path as :00/:15/:30/:45 (today only).
    ``?refresh=1&unlock_plan_soc=1`` — also rebuild the as-if-00:00 solid SOC
    curve (Forecast Overrides Refresh).
    ``?date=YYYY-MM-DD`` — past day from archive (timers) or Influx rebuild.
    """
    cfg = load_config()
    return await get_simulation_for_date(
        cfg, date, refresh=refresh, unlock_plan_soc=unlock_plan_soc,
    )


@router.post("/api/plan/timer-schedule")
async def api_set_plan_timer_schedule(body: dict[str, Any]) -> dict[str, Any]:
    """Save manual Timer Schedule cell; replay from that hour onward."""
    plan_date = str(body.get("plan_date") or "").strip()
    if not plan_date or len(plan_date) != 10:
        return {"ok": False, "error": "plan_date required (YYYY-MM-DD)"}
    try:
        hour = int(body["hour"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "hour required (0-23)"}
    if not (0 <= hour <= 23):
        return {"ok": False, "error": "hour must be 0-23"}

    now = now_warsaw()
    today_date = now.strftime("%Y-%m-%d")
    if not is_timer_schedule_hour_editable(
        plan_date, hour, today_date=today_date, plan_from_hour=now.hour,
    ):
        return {"ok": False, "error": "Only future plan hours can be edited"}

    reset = bool(body.get("reset"))
    timer_raw = body.get("timer_schedule")
    if reset:
        timer_schedule: str | None = None
    elif timer_raw is None:
        timer_schedule = ""
    else:
        timer_schedule = str(timer_raw).strip()

    if timer_schedule:
        if not parse_timer_schedule_segments(timer_schedule):
            return {
                "ok": False,
                "error": "Invalid timer format. Example: Chg 14:00-15:00 5kW cap80%",
            }

    cfg = load_config()
    set_timer_schedule_override(cfg, plan_date, hour, timer_schedule)
    save_config(cfg)
    plan = await hourly_plan_refresh(cfg, unlock_plan_soc=True)
    return {"ok": True, "plan_date": plan_date, "hour": hour, "timer_schedule": timer_schedule, "plan": plan}


@router.get("/api/accruals")
async def api_accruals(date: str | None = None) -> dict[str, Any]:
    """Return daily totals + hourly breakdown for any Warsaw day (default: today)."""
    if not date:
        date = now_warsaw().strftime("%Y-%m-%d")
    return await influxdb_mod.get_accruals_for_date(date)
