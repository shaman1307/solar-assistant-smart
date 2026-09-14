"""
Plan Simulation — Energy arbitrage pipeline; SQLite plan_latest is the only store.

Refreshed every 15 minutes (scheduler :00/:15/:30/:45 Warsaw), regardless of
smart_mode_enabled, with Load/PV forecast, Influx actuals for completed
quarters, and PSE RCE embedded in the persisted plan payload.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from . import forecast as forecast_mod
from . import influxdb as influxdb_mod
from . import rce as rce_mod
from . import sa_client
from .influxdb import now_warsaw
from .plan_hourly_actuals import interval_end_label
from .plan_cache_merge import (
    attach_immutable_history,
    merge_incremental_plan,
    plan_needs_full_rebuild,
)
from .simulation import (
    apply_locked_hour_labels_from_plan,
    ea_today_end_soc_pct,
    rebuild_tomorrow_plan_soc_from_ea_end,
    run_simulation,
)
from .simulation_config import (
    get_simulation_params,
    merge_simulation_defaults,
    plan_min_soc_pct,
)
from .sqlite_store import (
    list_plan_day_archives,
    load_plan_day_archive,
    read_plan,
    replace_plan_latest_payload,
    save_plan_day_archive,
    write_plan,
)
from .timer_plan import build_hourly_schedule
from .plan_cost import compute_plan_totals
from .plan_hourly_actuals import (
    backfill_history_rows_rce,
    build_completed_history_rows,
    history_rows_have_rce_holes,
    overlay_meter_soc_on_rows,
    reprice_history_rows_to_current_g12,
)

log = logging.getLogger(__name__)

_plan_lock: asyncio.Lock | None = None


def _get_plan_lock() -> asyncio.Lock:
    global _plan_lock
    if _plan_lock is None:
        _plan_lock = asyncio.Lock()
    return _plan_lock


def _overlay_meter_soc_on_plan(
    plan: dict[str, Any],
    metrics: dict[str, Any] | None,
    now,
) -> None:
    """Replace EA SOC with Influx/inverter readings (may be below plan min)."""
    today_str = now.strftime("%Y-%m-%d")
    hourly = (metrics or {}).get("today_hourly")
    series = (metrics or {}).get("series_10min")
    overlay_meter_soc_on_rows(
        plan.get("history_rows"),
        today_str=today_str,
        today_hourly=hourly,
        series_10min=series,
        current_hour=now.hour,
    )
    overlay_meter_soc_on_rows(
        plan.get("rows"),
        today_str=today_str,
        today_hourly=hourly,
        series_10min=series,
        current_hour=now.hour,
    )


async def _metrics_for_soc_overlay() -> dict[str, Any]:
    """Influx day payload for SOC overlay on a cached plan GET."""
    today_str = now_warsaw().strftime("%Y-%m-%d")
    try:
        day = await influxdb_mod.get_accruals_for_date(today_str)
    except Exception:
        log.warning("Meter SOC overlay — Influx fetch failed", exc_info=True)
        return {}
    if not isinstance(day, dict):
        return {}
    out: dict[str, Any] = {}
    if day.get("hourly"):
        out["today_hourly"] = day["hourly"]
    if day.get("series_10min"):
        out["series_10min"] = day["series_10min"]
    return out


def extract_plan_soc_hourly(plan: dict[str, Any] | None) -> dict[str, list[float | None]]:
    """Hourly planned/actual SOC from Energy arbitrage plan (today + tomorrow)."""
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    out: dict[str, list[float | None]] = {
        "today": [None] * 24,
        "tomorrow": [None] * 24,
    }
    if not plan:
        return out

    def _date_key(row: dict) -> str | None:
        return row.get("plan_date") or row.get("date")

    for row in (plan.get("history_rows") or []) + (plan.get("rows") or []):
        date_key = _date_key(row)
        label = None
        if date_key == today_str:
            label = "today"
        elif date_key == tomorrow_str:
            label = "tomorrow"
        if label is None:
            continue
        h = row.get("hour")
        if h is None or not (0 <= int(h) < 24):
            continue
        soc = row.get("soc")
        if soc is not None:
            out[label][int(h)] = round(float(soc), 1)
    return out


def _normalize_soc_q15(raw: Any) -> list[float | None]:
    """Pad/truncate to 96 slots; round numeric values."""
    if not isinstance(raw, list):
        return [None] * 96
    out: list[float | None] = []
    for v in list(raw)[:96]:
        out.append(round(float(v), 1) if v is not None else None)
    while len(out) < 96:
        out.append(None)
    return out


def _q15_index(now) -> int:
    """Index 0..95 for the current 15-minute slot (Warsaw wall clock)."""
    return int(now.hour) * 4 + int(now.minute) // 15


def normalize_plan_soc_q15(plan: dict[str, Any] | None) -> dict[str, list[float | None]]:
    """Pad simulator plan_soc_q15 to 96 slots per day (no EA stitching)."""
    bundle = (plan or {}).get("plan_soc_q15") or {}
    if not isinstance(bundle, dict):
        bundle = {}
    return {
        "today": _normalize_soc_q15(bundle.get("today")),
        "tomorrow": _normalize_soc_q15(bundle.get("tomorrow")),
    }


def compose_plan_soc_q15(
    existing: dict[str, Any] | None,
    fresh: dict[str, Any] | None,
    *,
    refresh: bool = False,
) -> dict[str, list[float | None]]:
    """Solid chart SOC: freeze *today* once locked; keep *tomorrow* fresh.

    Today: as-if-00:00 day run, frozen while *plan_soc_day_locked* for the same
    calendar day (unless *refresh*). Tomorrow: always the fresh simulator curve
    (seeded from live EA end-of-today); it keeps moving until that day becomes
    today and then freezes under the today lock.
    """
    old_bundle = (existing or {}).get("plan_soc_q15") or {}
    new_bundle = (fresh or {}).get("plan_soc_q15") or {}
    if not isinstance(old_bundle, dict):
        old_bundle = {}
    if not isinstance(new_bundle, dict):
        new_bundle = {}

    old_today = _normalize_soc_q15(old_bundle.get("today"))
    new_today = _normalize_soc_q15(new_bundle.get("today"))
    same_day = (
        existing is not None
        and str(existing.get("today_date") or "")
        == str((fresh or {}).get("today_date") or "")
    )
    locked = bool(
        same_day
        and not refresh
        and existing.get("plan_soc_day_locked")
        and any(v is not None for v in old_today)
    )

    today = old_today if locked else new_today
    new_tom = _normalize_soc_q15(new_bundle.get("tomorrow"))
    old_tom = _normalize_soc_q15(old_bundle.get("tomorrow"))
    tomorrow = new_tom if any(v is not None for v in new_tom) else old_tom
    return {"today": today, "tomorrow": tomorrow}


def extract_actual_soc_q15(
    plan: dict[str, Any] | None,
    now=None,
) -> dict[str, list[float | None]]:
    """Today q15 SOC from completed EA history / current hour only (not future plan)."""
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    max_idx = _q15_index(now)
    today_slots: list[float | None] = [None] * 96
    if not plan:
        return {"today": today_slots, "tomorrow": [None] * 96}

    def _fill_row(row: dict, *, hour_cap: int | None = None) -> None:
        h = row.get("hour")
        if h is None or not (0 <= int(h) < 24):
            return
        hour = int(h)
        if hour_cap is not None and hour > hour_cap:
            return
        for slot in row.get("q15") or []:
            q = int(slot.get("quarter", 0))
            idx = hour * 4 + q
            if idx > max_idx:
                continue
            soc = slot.get("soc")
            if 0 <= idx < 96 and soc is not None:
                today_slots[idx] = round(float(soc), 1)

    # Completed hours — meter / blended fact.
    for row in plan.get("history_rows") or []:
        date_key = row.get("plan_date") or row.get("date")
        if date_key != today_str:
            continue
        _fill_row(row)

    # Current hour only (future hours in rows are optimizer projections, not actuals).
    for row in plan.get("rows") or []:
        date_key = row.get("plan_date") or row.get("date")
        if date_key != today_str:
            continue
        h = row.get("hour")
        if h is None or int(h) != now.hour:
            continue
        _fill_row(row, hour_cap=now.hour)

    if not any(v is not None for v in today_slots):
        hourly = extract_plan_soc_hourly(plan)
        for h in range(0, now.hour + 1):
            v = hourly["today"][h]
            if v is None:
                continue
            for q in range(4):
                idx = h * 4 + q
                if idx <= max_idx:
                    today_slots[idx] = v

    # Hard clip — never draw "actual" into the future.
    for i in range(max_idx + 1, 96):
        today_slots[i] = None

    return {
        "today": today_slots,
        "tomorrow": [None] * 96,
    }


# Alias for extract_actual_soc_q15.
extract_plan_soc_q15 = extract_actual_soc_q15


def _plan_window_matches(stored: dict[str, Any], now) -> bool:
    """True when the SQLite plan is for the same calendar day and current hour."""
    start = now.replace(minute=0, second=0, microsecond=0)
    return (
        stored.get("today_date") == start.strftime("%Y-%m-%d")
        and stored.get("plan_from_hour") == start.hour
    )


def _compute_buy_tariff_rows(cfg: dict) -> list[dict[str, Any]]:
    """Build hourly buy-tariff rows from config (G12 zones) over the simulation horizon."""
    from datetime import timedelta

    from .g12_pricing import get_buy_price
    from .simulation_config import get_simulation_params

    steps = int(get_simulation_params(cfg)["horizon_hours"])
    start_dt = now_warsaw().replace(minute=0, second=0, microsecond=0)
    rows: list[dict[str, Any]] = []
    for step in range(steps):
        dt = start_dt + timedelta(hours=step)
        buy_price, g12_zone = get_buy_price(dt, cfg)
        rows.append({
            "hour": dt.hour,
            "plan_date": dt.strftime("%Y-%m-%d"),
            "start": interval_end_label(dt),
            "buy_price": round(buy_price, 4),
            "g12_zone": g12_zone,
        })
    return rows


def build_buy_tariff_payload(
    cfg: dict,
    sim_rows: list[dict] | None = None,
) -> dict[str, Any]:
    """Hourly buy-tariff rows aligned with the simulation horizon."""
    if sim_rows is None:
        rows = _compute_buy_tariff_rows(cfg)
    else:
        rows = [
            {
                "hour": r["hour"],
                "plan_date": r.get("plan_date"),
                "start": r["start"],
                "buy_price": r["buy_price"],
                "g12_zone": r["g12_zone"],
            }
            for r in sim_rows
        ]
    g12 = cfg["grid"]["g12"]
    return {
        "rows": rows,
        "tariff_name": g12.get("tariff_name", "Buy Tariff"),
        "peak_price_pln_kwh": g12.get("peak_price_pln_kwh"),
        "offpeak_price_pln_kwh": g12.get("offpeak_price_pln_kwh"),
    }


async def fetch_plan_inputs(
    cfg: dict, *, invalidate: bool = False,
) -> tuple[dict, dict, dict, dict]:
    """Return (forecast, metrics, rules, rce_prices)."""
    if invalidate:
        from .cache_registry import invalidate_input_caches
        invalidate_input_caches()

    today_str = now_warsaw().strftime("%Y-%m-%d")
    results = await asyncio.gather(
        sa_client.get_live_metrics(cfg),
        sa_client.get_rules(cfg, fresh=invalidate),
        rce_mod.get_rce_prices(),
        influxdb_mod.get_accruals_for_date(today_str),
        return_exceptions=True,
    )

    def _safe(val: Any, default: Any) -> Any:
        return default if isinstance(val, Exception) else val

    metrics = _safe(results[0], {"battery_soc": None, "sa_online": False})
    rules = _safe(results[1], {})
    rce_prices = _safe(results[2], {"current_price_pln_kwh": None, "today": [], "tomorrow": []})
    today_day = _safe(results[3], {})
    today_pv_actual = None
    if isinstance(today_day, dict) and today_day.get("hourly"):
        metrics = dict(metrics)
        metrics["today_hourly"] = today_day["hourly"]
        today_pv_actual = today_day["hourly"].get("pv")
        if today_day.get("series_10min"):
            metrics["series_10min"] = today_day["series_10min"]
    # Yesterday SOC: solid plan seeds from last available reading (10-min / hourly).
    yesterday_str = (now_warsaw() - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_day = await influxdb_mod.get_accruals_for_date(yesterday_str)
    if isinstance(prev_day, dict) and prev_day.get("hourly"):
        metrics = dict(metrics)
        metrics["prev_day_hourly"] = prev_day["hourly"]
        if prev_day.get("series_10min"):
            metrics["prev_day_series_10min"] = prev_day["series_10min"]
    forecast = await forecast_mod.get_forecast(cfg, today_pv_actual=today_pv_actual)
    return forecast, metrics, rules, rce_prices


def _wrap_sim_result(
    sim: dict[str, Any],
    *,
    forecast: dict,
    metrics: dict,
    rce_prices: dict,
    cfg: dict,
    rules: dict,
) -> dict[str, Any]:
    forecast_bundle = dict(forecast)
    if metrics.get("today_hourly"):
        forecast_bundle["load_actual_hourly"] = metrics["today_hourly"].get("load")
        forecast_bundle["pv_actual_hourly"] = metrics["today_hourly"].get("pv")

    now = now_warsaw()
    next_hour = (now.hour + 1) % 24
    next_hour_schedule = build_hourly_schedule(sim["rows"], next_hour, cfg, rules)
    computed_at = now.strftime("%Y-%m-%d %H:%M:%S")

    result: dict[str, Any] = {
        **sim,
        "computed_at": computed_at,
        "simulation_min_soc_pct": plan_min_soc_pct(cfg),
        "rce_current": rce_prices.get("current_price_pln_kwh"),
        "next_hour": next_hour,
        "next_hour_schedule": next_hour_schedule,
        "forecast_meta": forecast.get("meta", {}),
        "forecast": forecast_bundle,
        "rce": rce_prices,
        "buy_tariff": build_buy_tariff_payload(cfg, sim["rows"]),
        # plan_soc_q15 comes from run_simulation (optimizer); do not overwrite with EA actuals.
        "actual_soc_q15": extract_actual_soc_q15(sim),
    }
    if result.get("rce"):
        rce_mod.prepare_rce_payload(result["rce"])
        result["rce_current"] = result["rce"].get("current_price_pln_kwh")
    return result


async def _run_fresh_simulation(
    cfg: dict,
    *,
    invalidate_inputs: bool,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict, dict, dict, dict]:
    forecast, metrics, rules, rce_prices = await fetch_plan_inputs(
        cfg, invalidate=invalidate_inputs,
    )
    sim = await asyncio.to_thread(
        run_simulation,
        forecast,
        metrics,
        rules,
        cfg,
        rce_prices=rce_prices,
        now=now,
    )
    return sim, forecast, metrics, rules, rce_prices


async def build_plan_simulation(
    cfg: dict,
    *,
    invalidate_inputs: bool = False,
    store_cache: bool = True,
) -> dict[str, Any]:
    """Return Energy arbitrage plan from SQLite; rebuild only on window mismatch.

    Forced recalculation (config / Overrides / EV / UI Refresh / quarter jobs)
    goes through ``hourly_plan_refresh``.
    """
    cfg = merge_simulation_defaults(cfg)
    now = now_warsaw()
    cached = read_plan()

    if cached is not None and _plan_window_matches(cached, now):
        result = dict(cached)
        if result.get("rce"):
            rce_mod.prepare_rce_payload(result["rce"])
            result["rce_current"] = result["rce"].get("current_price_pln_kwh")
        _overlay_meter_soc_on_plan(result, await _metrics_for_soc_overlay(), now)
        result["actual_soc_q15"] = extract_actual_soc_q15(result, now=now)
        return result

    async with _get_plan_lock():
        cached = read_plan()
        if cached is not None and _plan_window_matches(cached, now):
            result = dict(cached)
            if result.get("rce"):
                rce_mod.prepare_rce_payload(result["rce"])
                result["rce_current"] = result["rce"].get("current_price_pln_kwh")
            _overlay_meter_soc_on_plan(result, await _metrics_for_soc_overlay(), now)
            result["actual_soc_q15"] = extract_actual_soc_q15(result, now=now)
            return result

        existing = read_plan()
        log.info(
            "Plan rebuild (window mismatch) — window %s/%s vs now %s",
            (existing or {}).get("today_date"),
            (existing or {}).get("plan_from_hour"),
            now.strftime("%Y-%m-%d %H:%M"),
        )
        sim, forecast, metrics, rules, rce_prices = await _run_fresh_simulation(
            cfg, invalidate_inputs=invalidate_inputs, now=now,
        )
        result = _wrap_sim_result(
            sim,
            forecast=forecast,
            metrics=metrics,
            rce_prices=rce_prices,
            cfg=cfg,
            rules=rules,
        )
        if existing is not None and not plan_needs_full_rebuild(existing, now):
            # Same day: merge so locked timer and from_actual q15 stay in SQLite.
            result = merge_incremental_plan(
                existing,
                result,
                now=now,
                metrics=metrics,
                cfg=cfg,
                rules=rules,
            )
        else:
            apply_locked_hour_labels_from_plan(result, existing, now, cfg=cfg)
            attach_immutable_history(result, existing, now=now)
        # Chart: solid stays frozen across window-mismatch merge (unlock via
        # hourly_plan_refresh(unlock_plan_soc=True)).
        result["plan_soc_q15"] = compose_plan_soc_q15(existing, result)
        result["plan_soc_q15"] = rebuild_tomorrow_plan_soc_from_ea_end(
            result["plan_soc_q15"],
            forecast=result.get("forecast") or forecast,
            cfg=cfg,
            today_date=str(result.get("today_date") or now.strftime("%Y-%m-%d")),
            ea_end_soc_pct=ea_today_end_soc_pct(result),
        )
        result["plan_soc_day_locked"] = any(
            v is not None for v in (result["plan_soc_q15"].get("today") or [])
        )
        _overlay_meter_soc_on_plan(result, metrics, now)
        result["actual_soc_q15"] = extract_actual_soc_q15(result, now=now)
        if store_cache:
            write_plan(result, now=now)
        return result


async def hourly_plan_refresh(
    cfg: dict,
    *,
    unlock_plan_soc: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scheduler / config refresh: incremental quarter merge or full rebuild at new day.

    *unlock_plan_soc*: when True (config / Forecast Overrides / EV / manual timer),
    replace the frozen solid SOC day-plan curve with the fresh simulator curve.
    Quarterly :00/:15/:30/:45 jobs keep the lock (default False).

    *now*: quarter-tick clock for merge (scheduler passes the job-start tick so a
    long Open-Meteo/sim run past :00 still finalizes previous-hour q3).
    """
    log.info(
        "Plan Simulation refresh%s …",
        " (unlock plan SOC)" if unlock_plan_soc else "",
    )
    cfg = merge_simulation_defaults(cfg)
    now = now or now_warsaw()
    existing = read_plan()

    async with _get_plan_lock():
        existing = read_plan()
        sim, forecast, metrics, rules, rce_prices = await _run_fresh_simulation(
            cfg, invalidate_inputs=True, now=now,
        )
        fresh = _wrap_sim_result(
            sim,
            forecast=forecast,
            metrics=metrics,
            rce_prices=rce_prices,
            cfg=cfg,
            rules=rules,
        )

        if plan_needs_full_rebuild(existing, now):
            result = fresh
            apply_locked_hour_labels_from_plan(result, existing, now, cfg=cfg)
            attach_immutable_history(result, existing, now=now)
        else:
            result = merge_incremental_plan(
                existing or {},
                fresh,
                now=now,
                metrics=metrics,
                cfg=cfg,
                rules=rules,
            )

        # Solid = as-if-00:00 day plan (frozen once); dashed = EA actual.
        result["plan_soc_q15"] = compose_plan_soc_q15(
            existing, result, refresh=unlock_plan_soc,
        )
        result["plan_soc_q15"] = rebuild_tomorrow_plan_soc_from_ea_end(
            result["plan_soc_q15"],
            forecast=result.get("forecast") or forecast,
            cfg=cfg,
            today_date=str(result.get("today_date") or now.strftime("%Y-%m-%d")),
            ea_end_soc_pct=ea_today_end_soc_pct(result),
        )
        result["plan_soc_day_locked"] = any(
            v is not None for v in (result["plan_soc_q15"].get("today") or [])
        )
        _overlay_meter_soc_on_plan(result, metrics, now)
        result["actual_soc_q15"] = extract_actual_soc_q15(result, now=now)

        result["next_hour"] = (now.hour + 1) % 24
        result["next_hour_schedule"] = build_hourly_schedule(
            result["rows"], result["next_hour"], cfg, rules,
        )
        if result.get("rce"):
            rce_mod.prepare_rce_payload(result["rce"])
            result["rce_current"] = result["rce"].get("current_price_pln_kwh")

        write_plan(result, now=now)
        log.info(
            "Plan updated %s — Δ=%.2f kWh, export_hours=%s",
            result["computed_at"],
            result["delta_kwh"],
            result.get("plan_export_hours"),
        )
        return result


