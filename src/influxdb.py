"""InfluxDB query module for daily energy accruals.

SA stores hourly energy in InfluxDB (port 8086, db=solar_assistant).
Pi clock is UTC; calendar days and hour buckets use Europe/Warsaw.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_DB  = "solar_assistant"
CACHE_TTL_S  = 60   # refresh every minute
WARSAW = ZoneInfo("Europe/Warsaw")

_cache: dict[str, Any] = {}


def now_warsaw() -> datetime:
    """Current Warsaw clock as a naive datetime."""
    return datetime.now(WARSAW).replace(tzinfo=None)


def _warsaw_midnight_utc() -> datetime:
    """Today's Warsaw midnight as an aware UTC datetime."""
    now_w = datetime.now(WARSAW)
    midnight_w = now_w.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_w.astimezone(timezone.utc)


def _warsaw_day_bounds_utc(date_str: str) -> tuple[datetime, datetime]:
    """UTC instants of [date 00:00, next day 00:00) in Warsaw."""
    start_w = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=WARSAW)
    end_w = start_w + timedelta(days=1)
    return start_w.astimezone(timezone.utc), end_w.astimezone(timezone.utc)


def _parse_influx_utc(ts: str) -> datetime | None:
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(ts[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc)


def _influx_ts_warsaw(ts: str) -> datetime | None:
    dt_utc = _parse_influx_utc(ts)
    if dt_utc is None:
        return None
    return dt_utc.astimezone(WARSAW)


# ---------------------------------------------------------------------------
# Public async interface
# ---------------------------------------------------------------------------

async def get_accruals() -> dict[str, float]:
    """Return today's energy accruals in kWh (Warsaw midnight to now)."""
    return await asyncio.to_thread(_get_accruals_sync)


# ---------------------------------------------------------------------------
# Synchronous implementation
# ---------------------------------------------------------------------------

def _get_accruals_sync() -> dict[str, float]:
    global _cache
    now_ts = time.time()
    if _cache.get("ts", 0) > now_ts - CACHE_TTL_S:
        return _cache["data"]

    result = _query_today()
    _cache = {"ts": now_ts, "data": result}
    return result


def _query_today() -> dict[str, float]:
    """Today's accrual totals — fast path without chart dots."""
    today_str = now_warsaw().strftime("%Y-%m-%d")
    day = _query_day(today_str)
    if day.get("error"):
        return {
            "pv_energy_today": 0.0,
            "load_energy_today": 0.0,
            "grid_buy_energy": 0.0,
            "grid_sell_energy": 0.0,
            "battery_charged_today": 0.0,
            "battery_discharged_today": 0.0,
        }
    t = day["totals"]
    return {
        "pv_energy_today": t.get("pv", 0.0),
        "load_energy_today": t.get("load", 0.0),
        "grid_buy_energy": t.get("grid_buy", 0.0),
        "grid_sell_energy": t.get("grid_sell", 0.0),
        "battery_charged_today": t.get("bat_charge", 0.0),
        "battery_discharged_today": t.get("bat_discharge", 0.0),
    }


def _influx_query(q: str) -> dict:
    url = f"{INFLUXDB_URL}/query?db={INFLUXDB_DB}&q={quote(q)}"
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json().get("results", [{}])[0]


def _split_srne_gross_grid(
    hourly: dict[str, list[float | None]],
    *,
    epsilon: float = 0.001,
) -> tuple[list[float | None], list[float | None], float, float]:
    """Split SA gross grid throughput (Grid power in hourly) into import and export.

    On SRNE via SolarAssistant, ``Grid power out hourly`` is often empty and
    ``Grid power in hourly`` stores import + export combined. When the hourly
    PV/load/battery balance shows a deficit (net import), the full gross meter
    reading counts as grid import (matches utility meter during grid-charge hours).
    When the balance shows export, gross counts as export.
    """
    buy: list[float | None] = []
    sell: list[float | None] = []
    gross_buy = 0.0
    gross_sell = 0.0
    for h in range(24):
        g_in = hourly["grid_in"][h]
        pv = hourly["pv"][h]
        load = hourly["load"][h]
        bat_in = hourly["bat_charge"][h]
        bat_out = hourly["bat_discharge"][h]
        has_flow = g_in is not None
        has_balance = not all(v is None for v in (pv, load, bat_in, bat_out))
        if not has_flow and not has_balance:
            buy.append(None)
            sell.append(None)
            continue
        gross = float(g_in or 0)
        if has_balance:
            net = float(load or 0) + float(bat_in or 0) - float(pv or 0) - float(bat_out or 0)
            if net > epsilon:
                if gross > epsilon:
                    imp, exp = gross, 0.0
                else:
                    imp, exp = net, 0.0
            elif gross > epsilon:
                imp, exp = 0.0, gross
            else:
                imp, exp = 0.0, 0.0
        else:
            imp, exp = gross, 0.0
        gross_buy += imp
        gross_sell += exp
        buy.append(round(-imp, 3) if imp > epsilon else 0.0)
        sell.append(round(exp, 3) if exp > epsilon else 0.0)
    return buy, sell, round(gross_buy, 3), round(gross_sell, 3)


def _grid_from_meter_hourly(
    hourly: dict[str, list[float | None]],
) -> tuple[list[float | None], list[float | None]]:
    """Map SA grid meter hourly (kWh) to signed import / positive export series."""
    buy: list[float | None] = []
    sell: list[float | None] = []
    for h in range(24):
        g_in = hourly["grid_in"][h]
        g_out = hourly["grid_out"][h]
        if g_in is None and g_out is None:
            buy.append(None)
            sell.append(None)
            continue
        imp = float(g_in or 0)
        exp = float(g_out or 0)
        buy.append(round(-imp, 3) if imp > 0 else 0.0)
        sell.append(round(exp, 3) if exp > 0 else 0.0)
    return buy, sell


def _derive_grid_hourly(
    hourly: dict[str, list[float | None]],
) -> tuple[list[float | None], list[float | None]]:
    """Derive signed grid import (negative kWh) and gross export (positive kWh).

    Hourly energy balance:

        PV + grid_import + bat_discharge = load + grid_export + bat_charge

    Per hour only one direction is active.  Import is negative for display.
    Hours without any source readings stay ``None`` (e.g. future slots today).
    """
    buy: list[float | None] = []
    sell: list[float | None] = []
    for h in range(24):
        pv = hourly["pv"][h]
        load = hourly["load"][h]
        bat_in = hourly["bat_charge"][h]
        bat_out = hourly["bat_discharge"][h]
        if all(v is None for v in (pv, load, bat_in, bat_out)):
            buy.append(None)
            sell.append(None)
            continue
        net = float(load or 0) + float(bat_in or 0) - float(pv or 0) - float(bat_out or 0)
        if net > 0:
            buy.append(round(-net, 3))
            sell.append(0.0)
        else:
            buy.append(0.0)
            sell.append(round(-net, 3))
    return buy, sell


def _gross_grid_totals(hourly: dict[str, list[float | None]]) -> tuple[float, float]:
    """Sum hourly grid import/export (SA gross kWh, not net daily balance)."""
    import_kwh = 0.0
    export_kwh = 0.0
    for h in range(24):
        pv = hourly["pv"][h]
        load = hourly["load"][h]
        bat_in = hourly["bat_charge"][h]
        bat_out = hourly["bat_discharge"][h]
        if all(v is None for v in (pv, load, bat_in, bat_out)):
            continue
        net = float(load or 0) + float(bat_in or 0) - float(pv or 0) - float(bat_out or 0)
        if net > 0:
            import_kwh += net
        else:
            export_kwh += -net
    return round(import_kwh, 3), round(export_kwh, 3)


# ---------------------------------------------------------------------------
# Per-day accruals + hourly breakdown (for any historical date)
# ---------------------------------------------------------------------------

CHART_SLOT_MIN = 10
SLOTS_CHART = 24 * 60 // CHART_SLOT_MIN  # 144 × 10-min buckets per Warsaw day


def _warsaw_slot_index(ts: str, date_str: str) -> int | None:
    """Map Influx UTC bucket timestamp to 0..143 slot for a Warsaw calendar day."""
    dt_w = _influx_ts_warsaw(ts)
    if dt_w is None:
        return None
    if dt_w.strftime("%Y-%m-%d") != date_str:
        return None
    return (dt_w.hour * 60 + dt_w.minute) // CHART_SLOT_MIN


def _influx_chart_mean_kw(
    meas: str,
    since: str,
    until: str,
    date_str: str,
) -> list[float | None]:
    """Mean power (kW) per chart bucket from a live SA watt measurement."""
    q = (
        f'SELECT mean("combined") FROM "{meas}" '
        f'WHERE time >= {since} AND time < {until} '
        f'GROUP BY time({CHART_SLOT_MIN}m) fill(none) ORDER BY time ASC'
    )
    data = _influx_query(q)
    out: list[float | None] = [None] * SLOTS_CHART
    for series in data.get("series", []):
        cols = series.get("columns", [])
        try:
            ti = cols.index("time")
            vi = cols.index("mean")
        except ValueError:
            continue
        for row in series.get("values", []):
            ts, val = row[ti], row[vi]
            if val is None:
                continue
            idx = _warsaw_slot_index(ts, date_str)
            if idx is not None and 0 <= idx < SLOTS_CHART:
                out[idx] = round(float(val) / 1000.0, 3)
    return out


def _influx_chart_soc(
    since: str,
    until: str,
    date_str: str,
) -> list[float | None]:
    """Last SOC (%) per chart bucket."""
    q = (
        f'SELECT last("combined") FROM "Battery state of charge" '
        f'WHERE time >= {since} AND time < {until} '
        f'GROUP BY time({CHART_SLOT_MIN}m) fill(none) ORDER BY time ASC'
    )
    data = _influx_query(q)
    out: list[float | None] = [None] * SLOTS_CHART
    for series in data.get("series", []):
        cols = series.get("columns", [])
        try:
            ti = cols.index("time")
            vi = cols.index("last")
        except ValueError:
            try:
                vi = cols.index("combined")
            except ValueError:
                continue
        for row in series.get("values", []):
            ts, val = row[ti], row[vi]
            if val is None:
                continue
            idx = _warsaw_slot_index(ts, date_str)
            if idx is not None and 0 <= idx < SLOTS_CHART:
                out[idx] = round(float(val), 1)
    return out


def _split_battery_power_chart_kw(
    bat_kw: list[float | None],
) -> tuple[list[float | None], list[float | None]]:
    """Split signed mean battery power (kW) into charge (+) and discharge (+) chart series.

    Influx ``Battery power``: positive = charging into battery, negative = discharge.
    """
    charge: list[float | None] = []
    discharge: list[float | None] = []
    for v in bat_kw:
        if v is None:
            charge.append(None)
            discharge.append(None)
            continue
        fv = float(v)
        if fv > 0:
            charge.append(round(fv, 3))
            discharge.append(0.0)
        elif fv < 0:
            charge.append(0.0)
            discharge.append(round(-fv, 3))
        else:
            charge.append(0.0)
            discharge.append(0.0)
    return charge, discharge


def _derive_battery_chart_kw(
    pv_kw: list[float | None],
    load_kw: list[float | None],
    grid_buy_kw: list[float | None],
    grid_sell_kw: list[float | None],
) -> tuple[list[float | None], list[float | None]]:
    """Derive per-slot battery charge/discharge (kW) from PV, load, and grid flows.

    Hourly energy balance (positive kWh):

        PV + grid_import + bat_discharge = load + grid_export + bat_charge

    Per 10-min bucket with mean kW (same convention as chart series):

        net_charge_kw = pv + import_kw - load - export_kw
    """
    n = max(len(pv_kw), len(load_kw), len(grid_buy_kw), len(grid_sell_kw))
    charge: list[float | None] = [None] * n
    discharge: list[float | None] = [None] * n
    for i in range(n):
        pv = pv_kw[i] if i < len(pv_kw) else None
        load = load_kw[i] if i < len(load_kw) else None
        buy = grid_buy_kw[i] if i < len(grid_buy_kw) else None
        sell = grid_sell_kw[i] if i < len(grid_sell_kw) else None
        if pv is None and load is None and buy is None and sell is None:
            continue
        import_kw = abs(float(buy)) if buy is not None and float(buy) < 0 else 0.0
        export_kw = float(sell) if sell is not None and float(sell) > 0 else 0.0
        net = float(pv or 0) + import_kw - float(load or 0) - export_kw
        if net > 0:
            charge[i] = round(net, 3)
            discharge[i] = 0.0
        elif net < 0:
            charge[i] = 0.0
            discharge[i] = round(-net, 3)
        else:
            charge[i] = 0.0
            discharge[i] = 0.0
    return charge, discharge


def _split_grid_power_chart_kw(
    grid_kw: list[float | None],
) -> tuple[list[float | None], list[float | None]]:
    """Split signed mean grid power (kW) into import (−) and export (+) chart series.

    Influx ``Grid power`` matches SA ``total/grid_power``: negative = export,
    positive = import.
    """
    buy: list[float | None] = []
    sell: list[float | None] = []
    for v in grid_kw:
        if v is None:
            buy.append(None)
            sell.append(None)
            continue
        fv = float(v)
        if fv < 0:
            buy.append(0.0)
            sell.append(round(-fv, 3))
        elif fv > 0:
            buy.append(round(-fv, 3))
            sell.append(0.0)
        else:
            buy.append(0.0)
            sell.append(0.0)
    return buy, sell


def _hourly_kwh_to_chart_kw(hourly: list[float | None]) -> list[float | None]:
    """Spread SA hourly kWh accruals into flat chart slots (average kW per hour).

    One hour of export at 0.024 kWh → 0.024 kW in each 10-min slot; integrating
    all slots in that hour recovers 0.024 kWh (matches totals card).
    """
    slots_per_hour = 60 // CHART_SLOT_MIN
    out: list[float | None] = [None] * SLOTS_CHART
    for h in range(24):
        if h >= len(hourly):
            break
        v = hourly[h]
        if v is None:
            continue
        kw = round(float(v), 3)
        base = h * slots_per_hour
        for q in range(slots_per_hour):
            out[base + q] = kw
    return out


def get_load_kwh_10min_for_date_sync(date_str: str) -> list[float] | None:
    """Energy kWh per 10-min Warsaw bucket for one calendar day (Load power)."""
    try:
        start_utc, end_utc = _warsaw_day_bounds_utc(date_str)
    except ValueError:
        return None
    since = start_utc.strftime("'%Y-%m-%dT%H:%M:%SZ'")
    until = end_utc.strftime("'%Y-%m-%dT%H:%M:%SZ'")
    kw_slots = _influx_chart_mean_kw("Load power", since, until, date_str)
    if not any(v is not None for v in kw_slots):
        return None
    kwh_per_slot = CHART_SLOT_MIN / 60.0
    return [round(float(v or 0.0) * kwh_per_slot, 6) for v in kw_slots]


def _query_day_chart_series(
    date_str: str,
    since: str,
    until: str,
    grid_buy_hourly: list[float | None],
    grid_sell_hourly: list[float | None],
) -> dict[str, list[float | None]]:
    """10-min PV/Load/Grid power (kW) + SOC for charts."""
    pv = _influx_chart_mean_kw("PV power", since, until, date_str)
    load = _influx_chart_mean_kw("Load power", since, until, date_str)
    soc = _influx_chart_soc(since, until, date_str)
    grid_kw = _influx_chart_mean_kw("Grid power", since, until, date_str)
    if any(v is not None for v in grid_kw):
        grid_buy, grid_sell = _split_grid_power_chart_kw(grid_kw)
    else:
        grid_buy = _hourly_kwh_to_chart_kw(grid_buy_hourly)
        grid_sell = _hourly_kwh_to_chart_kw(grid_sell_hourly)

    bat_kw = _influx_chart_mean_kw("Battery power", since, until, date_str)
    if any(v is not None for v in bat_kw):
        bat_charge, bat_discharge = _split_battery_power_chart_kw(bat_kw)
    else:
        bat_charge, bat_discharge = _derive_battery_chart_kw(
            pv, load, grid_buy, grid_sell,
        )

    return {
        "pv": pv,
        "load": load,
        "grid_buy": grid_buy,
        "grid_sell": grid_sell,
        "bat_charge": bat_charge,
        "bat_discharge": bat_discharge,
        "soc": soc,
    }


_DAY_CACHE: dict[str, Any] = {}
_DAY_CACHE_TTL = 120  # 2 min for today, cached permanently for past days


def invalidate_caches() -> None:
    """Clear in-memory accrual / load-profile caches (no process restart)."""
    global _cache, _DAY_CACHE, _load_cache
    _cache = {}
    _DAY_CACHE = {}
    _load_cache = {}


async def get_accruals_for_date(date_str: str) -> dict[str, Any]:
    """Return daily totals and 24-slot hourly breakdown for a Warsaw calendar day."""
    return await asyncio.to_thread(_get_day_sync, date_str)


def _get_day_sync(date_str: str) -> dict[str, Any]:
    now_ts = time.time()
    cached = _DAY_CACHE.get(date_str)
    today_str = now_warsaw().strftime("%Y-%m-%d")
    ttl = _DAY_CACHE_TTL if date_str == today_str else 86400  # past days cached 24 h
    if cached and now_ts - cached["ts"] < ttl:
        return cached["data"]
    result = _query_day(date_str)
    _DAY_CACHE[date_str] = {"ts": now_ts, "data": result}
    return result


def _query_day(date_str: str) -> dict[str, Any]:
    """Query InfluxDB for one Warsaw day and return totals + hourly kWh."""
    try:
        start_utc, end_utc = _warsaw_day_bounds_utc(date_str)
    except ValueError:
        return {"error": "invalid date"}

    since = start_utc.strftime("'%Y-%m-%dT%H:%M:%SZ'")
    until = end_utc.strftime("'%Y-%m-%dT%H:%M:%SZ'")

    METRICS = {
        "pv":           "PV power hourly",
        "load":         "Load power hourly",
        "bat_charge":   "Battery power in hourly",
        "bat_discharge":"Battery power out hourly",
        "grid_in":      "Grid power in hourly",
        "grid_out":     "Grid power out hourly",
    }

    totals:  dict[str, float]              = {}
    hourly:  dict[str, list[float | None]] = {
        k: [None] * 24
        for k in ("pv", "load", "bat_charge", "bat_discharge", "grid_buy", "grid_sell", "grid_in", "grid_out")
    }
    hourly["soc"] = [None] * 24

    for key, meas in METRICS.items():
        q = (f'SELECT * FROM "{meas}" '
             f'WHERE time >= {since} AND time < {until} ORDER BY time ASC')
        data = _influx_query(q)
        total_wh = 0.0
        for series in data.get("series", []):
            cols = series.get("columns", [])
            try:
                ti = cols.index("time"); vi = cols.index("combined")
            except ValueError:
                continue
            for row in series.get("values", []):
                ts, val = row[ti], row[vi]
                if val is None:
                    continue
                try:
                    dt_w = _influx_ts_warsaw(ts)
                    if dt_w is None:
                        continue
                    h = dt_w.hour
                    kwh = float(val) / 1000.0
                    hourly[key][h] = round(kwh, 3)
                    total_wh += float(val)
                except Exception:
                    pass
        totals[key] = round(total_wh / 1000.0, 3)

    # Prefer signed grid in/out series; else split gross meters or derive from energy balance.
    has_grid_in_series = any(v is not None for v in hourly["grid_in"])
    has_grid_out_series = any(v is not None for v in hourly["grid_out"])
    if has_grid_in_series and has_grid_out_series:
        grid_buy, grid_sell = _grid_from_meter_hourly(hourly)
        gross_buy = round(sum(float(v or 0) for v in hourly["grid_in"]), 3)
        gross_sell = round(sum(float(v or 0) for v in hourly["grid_out"]), 3)
    elif has_grid_in_series or has_grid_out_series:
        grid_buy, grid_sell, gross_buy, gross_sell = _split_srne_gross_grid(hourly)
    else:
        grid_buy, grid_sell = _derive_grid_hourly(hourly)
        gross_buy, gross_sell = _gross_grid_totals(hourly)
    totals["grid_sell"] = gross_sell
    totals["grid_buy"] = gross_buy
    totals.pop("grid_in", None)
    totals.pop("grid_out", None)
    hourly["grid_buy"] = grid_buy
    hourly["grid_sell"] = grid_sell
    del hourly["grid_in"]
    del hourly["grid_out"]

    # Hourly SOC (%): last reading per UTC hour bucket.
    soc_q = (
        f'SELECT last("combined") FROM "Battery state of charge" '
        f'WHERE time >= {since} AND time < {until} GROUP BY time(1h)'
    )
    soc_data = _influx_query(soc_q)
    latest_soc: float | None = None
    for series in soc_data.get("series", []):
        cols = series.get("columns", [])
        try:
            ti = cols.index("time")
            vi = cols.index("last")
        except ValueError:
            try:
                vi = cols.index("combined")
            except ValueError:
                continue
        for row in series.get("values", []):
            ts, val = row[ti], row[vi]
            if val is None:
                continue
            try:
                dt_w = _influx_ts_warsaw(ts)
                if dt_w is None:
                    continue
                h = dt_w.hour
                soc_val = round(float(val), 1)
                hourly["soc"][h] = soc_val
                latest_soc = soc_val
            except Exception:
                pass
    if latest_soc is not None:
        totals["soc"] = latest_soc

    series_10min = _query_day_chart_series(
        date_str, since, until,
        hourly["grid_buy"], hourly["grid_sell"],
    )

    return {"date": date_str, "totals": totals, "hourly": hourly, "series_10min": series_10min}


# ---------------------------------------------------------------------------
# Load profile from history
# ---------------------------------------------------------------------------

_load_cache: dict[str, Any] = {}
_LOAD_CACHE_TTL = 300  # 5 min


async def get_load_profile() -> dict[str, Any]:
    """Return hourly load profile: today's actual kWh and 7-day average."""
    return await asyncio.to_thread(_get_load_profile_sync)


def _get_load_profile_sync() -> dict[str, Any]:
    global _load_cache
    now_ts = time.time()
    if _load_cache.get("ts", 0) > now_ts - _LOAD_CACHE_TTL:
        return _load_cache["data"]
    result = _query_load_profile()
    _load_cache = {"ts": now_ts, "data": result}
    return result


def _rows_to_hourly_wh(data: dict) -> dict[int, float]:
    """Parse InfluxDB series into {warsaw_hour: Wh} dict."""
    by_hour: dict[int, list] = {}
    for series in data.get("series", []):
        cols = series.get("columns", [])
        try:
            ti = cols.index("time")
            vi = cols.index("combined")
        except ValueError:
            continue
        for row in series.get("values", []):
            ts, val = row[ti], row[vi]
            if val is None:
                continue
            try:
                dt_w = _influx_ts_warsaw(ts)
                if dt_w is None:
                    continue
                h = dt_w.hour
                by_hour.setdefault(h, []).append(float(val))
            except Exception:
                pass
    return {h: sum(vs) for h, vs in by_hour.items()}


def _query_load_profile() -> dict[str, Any]:
    midnight_utc = _warsaw_midnight_utc()
    since_today = midnight_utc.strftime("'%Y-%m-%dT%H:%M:%SZ'")
    since_7d = (midnight_utc - timedelta(days=7)).strftime("'%Y-%m-%dT%H:%M:%SZ'")

    # Today's actual hourly load and PV (Warsaw midnight → now).
    data_today = _influx_query(
        f'SELECT * FROM "Load power hourly" WHERE time >= {since_today} ORDER BY time ASC'
    )
    today_wh = _rows_to_hourly_wh(data_today)
    today_actual: list[float | None] = [
        round(today_wh[h] / 1000.0, 3) if h in today_wh else None
        for h in range(24)
    ]

    data_pv_today = _influx_query(
        f'SELECT * FROM "PV power hourly" WHERE time >= {since_today} ORDER BY time ASC'
    )
    today_pv_wh = _rows_to_hourly_wh(data_pv_today)
    today_pv_actual: list[float | None] = [
        round(today_pv_wh[h] / 1000.0, 3) if h in today_pv_wh else None
        for h in range(24)
    ]

    # 7-day average by Warsaw hour (previous 7 days, not including today).
    data_7d = _influx_query(
        f'SELECT * FROM "Load power hourly" WHERE time >= {since_7d} AND time < {since_today} ORDER BY time ASC'
    )
    avg_wh = _rows_to_hourly_wh(data_7d)
    # Group by hour across multiple days then average.
    raw: dict[int, list] = {}
    for series in data_7d.get("series", []):
        cols = series.get("columns", [])
        try:
            ti = cols.index("time"); vi = cols.index("combined")
        except ValueError:
            continue
        for row in series.get("values", []):
            ts, val = row[ti], row[vi]
            if val is None:
                continue
            try:
                dt_w = _influx_ts_warsaw(ts)
                if dt_w is None:
                    continue
                h = dt_w.hour
                raw.setdefault(h, []).append(float(val))
            except Exception:
                pass
    avg_7day: list[float | None] = [
        round(sum(raw[h]) / len(raw[h]) / 1000.0, 3) if h in raw else None
        for h in range(24)
    ]

    return {"today_actual": today_actual, "today_pv_actual": today_pv_actual, "avg_7day": avg_7day}
