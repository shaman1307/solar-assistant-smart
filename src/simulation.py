"""
Rolling energy balance simulation (15-min optimizer, hourly display).

Plan actions minimise G12 Energa cash cost over the horizon via dynamic
programming (see plan_optimizer.py).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from .g12_pricing import get_buy_price
from .plan_cost import compute_plan_totals
from .plan_q15 import (
    collect_q15_schedule_rows,
    merge_today_hourly_profile,
    run_day_smart_q15_plan,
)
from .plan_hourly_actuals import (
    apply_current_hour_blend,
    build_completed_history_rows,
    hour_in_progress,
    hourly_profile_to_q15,
    interval_end_label,
    last_available_soc_pct,
    last_q15_soc_pct,
    resolve_day_start_soc_kwh,
)
from .plan_orchestrator import (
    assemble_ea_plan_rows,
    build_chart_plan_soc_q15,
    run_horizon_smart_plans,
    soc_q15_from_q15_by_hour,
)
from .plan_optimizer import (
    battery_export_break_even_rce,
    g12_tariff_from_cfg,
)
from .rce import quarter_rce_for_dates
from .plan_timer_override import (
    apply_plan_timer_overrides_if_any,
    get_timer_overrides_for_date,
)
from .simulation_config import get_simulation_params, plan_min_soc_pct
from .timer_plan import derive_timer_schedule_q15

Q15_PER_HOUR = 4
STEP_SCALE = 1.0 / Q15_PER_HOUR


def ea_today_end_soc_pct(plan: dict[str, Any] | None) -> float | None:
    """End-of-day SOC % from Energy Arbitrage today (history + plan rows).

    Prefers the latest hour of *today_date*; for the same hour, later rows win
    so current-hour / plan rows override completed history.
    """
    if not plan:
        return None
    today = str(plan.get("today_date") or "")
    best_h = -1
    best: float | None = None
    for r in list(plan.get("history_rows") or []) + list(plan.get("rows") or []):
        if str(r.get("plan_date") or r.get("date") or "") != today:
            continue
        try:
            h = int(r.get("hour")) if r.get("hour") is not None else -1
        except (TypeError, ValueError):
            continue
        soc = None
        q15 = r.get("q15") or []
        if q15 and q15[-1].get("soc") is not None:
            soc = float(q15[-1]["soc"])
        elif r.get("soc") is not None:
            soc = float(r["soc"])
        if soc is None:
            continue
        if h >= best_h:
            best_h = h
            best = soc
    return best


def rebuild_tomorrow_plan_soc_from_ea_end(
    plan_soc_q15: dict[str, list[float | None]],
    *,
    forecast: dict[str, Any],
    cfg: dict,
    today_date: str,
    ea_end_soc_pct: float | None,
) -> dict[str, list[float | None]]:
    """Rebuild tomorrow chart SOC seeded from live EA end-of-today SOC.

    Tomorrow stays unlocked and refreshes as EA end moves; once that calendar
    day becomes *today*, compose freezes its solid curve.
    """
    today = plan_soc_q15.get("today") or []
    if ea_end_soc_pct is None:
        return plan_soc_q15

    try:
        base = datetime.strptime(str(today_date), "%Y-%m-%d")
    except ValueError:
        return plan_soc_q15
    tomorrow_str = (base + timedelta(days=1)).strftime("%Y-%m-%d")
    pv = [float(v) for v in (forecast.get("tomorrow") or {}).get("pv") or []]
    load = [float(v) for v in (forecast.get("tomorrow") or {}).get("load") or []]
    if len(pv) < 24 or len(load) < 24:
        return plan_soc_q15

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    end_pct = max(0.0, min(100.0, float(ea_end_soc_pct)))
    seed_kwh = max(
        0.0,
        min(battery_cap, (end_pct / 100.0) * battery_cap),
    )
    quarters = quarter_rce_for_dates(tomorrow_str).get(tomorrow_str) or []
    rce_q = quarters if len(quarters) >= Q15_PER_HOUR * 24 else None
    day_plan = run_day_smart_q15_plan(
        date_str=tomorrow_str,
        pv_hourly=pv,
        load_hourly=load,
        tomorrow_pv=pv,
        tomorrow_load=load,
        cfg=cfg,
        rce_quarters=rce_q,
        initial_soc_kwh=seed_kwh,
        from_hour=0,
    )
    tomorrow_timer_ov = get_timer_overrides_for_date(cfg, tomorrow_str)
    if day_plan and tomorrow_timer_ov:
        day_plan = apply_plan_timer_overrides_if_any(
            day_plan,
            date_str=tomorrow_str,
            pv_hourly=pv,
            load_hourly=load,
            tomorrow_pv=pv,
            tomorrow_load=load,
            cfg=cfg,
            from_hour=0,
            rce_quarters=rce_q,
            gap_mode="idle",
        )
    return {"today": list(today), "tomorrow": soc_q15_from_q15_by_hour(day_plan)}

def _now_warsaw() -> datetime:
    from .influxdb import now_warsaw
    return now_warsaw()


def g12_battery_export_economics(cfg: dict) -> dict[str, float]:
    """G12 net-billing economics for battery→grid export vs self-consumption."""
    g12 = cfg["grid"]["g12"]
    offpeak = float(g12["offpeak_price_pln_kwh"])
    energy = float(g12["offpeak_energy_only_pln_kwh"])
    distribution = offpeak - energy
    tariff = g12_tariff_from_cfg(cfg)
    min_rce = battery_export_break_even_rce(tariff, cfg)
    return {
        "offpeak_full_pln": offpeak,
        "offpeak_energy_pln": energy,
        "distribution_pln": distribution,
        "min_rce_export_pln": min_rce,
        "self_use_value_pln": offpeak,
    }


def battery_export_profitable(rce_price: float | None, cfg: dict) -> bool:
    if rce_price is None:
        return False
    econ = g12_battery_export_economics(cfg)
    rce = float(rce_price)
    return rce >= econ["min_rce_export_pln"]


def _hourly_soc_kwh(
    hourly: dict[str, list] | None,
    hour: int,
    battery_cap: float,
    min_soc_pct: float,
) -> float | None:
    """End-of-hour SOC in kWh from Influx hourly accruals."""
    if not hourly or not (0 <= hour < 24):
        return None
    soc_series = hourly.get("soc") or [None] * 24
    if hour >= len(soc_series) or soc_series[hour] is None:
        return None
    pct = max(0.0, min(100.0, float(soc_series[hour])))
    return (pct / 100.0) * battery_cap


def plan_row_end_soc_kwh(row: dict[str, Any], battery_cap: float) -> float | None:
    """End-of-hour SOC (kWh) from a plan row's last q15 slot or hour soc %."""
    if battery_cap <= 0:
        return None
    q15 = row.get("q15") or []
    if q15:
        try:
            last_soc_pct = float(q15[-1].get("soc") or 0)
            return (last_soc_pct / 100.0) * battery_cap
        except (TypeError, ValueError):
            pass
    soc_pct = row.get("soc")
    if soc_pct is not None:
        try:
            return (float(soc_pct) / 100.0) * battery_cap
        except (TypeError, ValueError):
            pass
    return None