def apply_current_g12_to_ea_payload(
    payload: dict[str, Any],
    cfg: dict,
) -> tuple[dict[str, Any], bool]:
    """Reprice Buy Price / Energy Cost on stored EA rows from current G12/G12w."""
    out = dict(payload)
    hist, hist_changed = reprice_history_rows_to_current_g12(
        list(out.get("history_rows") or []), cfg,
    )
    live, live_changed = reprice_history_rows_to_current_g12(
        list(out.get("rows") or []), cfg,
    )
    out["history_rows"] = hist
    out["rows"] = live
    day_rows = [
        r for r in hist + live
        if str(r.get("start") or "") != "TOTAL"
    ]
    if day_rows:
        out["totals"] = compute_plan_totals(day_rows)
    return out, hist_changed or live_changed


def reprice_all_stored_ea_g12(cfg: dict) -> dict[str, Any]:
    """Reprice every plan_day_archive and plan_latest EA cash to current G12."""
    cfg = merge_simulation_defaults(cfg)
    checked = 0
    repriced: list[str] = []
    for day in list_plan_day_archives(limit=None):
        payload = load_plan_day_archive(day)
        checked += 1
        if not payload:
            continue
        out, changed = apply_current_g12_to_ea_payload(payload, cfg)
        if changed:
            save_plan_day_archive(day, out)
            repriced.append(day)
    live_changed = False
    plan = read_plan()
    if plan:
        out, changed = apply_current_g12_to_ea_payload(plan, cfg)
        if changed:
            replace_plan_latest_payload(out)
            live_changed = True
    log.info(
        "EA G12 reprice archives checked=%s changed=%s live=%s",
        checked,
        len(repriced),
        live_changed,
    )
    return {
        "archives_checked": checked,
        "archives_repriced": repriced,
        "plan_latest_repriced": live_changed,
    }


