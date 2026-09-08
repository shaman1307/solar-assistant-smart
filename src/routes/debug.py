"""Inverter physics debug API — sim vs actual hourly replay."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter

from .. import influxdb as influxdb_mod
from .. import rce as rce_mod
from .. import sa_client
from ..config import load_config
from .. import forecast as forecast_mod
from ..debug_plan import merge_ea_plan_into_debug_day
from ..plan_q15 import (
    apply_smart_plan_for_day,
    hourly_rows_from_pv_load,
    merge_today_hourly_profile,
)
from ..g12_pricing import get_buy_price
from ..inverter_sim import simulate_day_from_profile
from ..plan_cache_merge import attach_immutable_history
from ..plan_simulation import fetch_plan_inputs
from ..simulation import (
    apply_locked_hour_labels_from_plan,
    build_energy_arbitrage_plan,
)
from ..simulation_config import merge_simulation_defaults, plan_min_soc_pct
from ..sqlite_store import read_plan
from ..app_logging import list_log_dates, parse_log_date, read_log_day

router = APIRouter()


def _next_date(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _enrich_rows(
    rows: list[dict[str, Any]],
    date_str: str,
    rce_quarter_by_date: dict[str, list[float | None]],
    cfg: dict,
) -> list[dict[str, Any]]:
    quarters = rce_quarter_by_date.get(date_str) or [None] * 96
    base = datetime.strptime(date_str, "%Y-%m-%d")
    out: list[dict[str, Any]] = []
    for row in rows:
        h = row.get("hour")
        buy_price: float | None = None
        rce_q15: list[float | None] = [None] * 4
        if h is not None and 0 <= int(h) < 24:
            hi = int(h)
            dt = base.replace(hour=hi)
            buy_price = round(get_buy_price(dt, cfg)[0], 4)
            rce_q15 = list(quarters[hi * 4:(hi + 1) * 4])
        out.append({
            **row,
            "date": date_str,
            "buy_price": buy_price,
            "rce_q15": rce_q15,
        })
    return out


def _day_payload(
    date_str: str,
    sim: dict[str, Any],
    rce_quarter_by_date: dict[str, list[float | None]],
    cfg: dict,
) -> dict[str, Any]:
    rows = _enrich_rows(sim.get("rows") or [], date_str, rce_quarter_by_date, cfg)
    return {"date": date_str, **sim, "rows": rows}


async def _apply_ea_smart_for_today(
    days: list[dict[str, Any]],
    *,
    date_str: str,
    next_date: str,
    cfg: dict,
) -> None:
    """Smart column via the same pipeline as Energy arbitrage (with SQLite lock)."""
    cfg = merge_simulation_defaults(cfg)
    forecast, metrics, rules, rce_prices = await fetch_plan_inputs(cfg, invalidate=False)
    existing_plan = read_plan()
    ea_plan = await asyncio.to_thread(
        build_energy_arbitrage_plan,
        forecast,
        metrics,
        rules,
        cfg,
        rce_prices=rce_prices,
    )
    now = influxdb_mod.now_warsaw()
    apply_locked_hour_labels_from_plan(ea_plan, existing_plan, now, cfg=cfg)
    attach_immutable_history(ea_plan, existing_plan, now=now)
    merge_ea_plan_into_debug_day(days[0], ea_plan, date_str)
    if len(days) > 1:
        merge_ea_plan_into_debug_day(days[1], ea_plan, next_date)


@router.get("/api/inverter-debug")
async def api_inverter_debug(
    date: str | None = None,
    initial_soc_pct: float | None = None,
) -> dict[str, Any]:
    """Replay hourly PV/load with load-priority physics; compare to Influx accruals."""
    if not date:
        date = influxdb_mod.now_warsaw().strftime("%Y-%m-%d")

    cfg = load_config()
    accruals = await influxdb_mod.get_accruals_for_date(date)
    if accruals.get("error"):
        return {"error": accruals["error"], "date": date}

    # Today: completed hours from Influx; current hour and later from forecast.
    today_str = influxdb_mod.now_warsaw().strftime("%Y-%m-%d")
    hourly = accruals.get("hourly") or {}
    plan_from_hour: int | None = None
    live_soc_kwh: float | None = None
    if date == today_str:
        now = influxdb_mod.now_warsaw()
        plan_from_hour = now.replace(minute=0, second=0, microsecond=0).hour
        fc_day = await forecast_mod.get_horizon_day_profile(date, cfg)
        pv, load = merge_today_hourly_profile(
            fc_day["pv"],
            fc_day["load"],
            hourly,
            until_hour=plan_from_hour,
        )
        hourly = dict(hourly)
        hourly["pv"] = pv
        hourly["load"] = load
        metrics = await sa_client.get_live_metrics(cfg)
        battery_cap = float(cfg["battery"]["capacity_kwh"])
        min_soc = plan_min_soc_pct(cfg)
        live_soc_kwh = (
            max(min_soc, min(100.0, float(metrics.get("battery_soc", 50.0))))
            / 100.0
            * battery_cap
        )

    day1 = simulate_day_from_profile(
        hourly,
        initial_soc_pct=initial_soc_pct,
    )

    next_date = _next_date(date)
    forecast_date = _next_date(next_date)
    rce_quarter_by_date = await rce_mod.get_quarter_rce_for_dates(date, next_date, forecast_date)

    day1_payload = _day_payload(date, day1, rce_quarter_by_date, cfg)
    days: list[dict[str, Any]] = [day1_payload]

    accruals_next = await influxdb_mod.get_accruals_for_date(next_date)
    day2_rows: list[dict[str, Any]] = []
    hourly_next = accruals_next.get("hourly") if isinstance(accruals_next, dict) else None
    has_influx_next = (
        not accruals_next.get("error")
        and isinstance(hourly_next, dict)
        and any(v is not None for v in (hourly_next.get("pv") or []))
        and any(v is not None for v in (hourly_next.get("load") or []))
    )

    if has_influx_next:
        day2 = simulate_day_from_profile(
            hourly_next or {},
            initial_soc_kwh=day1.get("end_soc_kwh"),
        )
    else:
        next_fc = await forecast_mod.get_horizon_day_profile(next_date, cfg)
        day2 = simulate_day_from_profile(
            {"pv": next_fc["pv"], "load": next_fc["load"]},
            initial_soc_kwh=day1.get("end_soc_kwh"),
        )

    days.append(_day_payload(next_date, day2, rce_quarter_by_date, cfg))
    day2_rows = days[1]["rows"]

    horizon_day = await forecast_mod.get_horizon_day_profile(forecast_date, cfg)
    forecast = {
        "date": horizon_day["date"],
        "pv_total": horizon_day["pv_total"],
        "load_total": horizon_day["load_total"],
        "source": horizon_day["source"],
    }
    day3_rows = hourly_rows_from_pv_load(horizon_day["pv"], horizon_day["load"])

    if date == today_str:
        await _apply_ea_smart_for_today(
            days,
            date_str=date,
            next_date=next_date,
            cfg=cfg,
        )
    else:
        end_soc = apply_smart_plan_for_day(
            day1_payload,
            day2_rows,
            date,
            cfg,
            rce_quarters=rce_quarter_by_date.get(date),
            plan_from_hour=plan_from_hour,
            live_soc_kwh=live_soc_kwh,
        )
        if len(days) > 1:
            apply_smart_plan_for_day(
                days[1],
                day3_rows,
                next_date,
                cfg,
                rce_quarters=rce_quarter_by_date.get(next_date),
                initial_soc_kwh=end_soc,
            )

    return {
        "date": date,
        "date_next": next_date,
        "date_forecast": forecast_date,
        "mode": "load_priority",
        "rules": False,
        "forecast": forecast,
        "days": days,
        **{k: v for k, v in day1_payload.items() if k != "date"},
    }


@router.get("/api/debug/logs/days")
async def api_debug_log_days() -> dict[str, Any]:
    """List dates that have a daily log file (newest first)."""
    days = list_log_dates()
    return {"days": days, "default": days[0] if days else None}


@router.get("/api/debug/logs")
async def api_debug_log(date: str = "") -> dict[str, Any]:
    """Return text of one daily log file under logs/YYYY/MM/."""
    if not date.strip():
        days = list_log_dates(limit=1)
        date = days[0] if days else ""
    if parse_log_date(date) is None:
        return {
            "date": date,
            "exists": False,
            "error": "invalid_date",
            "text": "",
            "truncated": False,
            "path": None,
        }
    return read_log_day(date)