def _sqlite_current_hour_row(
    today_str: str,
    plan_from_hour: int,
) -> dict[str, Any] | None:
    """SQLite row for today's *plan_from_hour*, idle or with a timer."""
    try:
        from .sqlite_store import read_plan
        stored = read_plan()
    except Exception:
        return None
    if not stored:
        return None
    for row in stored.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        if str(row.get("plan_date") or "") != today_str:
            continue
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if hour != plan_from_hour:
            continue
        return row
    return None


def committed_current_hour_row(
    today_str: str,
    plan_from_hour: int,
) -> dict[str, Any] | None:
    """SQLite row for today's current hour (empty or Chg/Dis Timer).

    Receding horizon: the current-hour timer is already committed; DP starts at H+1
    and seeds from this row's end-of-hour SOC.
    """
    return _sqlite_current_hour_row(today_str, plan_from_hour)


def _locked_current_hour_end_soc_kwh(
    plan_from_hour: int,
    today_str: str,
    battery_cap: float,
) -> float | None:
    """SOC at end of current (locked) hour from SQLite plan_latest.

    Returns None when the stored row is not locked or not found.
    """
    try:
        from .sqlite_store import read_plan
        stored = read_plan()
        if not stored:
            return None
        rows = stored.get("rows") or []
        for row in rows:
            if (
                str(row.get("plan_date") or "") == today_str
                and int(row.get("hour", -1)) == plan_from_hour
                and row.get("hour_labels_locked")
                and row.get("start") != "TOTAL"
            ):
                return plan_row_end_soc_kwh(row, battery_cap)
    except Exception:
        pass
    return None