def apply_rce_backfill_to_ea_payload(
    payload: dict[str, Any],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    """Fill missing RCE on stored EA rows from PSE quarters; recompute totals."""
    out = dict(payload)
    hist, hist_filled = backfill_history_rows_rce(
        list(out.get("history_rows") or []), quarters_by_date, cfg,
    )
    live, live_filled = backfill_history_rows_rce(
        list(out.get("rows") or []), quarters_by_date, cfg,
    )
    out["history_rows"] = hist
    out["rows"] = live
    filled = hist_filled + live_filled
    if filled:
        day_rows = [
            r for r in hist + live
            if str(r.get("start") or "") != "TOTAL"
        ]
        if day_rows:
            out["totals"] = compute_plan_totals(day_rows)
    return out, filled


def _payload_rce_dates(payload: dict[str, Any]) -> list[str]:
    dates: set[str] = set()
    for r in list(payload.get("history_rows") or []) + list(payload.get("rows") or []):
        day = str(r.get("plan_date") or "")
        if day:
            dates.add(day)
    return sorted(dates)


def backfill_all_stored_ea_rce(cfg: dict) -> dict[str, Any]:
    """Fill missing RCE in every plan_day_archive and plan_latest from PSE."""
    cfg = merge_simulation_defaults(cfg)
    days = list(list_plan_day_archives(limit=None))
    plan = read_plan()
    fetch_dates = list(days)
    if plan:
        fetch_dates.extend(_payload_rce_dates(plan))
    fetch_dates = sorted({d for d in fetch_dates if d})
    quarters: dict[str, list[float | None]] = {}
    if fetch_dates:
        quarters = rce_mod.quarter_rce_for_dates(*fetch_dates)
    archives_filled: dict[str, list[int]] = {}
    checked = 0
    for day in days:
        payload = load_plan_day_archive(day)
        checked += 1
        if not payload:
            continue
        out, filled = apply_rce_backfill_to_ea_payload(payload, quarters, cfg)
        if filled:
            save_plan_day_archive(day, out)
            archives_filled[day] = [h for d, h in filled]
    live_filled: list[tuple[str, int]] = []
    if plan:
        out, filled = apply_rce_backfill_to_ea_payload(plan, quarters, cfg)
        if filled:
            replace_plan_latest_payload(out)
            live_filled = filled
    log.info(
        "EA RCE backfill archives checked=%s days_filled=%s live=%s",
        checked,
        list(archives_filled),
        live_filled,
    )
    return {
        "archives_checked": checked,
        "archives_filled": archives_filled,
        "plan_latest_filled": live_filled,
    }


async def build_past_day_simulation(cfg: dict, date_str: str) -> dict[str, Any]:
    """Read-only EA day view for a past Warsaw date.

    Prefer ``plan_day_archive`` (keeps Timer Schedule). Fallback: rebuild hourly
    rows from Influx actuals (timers empty — not stored historically).
    """
    cfg = merge_simulation_defaults(cfg)
    archived = load_plan_day_archive(date_str)
    if archived and (archived.get("history_rows") or archived.get("rows")):
        out, changed = apply_current_g12_to_ea_payload(archived, cfg)
        if history_rows_have_rce_holes(out.get("history_rows")) or history_rows_have_rce_holes(
            out.get("rows"),
        ):
            quarters = await rce_mod.get_quarter_rce_for_dates(date_str)
            out, rce_filled = apply_rce_backfill_to_ea_payload(out, quarters, cfg)
            changed = changed or bool(rce_filled)
        if changed:
            save_plan_day_archive(date_str, out)
        out["today_date"] = date_str
        out["history_view"] = True
        out["history_source"] = "archive"
        out.setdefault("rows", [])
        out.setdefault("plan_from_hour", 24)
        out["has_history_rows"] = bool(out.get("history_rows"))
        return out

    params = get_simulation_params(cfg)
    try:
        acc, quarters_by_date = await asyncio.gather(
            influxdb_mod.get_accruals_for_date(date_str),
            rce_mod.get_quarter_rce_for_dates(date_str),
        )
    except Exception as exc:
        log.warning("Past day %s Influx/RCE fetch failed: %s", date_str, exc)
        return {
            "today_date": date_str,
            "history_rows": [],
            "rows": [],
            "totals": None,
            "history_view": True,
            "history_source": "influx",
            "plan_from_hour": 24,
            "has_history_rows": False,
            "error": str(exc),
            "g12_tariff_name": cfg.get("grid", {}).get("g12", {}).get("tariff_name", "G12"),
            "simulation_min_soc_pct": plan_min_soc_pct(cfg),
        }
    if not isinstance(acc, dict) or acc.get("error"):
        return {
            "today_date": date_str,
            "history_rows": [],
            "rows": [],
            "totals": None,
            "history_view": True,
            "history_source": "influx",
            "plan_from_hour": 24,
            "has_history_rows": False,
            "error": (acc or {}).get("error") if isinstance(acc, dict) else "no data",
            "g12_tariff_name": cfg.get("grid", {}).get("g12", {}).get("tariff_name", "G12"),
            "simulation_min_soc_pct": plan_min_soc_pct(cfg),
        }
    hourly = acc.get("hourly") or {}
    history_rows = build_completed_history_rows(
        date_str, 24, hourly, quarters_by_date, cfg, params,
    )
    totals = compute_plan_totals(history_rows) if history_rows else None
    return {
        "today_date": date_str,
        "history_rows": history_rows,
        "rows": [],
        "totals": totals,
        "history_view": True,
        "history_source": "influx",
        "plan_from_hour": 24,
        "has_history_rows": bool(history_rows),
        "g12_tariff_name": cfg.get("grid", {}).get("g12", {}).get("tariff_name", "G12"),
        "simulation_min_soc_pct": plan_min_soc_pct(cfg),
        "computed_at": now_warsaw().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def get_simulation_for_date(
    cfg: dict,
    date_str: str | None = None,
    *,
    refresh: bool = False,
    unlock_plan_soc: bool = False,
) -> dict[str, Any]:
    """Today → live plan; past date → archive / Influx history view.

    *unlock_plan_soc*: replace the frozen as-if-00:00 solid SOC curve (Forecast
    Overrides Refresh / config). Ignored unless *refresh* is True.
    """
    now = now_warsaw()
    today = now.strftime("%Y-%m-%d")
    day = (date_str or today).strip()
    if len(day) != 10:
        day = today
    if day > today:
        day = today
    if day == today:
        if refresh:
            out = await hourly_plan_refresh(cfg, unlock_plan_soc=unlock_plan_soc)
        else:
            out = await build_plan_simulation(cfg, invalidate_inputs=False)
    else:
        out = await build_past_day_simulation(cfg, day)
    from .g12_pricing import g12_buy_energy_price_pln_kwh
    out["g12_peak_energy_only_pln_kwh"] = round(g12_buy_energy_price_pln_kwh("peak", cfg), 4)
    out["g12_offpeak_energy_only_pln_kwh"] = round(g12_buy_energy_price_pln_kwh("offpeak", cfg), 4)
    return out