def resolve_plan_start_soc_kwh(
    plan_from_hour: int,
    today_hourly: dict | None,
    battery_cap: float,
    min_soc_pct: float,
    day_start_soc: float,
    live_soc_kwh: float,
) -> float:
    """SOC at the start of plan_from_hour (end of the last completed hour).

    Hour 0 uses calendar midnight seed (yesterday H23 / day_start), not live —
    live is mid-hour and would corrupt the end-of-hour SOC column on Reset.
    """
    if plan_from_hour > 0 and today_hourly:
        anchor = _hourly_soc_kwh(
            today_hourly, plan_from_hour - 1, battery_cap, min_soc_pct,
        )
        if anchor is not None:
            return anchor
    if plan_from_hour == 0:
        return float(day_start_soc)
    # No prior-hour Influx anchor: live meter as last resort.
    return live_soc_kwh


def apply_locked_hour_labels_from_plan(
    result: dict[str, Any],
    existing: dict[str, Any] | None,
    now: datetime,
    cfg: dict | None = None,
) -> None:
    """Keep the SQLite Timer Schedule for the current hour (empty, Chg, or Dis).

    Action, q15, meters, and Energy Cost stay on the fresh row.
    """
    del cfg
    today_str = now.strftime("%Y-%m-%d")
    hour = now.hour

    existing_row = None
    if existing:
        existing_row = next(
            (
                r for r in (existing.get("rows") or [])
                if r.get("start") != "TOTAL"
                and str(r.get("plan_date") or "") == today_str
                and int(r.get("hour", -1)) == hour
            ),
            None,
        )

    for row in result.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        if str(row.get("plan_date") or "") != today_str or int(row.get("hour", -1)) != hour:
            continue
        if row.get("timer_schedule_manual"):
            row["hour_labels_locked"] = True
            break
        if existing_row is not None:
            row["timer_schedule"] = existing_row.get("timer_schedule", "")
            if existing_row.get("timer_schedule_manual"):
                row["timer_schedule_manual"] = True
            row["hour_labels_locked"] = True
        elif now.minute == 0:
            row["timer_schedule"] = ""
            row["hour_labels_locked"] = True
        break


def build_energy_arbitrage_plan(
    forecast: dict[str, Any],
    live_metrics: dict[str, Any],
    rules: dict[str, Any],
    cfg: dict,
    rce_prices: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = plan_min_soc_pct(cfg)
    epsilon = float(params["epsilon_kwh"])

    pv_today = forecast["today"]["pv"]
    pv_tomorrow = forecast["tomorrow"]["pv"]
    load_today = forecast["today"]["load"]
    load_tomorrow = forecast["tomorrow"]["load"]
    pv_forecast_today = forecast["today"].get("pv_forecast") or pv_today
    load_forecast_today = forecast["today"].get("load_forecast") or load_today

    now = now or _now_warsaw()
    start_dt = now.replace(minute=0, second=0, microsecond=0)
    today_date = start_dt.date()
    today_str = start_dt.strftime("%Y-%m-%d")
    tomorrow_str = (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_str = (start_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    hour_steps = int(params["horizon_hours"])
    plan_from_hour = start_dt.hour

    rce_dates = [today_str, tomorrow_str]
    if start_dt.hour == 0:
        rce_dates.insert(0, yesterday_str)
    quarters_by_date = quarter_rce_for_dates(*rce_dates)
    tomorrow_remainder_rows = _tomorrow_remainder_rows(
        start_dt=start_dt,
        horizon_hours=hour_steps,
        today_date=today_date,
        pv_tomorrow=pv_tomorrow,
        load_tomorrow=load_tomorrow,
        quarters_by_date=quarters_by_date,
        cfg=cfg,
    )

    actual_step0 = hour_in_progress(now, start_dt)
    today_hourly = live_metrics.get("today_hourly")
    series_10min = live_metrics.get("series_10min")
    prev_day_hourly = live_metrics.get("prev_day_hourly")
    prev_day_series_10min = live_metrics.get("prev_day_series_10min")

    live_raw = live_metrics.get("battery_soc")
    if live_raw is not None:
        initial_soc_pct = max(0.0, min(100.0, float(live_raw)))
    else:
        fallback_pct = last_available_soc_pct(today_hourly, series_10min)
        if fallback_pct is None:
            fallback_pct = last_available_soc_pct(prev_day_hourly, prev_day_series_10min)
        initial_soc_pct = (
            max(0.0, min(100.0, float(fallback_pct)))
            if fallback_pct is not None
            else float(min_soc_pct)
        )
    soc_kwh = (initial_soc_pct / 100.0) * battery_cap

    # Optimizer: Influx for completed hours only; current hour blended actual+forecast.
    pv_merged, load_merged = merge_today_hourly_profile(
        pv_forecast_today,
        load_forecast_today,
        today_hourly,
        until_hour=plan_from_hour,
    )
    forecast_pv_q15 = forecast["today"].get("pv_forecast_q15") or forecast["today"].get("pv_q15")
    forecast_load_q15 = forecast["today"].get("load_q15")
    # Solid day-plan SOC seeds from last available yesterday reading (not 50%).
    day_start_soc = resolve_day_start_soc_kwh(
        battery_cap=battery_cap,
        min_soc_pct=min_soc_pct,
        live_soc_kwh=soc_kwh if live_raw is not None else None,
        today_hourly=today_hourly,
        prev_day_hourly=prev_day_hourly,
        prev_day_series_10min=prev_day_series_10min,
    )

    rce_today = quarters_by_date.get(today_str) or []
    committed_hour = committed_current_hour_row(today_str, plan_from_hour)
    committed_end_soc = (
        plan_row_end_soc_kwh(committed_hour, battery_cap)
        if committed_hour is not None
        else None
    )
    if committed_hour is not None and committed_end_soc is None:
        committed_end_soc = float(soc_kwh)
    hours_today_in_plan = max(0, 24 - plan_from_hour)
    need_tomorrow_hours = max(0, hour_steps - hours_today_in_plan)
    rce_today_full = rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None
    rce_tomorrow_list = quarters_by_date.get(tomorrow_str) or []
    rce_tomorrow_full = (
        rce_tomorrow_list if len(rce_tomorrow_list) >= Q15_PER_HOUR * 24 else None
    )

    smart_today, smart_tomorrow = run_horizon_smart_plans(
        today_str=today_str,
        tomorrow_str=tomorrow_str,
        pv_merged=pv_merged,
        load_merged=load_merged,
        pv_tomorrow=pv_tomorrow,
        load_tomorrow=load_tomorrow,
        cfg=cfg,
        rce_today_full=rce_today_full,
        rce_tomorrow_full=rce_tomorrow_full,
        committed_hour=committed_hour,
        committed_end_soc=committed_end_soc,
        plan_from_hour=plan_from_hour,
        hour_steps=hour_steps,
        need_tomorrow_hours=need_tomorrow_hours,
        soc_kwh=soc_kwh,
        day_start_soc=day_start_soc,
        epsilon=epsilon,
    )

    if 0 <= plan_from_hour < 24:
        pv_merged, load_merged = apply_current_hour_blend(
            pv_merged,
            load_merged,
            plan_from_hour,
            now,
            forecast_pv_q15=forecast_pv_q15,
            forecast_load_q15=forecast_load_q15,
            series_10min=series_10min,
        )

    merged_pv_q15_today = hourly_profile_to_q15(pv_merged)
    merged_load_q15_today = hourly_profile_to_q15(load_merged)
    merged_pv_q15_tomorrow = hourly_profile_to_q15([float(v) for v in pv_tomorrow])
    merged_load_q15_tomorrow = hourly_profile_to_q15([float(v) for v in load_tomorrow])

    plan_start_soc_kwh = resolve_plan_start_soc_kwh(
        plan_from_hour,
        today_hourly,
        battery_cap,
        min_soc_pct,
        day_start_soc,
        soc_kwh,
    )

    today_timer_ov = get_timer_overrides_for_date(cfg, today_str)
    tomorrow_timer_ov = get_timer_overrides_for_date(cfg, tomorrow_str)

    if smart_today and today_timer_ov:
        ov_from = plan_from_hour + 1 if committed_hour is not None else plan_from_hour
        smart_today = apply_plan_timer_overrides_if_any(
            smart_today,
            date_str=today_str,
            pv_hourly=pv_merged,
            load_hourly=load_merged,
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            from_hour=ov_from,
            rce_quarters=rce_today_full,
        )

    if smart_tomorrow and tomorrow_timer_ov:
        smart_tomorrow = apply_plan_timer_overrides_if_any(
            smart_tomorrow,
            date_str=tomorrow_str,
            pv_hourly=[float(v) for v in pv_tomorrow],
            load_hourly=[float(v) for v in load_tomorrow],
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            from_hour=0,
            rce_quarters=rce_tomorrow_full,
        )

    all_rows, export_hours = assemble_ea_plan_rows(
        smart_today=smart_today,
        smart_tomorrow=smart_tomorrow,
        committed_hour=committed_hour,
        committed_end_soc=committed_end_soc,
        plan_from_hour=plan_from_hour,
        hour_steps=hour_steps,
        today_str=today_str,
        tomorrow_str=tomorrow_str,
        today_date=today_date,
        now=now,
        pv_merged=pv_merged,
        load_merged=load_merged,
        pv_tomorrow=pv_tomorrow,
        load_tomorrow=load_tomorrow,
        forecast_pv_q15=forecast_pv_q15,
        forecast_load_q15=forecast_load_q15,
        series_10min=series_10min,
        live_metrics=live_metrics,
        quarters_by_date=quarters_by_date,
        cfg=cfg,
        params=params,
        rules=rules,
        epsilon=epsilon,
        battery_cap=battery_cap,
        plan_start_soc_kwh=plan_start_soc_kwh,
        initial_soc_pct=initial_soc_pct,
        today_timer_ov=today_timer_ov,
        tomorrow_timer_ov=tomorrow_timer_ov,
        merged_pv_q15_today=merged_pv_q15_today,
        merged_load_q15_today=merged_load_q15_today,
        merged_pv_q15_tomorrow=merged_pv_q15_tomorrow,
        merged_load_q15_tomorrow=merged_load_q15_tomorrow,
    )

    today_plan_soc, tomorrow_plan_soc = build_chart_plan_soc_q15(
        today_str=today_str,
        tomorrow_str=tomorrow_str,
        pv_forecast_today=pv_forecast_today,
        load_forecast_today=load_forecast_today,
        pv_tomorrow=pv_tomorrow,
        load_tomorrow=load_tomorrow,
        cfg=cfg,
        rce_today=rce_today,
        rce_tomorrow_full=rce_tomorrow_full,
        day_start_soc=day_start_soc,
        today_timer_ov=today_timer_ov,
        tomorrow_timer_ov=tomorrow_timer_ov,
        all_rows=all_rows,
        smart_today=smart_today,
        battery_cap=battery_cap,
    )


    schedule_from = (
        now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        if actual_step0 else start_dt
    )
    q15_schedule_rows = collect_q15_schedule_rows(
        smart_today=smart_today,
        smart_tomorrow=smart_tomorrow,
        today_str=today_str,
        tomorrow_str=tomorrow_str,
        from_dt=schedule_from,
    )
    timer_schedule = derive_timer_schedule_q15(q15_schedule_rows, cfg, rules)

    history_rows: list[dict] = []
    if today_hourly:
        history_rows = build_completed_history_rows(
            today_str,
            plan_from_hour,
            today_hourly,
            quarters_by_date,
            cfg,
            params,
        )
        # Seed only for empty/new-day SQLite. Same-day history is attached later
        # via attach_immutable_history and never overwritten on rebuild.

    rows = all_rows
    today_plan_rows = [r for r in all_rows if r.get("plan_date") == today_str]
    # TOTAL = completed actuals + remaining today plan (matches visible today rows).
    today_totals = compute_plan_totals(history_rows + today_plan_rows)
    delta = compute_balance_delta(forecast, live_metrics, cfg)

    return {
        "rows": rows,
        "history_rows": history_rows,
        "has_history_rows": bool(history_rows),
        "plan_from_hour": plan_from_hour,
        "live_soc_pct": round(initial_soc_pct, 1),
        "battery_capacity_kwh": battery_cap,
        "today_date": today_str,
        "plan_soc_q15": {
            "today": today_plan_soc,
            "tomorrow": tomorrow_plan_soc,
        },
        # Candidate for lock; plan_simulation freezes today once locked.
        "plan_soc_day_locked": False,
        "totals": today_totals,
        "tomorrow_remainder_rows": tomorrow_remainder_rows,
        "has_tomorrow_remainder": bool(tomorrow_remainder_rows),
        "delta_kwh": delta,
        "plan_charge": delta < 0,
        "plan_export_hours": sorted(export_hours),
        "forecast_tomorrow": {
            "pv_total": round(float(forecast["tomorrow"]["pv_total"]), 2),
            "load_total": round(float(forecast["tomorrow"]["load_total"]), 2),
            "balance_kwh": round(
                float(forecast["tomorrow"]["pv_total"]) - float(forecast["tomorrow"]["load_total"]),
                2,
            ),
        },
        "proposed_schedule": timer_schedule,
        "g12_tariff_name": cfg["grid"]["g12"].get("tariff_name", "G12"),
    }


def run_simulation(
    forecast: dict[str, Any],
    live_metrics: dict[str, Any],
    rules: dict[str, Any],
    cfg: dict,
    rce_prices: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rolling energy arbitrage plan (same core as Rules / Debug smart today)."""
    return build_energy_arbitrage_plan(
        forecast, live_metrics, rules, cfg, rce_prices=rce_prices, now=now,
    )


def compute_balance_delta(
    forecast: dict[str, Any],
    live_metrics: dict[str, Any],
    cfg: dict,
) -> float:
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    soc_pct = float(live_metrics.get("battery_soc", 50.0))
    soc_kwh = (soc_pct / 100.0) * battery_cap

    overrides = cfg.get("overrides", {})
    pv_tomorrow = (
        float(overrides["tomorrow_pv_kwh"])
        if overrides.get("tomorrow_pv_kwh") is not None
        else forecast["tomorrow"]["pv_total"]
    )
    load_tomorrow = (
        float(overrides["tomorrow_load_kwh"])
        if overrides.get("tomorrow_load_kwh") is not None
        else forecast["tomorrow"]["load_total"]
    )
    return round((pv_tomorrow + soc_kwh) - load_tomorrow, 3)


compute_nightly_delta = compute_balance_delta


def _tomorrow_remainder_rows(
    *,
    start_dt: datetime,
    horizon_hours: int,
    today_date,
    pv_tomorrow: list[float],
    load_tomorrow: list[float],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
) -> list[dict[str, Any]]:
    """Rows for tomorrow after the rolling 24h plan window.

    UI affordance only: PV/Load + prices for hours past the plan horizon.
    Those hours were used as forecast lookahead for reserve/charge-target, but
    are not simulated as EA plan rows (no flows, SOC, cost, or actions).
    """
    tomorrow_date = (start_dt + timedelta(days=1)).date()
    tomorrow_str = (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    last_dt = start_dt + timedelta(hours=max(0, horizon_hours - 1))
    if last_dt.date() > tomorrow_date:
        return []

    if last_dt.date() == tomorrow_date:
        start_hour = last_dt.hour + 1
    else:
        start_hour = 0

    if start_hour > 23:
        return []

    quarters = quarters_by_date.get(tomorrow_str) or []
    rows: list[dict[str, Any]] = []
    for h in range(start_hour, 24):
        dt = datetime.strptime(tomorrow_str, "%Y-%m-%d").replace(hour=h)
        pv_h = float(pv_tomorrow[h]) if h < len(pv_tomorrow) else 0.0
        load_h = float(load_tomorrow[h]) if h < len(load_tomorrow) else 0.0
        buy_price, g12_zone = get_buy_price(dt, cfg)
        chunk = quarters[h * Q15_PER_HOUR:(h + 1) * Q15_PER_HOUR]
        vals = [float(v) for v in chunk if v is not None]
        rce_price = round(sum(vals) / len(vals), 4) if vals else None
        rce_q15 = list(chunk) if chunk else [None] * Q15_PER_HOUR
        while len(rce_q15) < Q15_PER_HOUR:
            rce_q15.append(None)

        rows.append(
            {
                "hour": h,
                "plan_date": tomorrow_str,
                "start": interval_end_label(dt),
                "production": round(pv_h, 3),
                "consumption": round(load_h, 3),
                "battery": None,
                "bat_charge": None,
                "bat_discharge": None,
                "grid_import": None,
                "grid_export": None,
                "soc": None,
                "import_cost": None,
                "export_revenue": None,
                "energy_cost": None,
                "service_cost": None,
                "cost": None,
                "action": "",
                "timer_schedule": "",
                "rce_price": rce_price,
                "rce_q15": rce_q15,
                "export_credit": None,
                "g12_zone": g12_zone,
                "buy_price": round(buy_price, 4),
                "export_planned": False,
                "uncalculated": True,
            }
        )
    return rows
