"""Actual Influx data for the last completed hour in plan simulation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .g12_pricing import get_buy_price
from .plan_cost import (
    derive_grid_flows_from_balance,
    hour_meter_cash_pln,
)
from .inverter_sim import resolve_hour0_soc_kwh
from .timer_plan import build_hour_timer_schedule, classify_action

SLOTS_PER_HOUR_10M = 6
Q15_PER_HOUR = 4
TEN_MIN_KWH_PER_KW = 10.0 / 60.0
PARTIAL_Q15_SCALE = 1.5


def hour_in_progress(now: datetime, hour_start: datetime) -> bool:
    return now > hour_start


def interval_end_label(hour_dt: datetime) -> str:
    """Table Start column = end of hourly bucket (15:00 row = energy over 14:00–15:00)."""
    return (hour_dt + timedelta(hours=1)).strftime("%d-%m-%Y %H:00")


def _hourly_slot(
    hourly: dict[str, list[float | None]] | None,
    hour: int,
    key: str,
) -> float | None:
    if not hourly:
        return None
    arr = hourly.get(key) or [None] * 24
    if 0 <= hour < len(arr):
        return arr[hour]
    return None


def build_meter_hour_row(
    hour_dt: datetime,
    hourly: dict[str, list[float | None]],
    *,
    cfg: dict,
    params: dict[str, float | int],
    rce_price: float | None,
    plan_date: str,
    meter_hour: int | None = None,
    production: float | None = None,
    consumption: float | None = None,
    rce_q15: list[float | None] | None = None,
    timer_schedule: str = "",
    require_pv_or_load: bool = True,
    include_q15: bool = True,
    extra_flags: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One hour from Influx meter series (EA history / H0 carryover)."""
    h_meter = hour_dt.hour if meter_hour is None else int(meter_hour)
    pv_h = _hourly_slot(hourly, h_meter, "pv")
    load_h = _hourly_slot(hourly, h_meter, "load")
    bat_in = _hourly_slot(hourly, h_meter, "bat_charge")
    bat_out = _hourly_slot(hourly, h_meter, "bat_discharge")
    grid_buy_h = _hourly_slot(hourly, h_meter, "grid_buy")
    grid_sell_h = _hourly_slot(hourly, h_meter, "grid_sell")
    soc_h = _hourly_slot(hourly, h_meter, "soc")

    if require_pv_or_load and pv_h is None and load_h is None:
        return None
    if not require_pv_or_load and all(
        v is None for v in (bat_in, bat_out, grid_buy_h, grid_sell_h, soc_h)
    ):
        return None

    pv = float(production) if production is not None else (
        float(pv_h) if pv_h is not None else 0.0
    )
    load = float(consumption) if consumption is not None else (
        float(load_h) if load_h is not None else 0.0
    )
    min_soc_pct = float(params["min_soc_pct"])

    if bat_in is not None or bat_out is not None:
        battery_delta = float(bat_in or 0.0) - float(bat_out or 0.0)
    else:
        battery_delta = 0.0

    epsilon = float(params["epsilon_kwh"])
    grid_import = abs(float(grid_buy_h)) if grid_buy_h is not None and grid_buy_h < 0 else 0.0
    grid_export = float(grid_sell_h) if grid_sell_h is not None and grid_sell_h > 0 else 0.0
    if grid_import <= 0.0 and grid_export <= 0.0 and (bat_in is not None or bat_out is not None):
        grid_import, grid_export = derive_grid_flows_from_balance(
            pv, load, battery_delta, epsilon=epsilon,
        )

    buy_price, g12_zone = get_buy_price(hour_dt, cfg)
    bat_in_kwh = float(bat_in or 0.0)
    bat_out_kwh = float(bat_out or 0.0)
    action = classify_action(
        bat_charge=bat_in_kwh,
        bat_discharge=bat_out_kwh,
        grid_import=grid_import,
        grid_export=grid_export,
        production=pv,
        epsilon=epsilon,
    )
    cash = hour_meter_cash_pln(
        grid_import, grid_export, buy_price, rce_price, cfg, g12_zone=g12_zone,
    )

    q15: list[dict[str, Any]] = []
    if include_q15:
        battery_cap = float(cfg["battery"]["capacity_kwh"])
        q15 = build_history_hour_q15(
            h_meter, hourly, battery_cap=battery_cap, min_soc_pct=min_soc_pct,
            bat_in_kwh=bat_in_kwh, bat_out_kwh=bat_out_kwh,
            grid_import=grid_import, grid_export=grid_export,
        )
    if soc_h is not None:
        soc_pct = _bound_soc_pct(float(soc_h))
    elif q15:
        soc_pct = float(q15[-1].get("soc") or 0.0)
    else:
        soc_pct = 0.0

    row: dict[str, Any] = {
        "hour": hour_dt.hour,
        "plan_date": plan_date,
        "start": interval_end_label(hour_dt),
        "production": round(pv, 3),
        "consumption": round(load, 3),
        "battery": round(battery_delta, 3),
        "bat_charge": round(bat_in_kwh, 3),
        "bat_discharge": round(bat_out_kwh, 3),
        "grid_import": round(grid_import, 3),
        "grid_export": round(grid_export, 3),
        "soc": round(soc_pct, 1),
        "import_cost": cash["import_cost"],
        "export_revenue": cash["export_revenue"],
        "energy_cost": cash["energy_cost"],
        "service_cost": cash["service_cost"],
        "cost": cash["cost"],
        "action": action,
        "timer_schedule": timer_schedule,
        "rce_price": round(rce_price, 4) if rce_price is not None else None,
        "export_credit": cash["export_credit"],
        "g12_zone": g12_zone,
        "buy_price": round(buy_price, 4),
        "export_planned": False,
    }
    if include_q15:
        row["q15"] = q15
        row["rce_q15"] = list(rce_q15) if rce_q15 else None
    if extra_flags:
        row.update(extra_flags)
    return row


def reprice_history_rows_to_current_g12(
    rows: list[dict[str, Any]],
    cfg: dict,
) -> tuple[list[dict[str, Any]], bool]:
    """Set stored EA hour buy zone from current G12/G12w; recompute hour cash."""
    changed = False
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("start") or "") == "TOTAL":
            out.append(row)
            continue
        plan_date = str(row.get("plan_date") or "")
        try:
            hour = int(row.get("hour"))
        except (TypeError, ValueError):
            out.append(row)
            continue
        if not plan_date:
            out.append(row)
            continue
        dt = datetime.strptime(plan_date, "%Y-%m-%d").replace(hour=hour)
        buy_price, zone = get_buy_price(dt, cfg)
        new_row = dict(row)
        priced = round(buy_price, 4)
        if new_row.get("buy_price") != priced or new_row.get("g12_zone") != zone:
            changed = True
        new_row["buy_price"] = priced
        new_row["g12_zone"] = zone
        cash = hour_meter_cash_pln(
            float(new_row.get("grid_import") or 0.0),
            float(new_row.get("grid_export") or 0.0),
            buy_price,
            new_row.get("rce_price"),
            cfg,
            g12_zone=zone,
        )
        new_row["import_cost"] = cash["import_cost"]
        new_row["export_revenue"] = cash["export_revenue"]
        new_row["energy_cost"] = cash["energy_cost"]
        new_row["service_cost"] = cash["service_cost"]
        new_row["cost"] = cash["cost"]
        new_row["export_credit"] = cash["export_credit"]
        new_row["import_energy_cost"] = cash.get("import_energy_cost")
        out.append(new_row)
    return out, changed


def _hour_rce_mean(quarters: list[float | None]) -> float | None:
    vals = [float(v) for v in quarters if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def backfill_history_rows_rce(
    rows: list[dict[str, Any]],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
) -> tuple[list[dict[str, Any]], list[tuple[str, int]]]:
    """Fill missing EA hour RCE quarters from PSE; recompute hour cash."""
    filled: list[tuple[str, int]] = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("start") or "") == "TOTAL":
            out.append(row)
            continue
        plan_date = str(row.get("plan_date") or "")
        try:
            hour = int(row.get("hour"))
        except (TypeError, ValueError):
            out.append(row)
            continue
        if not plan_date:
            out.append(row)
            continue
        series = quarters_by_date.get(plan_date) or []
        c0 = hour * 4
        pse_q = list(series[c0:c0 + 4])
        while len(pse_q) < 4:
            pse_q.append(None)
        stored = list(row.get("rce_q15") or [])
        while len(stored) < 4:
            stored.append(None)
        new_q: list[float | None] = []
        hour_filled = False
        for i in range(4):
            if stored[i] is not None:
                new_q.append(round(float(stored[i]), 4))
                continue
            if pse_q[i] is None:
                new_q.append(None)
                continue
            new_q.append(round(float(pse_q[i]), 4))
            hour_filled = True
        if not hour_filled:
            out.append(row)
            continue
        new_row = dict(row)
        new_row["rce_q15"] = new_q
        rce_price = _hour_rce_mean(new_q)
        new_row["rce_price"] = rce_price
        slots = new_row.get("q15")
        if isinstance(slots, list) and len(slots) == 4:
            patched: list[Any] = []
            for i, slot in enumerate(slots):
                if isinstance(slot, dict) and slot.get("rce") is None and new_q[i] is not None:
                    slot = dict(slot)
                    slot["rce"] = new_q[i]
                patched.append(slot)
            new_row["q15"] = patched
        zone = str(new_row.get("g12_zone") or "offpeak")
        buy = float(new_row.get("buy_price") or 0.0)
        cash = hour_meter_cash_pln(
            float(new_row.get("grid_import") or 0.0),
            float(new_row.get("grid_export") or 0.0),
            buy,
            rce_price,
            cfg,
            g12_zone=zone,
        )
        new_row["import_cost"] = cash["import_cost"]
        new_row["export_revenue"] = cash["export_revenue"]
        new_row["energy_cost"] = cash["energy_cost"]
        new_row["service_cost"] = cash["service_cost"]
        new_row["cost"] = cash["cost"]
        new_row["export_credit"] = cash["export_credit"]
        new_row["import_energy_cost"] = cash.get("import_energy_cost")
        filled.append((plan_date, hour))
        out.append(new_row)
    return out, filled


def history_rows_have_rce_holes(rows: list[dict[str, Any]] | None) -> bool:
    """True when any hour is missing a 15-min RCE slot."""
    for row in rows or []:
        if str(row.get("start") or "") == "TOTAL":
            continue
        qs = list(row.get("rce_q15") or [])
        if len(qs) < 4 or any(v is None for v in qs):
            return True
    return False


def hour_start_soc_kwh(
    hourly: dict[str, list[float | None]] | None,
    hour: int,
    battery_cap: float,
    min_soc_pct: float,
) -> float | None:
    """SOC at the start of *hour* from Influx (previous hour end, or hour-0 backtrack).

    Return None when Influx has no reading; callers fall back to live SOC.
    """
    if not hourly or not (0 <= hour < 24):
        return None
    del min_soc_pct
    soc_series = hourly.get("soc") or [None] * 24
    if hour == 0:
        # Backtrack only when hour-0 SOC exists; otherwise return None.
        if not soc_series or soc_series[0] is None:
            return None
        start_kwh, _ = resolve_hour0_soc_kwh(hourly, battery_cap)
        return start_kwh

    prev_pct = soc_series[hour - 1] if hour - 1 < len(soc_series) else None
    if prev_pct is not None:
        pct = _bound_soc_pct(float(prev_pct))
        return (pct / 100.0) * battery_cap

    end_pct = soc_series[hour] if hour < len(soc_series) else None
    if end_pct is None:
        return None
    end_kwh = (_bound_soc_pct(float(end_pct)) / 100.0) * battery_cap
    bc = float((hourly.get("bat_charge") or [None] * 24)[hour] or 0.0)
    bd = float((hourly.get("bat_discharge") or [None] * 24)[hour] or 0.0)
    return min(battery_cap, max(0.0, end_kwh - bc + bd))


def last_available_soc_pct(
    hourly: dict[str, list[float | None]] | None = None,
    series_10min: dict[str, list[float | None]] | None = None,
) -> float | None:
    """Latest SOC % from 10-min series (prefer) or hourly buckets (H23→H0)."""
    if series_10min:
        for v in reversed(series_10min.get("soc") or []):
            if v is not None:
                return float(v)
    if hourly:
        soc_series = hourly.get("soc") or []
        for h in range(len(soc_series) - 1, -1, -1):
            if soc_series[h] is not None:
                return float(soc_series[h])
    return None


def resolve_day_start_soc_kwh(
    *,
    battery_cap: float,
    min_soc_pct: float,
    live_soc_kwh: float | None,
    today_hourly: dict[str, list[float | None]] | None,
    prev_day_hourly: dict[str, list[float | None]] | None,
    prev_day_series_10min: dict[str, list[float | None]] | None = None,
) -> float:
    """Seed midnight SOC for the solid as-if-00:00 day-plan simulation.

    Priority:
      1. Last available SOC from yesterday (10-min series, else hourly H23→H0)
      2. Live MQTT SOC
      3. Today hour-0 Influx backtrack (when H0 reading exists)
    """
    min_kwh = (float(min_soc_pct) / 100.0) * battery_cap
    cap = float(battery_cap)

    prev_pct = last_available_soc_pct(prev_day_hourly, prev_day_series_10min)
    if prev_pct is not None:
        pct = _bound_soc_pct(prev_pct)
        return min(cap, max(0.0, (pct / 100.0) * cap))

    if live_soc_kwh is not None:
        return min(cap, max(0.0, float(live_soc_kwh)))

    backtrack = hour_start_soc_kwh(today_hourly, 0, cap, float(min_soc_pct))
    if backtrack is not None:
        return backtrack

    # Live SOC is expected whenever meters are online; min floor only if callers
    # pass no live and no Influx yet (startup race).
    return min_kwh


def last_q15_soc_pct(slots: list[float | None] | None) -> float | None:
    """Last non-null SOC % in a 96-slot day plan curve."""
    for v in reversed(slots or []):
        if v is not None:
            return float(v)
    return None


def _hourly_slot_empty(hourly: dict[str, list[float | None]], h: int) -> bool:
    """True when all energy slots for this hour are zero or missing."""
    for key in ("pv", "load", "bat_charge", "bat_discharge", "grid_buy", "grid_sell"):
        arr = hourly.get(key) or []
        if h >= len(arr) or arr[h] is None:
            continue
        if abs(float(arr[h])) > 0:
            return False
    return True


def first_history_hour(hourly: dict[str, list[float | None]] | None) -> int:
    """First hour row to show: 00:00 only when hour-0 is all zeros (SA buckets from 01:00)."""
    if not hourly:
        return 0
    return 0 if _hourly_slot_empty(hourly, 0) else 1


def build_h0_carryover_row(
    plan_date: str,
    prev_day_hourly: dict[str, list[float | None]],
    *,
    forecast_pv: float,
    forecast_load: float,
    cfg: dict,
    params: dict[str, float | int],
    rce_price: float | None,
    timer_schedule: str = "",
) -> dict[str, Any] | None:
    """Fallback first row at 00:00–01:00 when smart plan slots are unavailable."""
    hour_dt = datetime.strptime(plan_date, "%Y-%m-%d").replace(hour=0)
    return build_meter_hour_row(
        hour_dt, prev_day_hourly,
        cfg=cfg, params=params, rce_price=rce_price, plan_date=plan_date,
        meter_hour=23,
        production=forecast_pv,
        consumption=forecast_load,
        timer_schedule=timer_schedule,
        require_pv_or_load=False,
        include_q15=False,
        extra_flags={"carryover_hour": True},
    )


def build_completed_history_rows(
    plan_date: str,
    until_hour: int,
    today_hourly: dict[str, list[float | None]],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
    params: dict[str, float | int],
) -> list[dict[str, Any]]:
    """Completed today hours [0, until_hour) from Influx (for collapsed PROD table)."""
    if until_hour <= 0 or not today_hourly:
        return []

    base = datetime.strptime(plan_date, "%Y-%m-%d")
    quarters = quarters_by_date.get(plan_date) or []
    rows: list[dict[str, Any]] = []

    for h in range(0, until_hour):
        dt = base.replace(hour=h)
        c0 = h * 4
        hour_rce_q15 = list(quarters[c0:c0 + 4])
        hour_rce_vals = [
            float(v) for v in hour_rce_q15 if v is not None
        ]
        rce_price = (
            round(sum(hour_rce_vals) / len(hour_rce_vals), 4)
            if hour_rce_vals else None
        )
        row = build_meter_hour_row(
            dt, today_hourly,
            cfg=cfg, params=params, rce_price=rce_price, plan_date=plan_date,
            rce_q15=hour_rce_q15,
            extra_flags={"history_hour": True},
        )
        if row:
            rows.append(row)
    return rows


def _hour_10m_base(hour: int) -> int:
    return hour * SLOTS_PER_HOUR_10M


def _ten_min_slot_present(
    series: list[float | None] | None,
    hour: int,
    slot_i: int,
) -> bool:
    """True when the 10-min bucket exists (not missing) in *series*."""
    if not series or slot_i < 0:
        return False
    idx = _hour_10m_base(hour) + slot_i
    return idx < len(series) and series[idx] is not None


def _ten_min_energy_kwh(
    series: list[float | None] | None,
    hour: int,
    start_slot: int,
    count: int,
    *,
    scale: float = 1.0,
) -> float:
    """kWh from 10-min mean kW buckets within one clock hour."""
    if not series or count <= 0:
        return 0.0
    base = _hour_10m_base(hour)
    total = 0.0
    for i in range(start_slot, start_slot + count):
        idx = base + i
        if idx < len(series) and series[idx] is not None:
            total += float(series[idx]) * TEN_MIN_KWH_PER_KW
    return total * scale


def _elapsed_to_quarter_boundary_kwh(
    series: list[float | None] | None,
    hour: int,
    boundary: int,
    *,
    fn,
) -> float:
    """Cumulative kWh from hour start through :15/:30/:45 (boundary 1/2/3).

    When the next 10-min sample after a :15/:45 mark is present, accumulate with
    a half-slot split (slot0 + 0.5·slot1) so mid-bucket charge enters completed q0.
    """
    if boundary <= 0:
        return 0.0
    if boundary == 1:
        first = fn(series, hour, 0, 1)
        if _ten_min_slot_present(series, hour, 1):
            return first + fn(series, hour, 1, 1, scale=0.5)
        return first * PARTIAL_Q15_SCALE
    if boundary == 2:
        return fn(series, hour, 0, 3)
    if boundary == 3:
        first_half = fn(series, hour, 0, 3)
        if _ten_min_slot_present(series, hour, 4):
            return first_half + fn(series, hour, 3, 1) + fn(
                series, hour, 4, 1, scale=0.5,
            )
        return first_half + fn(series, hour, 3, 1, scale=PARTIAL_Q15_SCALE)
    return 0.0


def _ten_min_grid_import_kwh(
    grid_buy: list[float | None] | None,
    hour: int,
    start_slot: int,
    count: int,
    *,
    scale: float = 1.0,
) -> float:
    """Positive import kWh from 10-min grid_buy (negative kW = import)."""
    if not grid_buy or count <= 0:
        return 0.0
    base = _hour_10m_base(hour)
    total = 0.0
    for i in range(start_slot, start_slot + count):
        idx = base + i
        if idx < len(grid_buy) and grid_buy[idx] is not None:
            v = float(grid_buy[idx])
            if v < 0:
                total += abs(v) * TEN_MIN_KWH_PER_KW
    return total * scale


def _ten_min_grid_export_kwh(
    grid_sell: list[float | None] | None,
    hour: int,
    start_slot: int,
    count: int,
    *,
    scale: float = 1.0,
) -> float:
    """Positive export kWh from 10-min grid_sell (positive kW = export)."""
    if not grid_sell or count <= 0:
        return 0.0
    base = _hour_10m_base(hour)
    total = 0.0
    for i in range(start_slot, start_slot + count):
        idx = base + i
        if idx < len(grid_sell) and grid_sell[idx] is not None:
            v = float(grid_sell[idx])
            if v > 0:
                total += v * TEN_MIN_KWH_PER_KW
    return total * scale


def _actual_kwh_for_quarter(
    series: list[float | None] | None,
    hour: int,
    quarter: int,
    *,
    grid_mode: str | None = None,
) -> float:
    """Cumulative kWh from hour start through boundary after q15 quarter (quarter-1)."""
    if quarter <= 0:
        return 0.0
    if grid_mode is None:
        return _actual_energy_for_quarter(series, hour, quarter)

    if grid_mode == "import":
        fn = _ten_min_grid_import_kwh
    else:
        fn = _ten_min_grid_export_kwh
    return _elapsed_to_quarter_boundary_kwh(series, hour, quarter, fn=fn)


def _elapsed_kwh_through_quarter(
    series: list[float | None] | None,
    hour: int,
    q_inclusive: int,
    *,
    grid_mode: str | None = None,
) -> float:
    """kWh from hour start through end of q15 quarter q_inclusive (0..3)."""
    if q_inclusive < 0:
        return 0.0
    if q_inclusive <= 2:
        return _actual_kwh_for_quarter(
            series, hour, q_inclusive + 1, grid_mode=grid_mode,
        )
    if grid_mode == "import":
        return _ten_min_grid_import_kwh(series, hour, 0, SLOTS_PER_HOUR_10M)
    if grid_mode == "export":
        return _ten_min_grid_export_kwh(series, hour, 0, SLOTS_PER_HOUR_10M)
    return _ten_min_energy_kwh(series, hour, 0, SLOTS_PER_HOUR_10M)


def actual_q15_slice_kwh(
    series: list[float | None] | None,
    hour: int,
    q: int,
    *,
    grid_mode: str | None = None,
) -> float:
    """kWh in one completed q15 quarter from 10-min Influx (matches PV/load blend windows)."""
    if not (0 <= q < Q15_PER_HOUR):
        return 0.0
    end = _elapsed_kwh_through_quarter(series, hour, q, grid_mode=grid_mode)
    start = (
        _elapsed_kwh_through_quarter(series, hour, q - 1, grid_mode=grid_mode)
        if q > 0 else 0.0
    )
    return end - start


def _actual_energy_for_quarter(
    series: list[float | None] | None,
    hour: int,
    quarter: int,
) -> float:
    """Influx 10-min energy accumulated so far in the hour (scaled at :15/:45)."""
    return _elapsed_to_quarter_boundary_kwh(
        series, hour, quarter, fn=_ten_min_energy_kwh,
    )


# 10-min slot weights covering one q15 (1.5 × 10 min = 15 min).
_OPEN_Q15_SLOT_WEIGHTS: tuple[tuple[tuple[int, float], ...], ...] = (
    ((0, 1.0), (1, 0.5)),  # q0 :00-:15
    ((1, 0.5), (2, 1.0)),  # q1 :15-:30
    ((3, 1.0), (4, 0.5)),  # q2 :30-:45
    ((4, 0.5), (5, 1.0)),  # q3 :45-:00
)
_OPEN_Q15_FULL_WEIGHT = 1.5


def _refresh_slot_index(now: datetime, hour: int) -> int:
    """Open/pull q15 index for this hour (-1 = none yet).

    Pulls the just-ended quarter every tick (Influx partial + forecast fill).
    Freezing uses `_freeze_through_index`, not this index.
    """
    if now.hour != hour:
        return -1
    if now.minute < 15:
        return -1
    if now.minute < 30:
        return 0
    if now.minute < 45:
        return 1
    return 2


def _freeze_through_index(now: datetime, hour: int) -> int:
    """Highest q15 index frozen for this hour (-1 = none).

    At :30-:44 freeze q0–q1 (:00-:30). At :45 freeze q0–q1; q2 stays open.
    Previous-hour q2/q3 are handled by merge via `freeze_ready_quarter_tick`.
    """
    if now.hour != hour:
        return -1
    if now.minute < 30:
        return -1
    return 1


def _frozen_q0_kwh(series: list[float | None] | None, hour: int) -> float:
    """q0 energy when only the first 10-min bucket is available (scaled)."""
    if not series:
        return 0.0
    return _ten_min_energy_kwh(series, hour, 0, 1, scale=PARTIAL_Q15_SCALE)


def _weighted_ten_min_kwh(
    series: list[float | None] | None,
    hour: int,
    slot_i: int,
    weight: float,
    *,
    grid_mode: str | None = None,
) -> float:
    if grid_mode == "import":
        return _ten_min_grid_import_kwh(series, hour, slot_i, 1, scale=weight)
    if grid_mode == "export":
        return _ten_min_grid_export_kwh(series, hour, slot_i, 1, scale=weight)
    return _ten_min_energy_kwh(series, hour, slot_i, 1, scale=weight)


def _open_quarter_missing_frac(
    series: list[float | None] | None,
    hour: int,
    q: int,
) -> float:
    """Fraction of the 15-min window still missing from Influx (0..1)."""
    if not (0 <= q < Q15_PER_HOUR):
        return 1.0
    present = 0.0
    for slot_i, weight in _OPEN_Q15_SLOT_WEIGHTS[q]:
        if _ten_min_slot_present(series, hour, slot_i):
            present += weight
    return max(0.0, (_OPEN_Q15_FULL_WEIGHT - present) / _OPEN_Q15_FULL_WEIGHT)


def _open_quarter_partial_kwh(
    series: list[float | None] | None,
    hour: int,
    q: int,
    *,
    grid_mode: str | None = None,
) -> float:
    """Influx energy already available inside one open q15 (no scale invention)."""
    if not (0 <= q < Q15_PER_HOUR) or not series:
        return 0.0
    total = 0.0
    for slot_i, weight in _OPEN_Q15_SLOT_WEIGHTS[q]:
        if _ten_min_slot_present(series, hour, slot_i):
            total += _weighted_ten_min_kwh(
                series, hour, slot_i, weight, grid_mode=grid_mode,
            )
    return total


def _open_quarter_blend_kwh(
    series: list[float | None] | None,
    hour: int,
    q: int,
    forecast_kwh: float,
    *,
    grid_mode: str | None = None,
) -> float:
    """Open tick: influx partial + forecast × (missing_min / 15)."""
    partial = _open_quarter_partial_kwh(series, hour, q, grid_mode=grid_mode)
    missing = _open_quarter_missing_frac(series, hour, q)
    return partial + float(forecast_kwh) * missing


def _open_q15_battery_grid(
    series_10min: dict[str, list[float | None]] | None,
    hour: int,
    q: int,
    *,
    forecast_bat: float,
    forecast_import: float,
    forecast_export: float,
) -> tuple[float, float, float]:
    """Open-tick bat/grid: Influx partial + forecast × missing fraction."""
    s = series_10min or {}
    ref = s.get("pv") or s.get("load") or s.get("grid_buy") or s.get("grid_sell")
    missing = _open_quarter_missing_frac(ref, hour, q) if ref else 1.0

    bat_in = _open_quarter_partial_kwh(s.get("bat_charge"), hour, q)
    bat_out = _open_quarter_partial_kwh(s.get("bat_discharge"), hour, q)
    grid_import = _open_quarter_partial_kwh(
        s.get("grid_buy"), hour, q, grid_mode="import",
    )
    grid_export = _open_quarter_partial_kwh(
        s.get("grid_sell"), hour, q, grid_mode="export",
    )
    bat_delta = bat_in - bat_out
    if abs(bat_delta) < 1e-6:
        pv = _open_quarter_partial_kwh(s.get("pv"), hour, q)
        load = _open_quarter_partial_kwh(s.get("load"), hour, q)
        if (
            abs(pv) > 1e-6
            or abs(load) > 1e-6
            or grid_import > 1e-6
            or grid_export > 1e-6
        ):
            bat_delta = pv - load + grid_import - grid_export

    bat_delta = bat_delta + float(forecast_bat) * missing
    grid_import = grid_import + float(forecast_import) * missing
    grid_export = grid_export + float(forecast_export) * missing
    return round(bat_delta, 4), round(grid_import, 4), round(grid_export, 4)


def _blended_q15_slot_kwh(
    series: list[float | None] | None,
    hour: int,
    q: int,
    now: datetime,
    forecast_q15: list[float] | None,
    hourly_fallback: float,
) -> float:
    """One q15 slot: frozen Influx, open pull (fact+forecast fill), or forecast tail."""
    forecast = _q15_slot_energy(forecast_q15, hour, q, hourly_fallback)
    freeze_through = _freeze_through_index(now, hour)
    pull = _refresh_slot_index(now, hour)

    if freeze_through >= 0 and q <= freeze_through:
        return actual_q15_slice_kwh(series, hour, q)

    if pull >= 0 and q == pull:
        return _open_quarter_blend_kwh(series, hour, q, forecast)

    return forecast


def blended_q15_pv_load_slots(
    hour: int,
    now: datetime,
    *,
    forecast_pv_q15: list[float] | None,
    forecast_load_q15: list[float] | None,
    series_10min: dict[str, list[float | None]] | None,
    pv_hourly: float = 0.0,
    load_hourly: float = 0.0,
) -> list[tuple[float, float]]:
    """Per-q15 blended PV/load (kWh) for the in-progress hour."""
    pv_10 = (series_10min or {}).get("pv")
    load_10 = (series_10min or {}).get("load")
    out: list[tuple[float, float]] = []
    for q in range(Q15_PER_HOUR):
        pv = _blended_q15_slot_kwh(pv_10, hour, q, now, forecast_pv_q15, pv_hourly)
        load = _blended_q15_slot_kwh(load_10, hour, q, now, forecast_load_q15, load_hourly)
        out.append((round(pv, 4), round(load, 4)))
    return out


def hourly_profile_to_q15(hourly: list[float]) -> list[float]:
    """Split hourly kWh into four equal 15-min energy steps (96 slots)."""
    out: list[float] = []
    for v in hourly[:24]:
        quarter = float(v) / Q15_PER_HOUR
        out.extend([quarter] * Q15_PER_HOUR)
    while len(out) < 96:
        out.append(0.0)
    return out[:96]


def _hour_control_from_slot(slot: dict[str, Any]) -> "HourControl":
    from .plan_optimizer import HourControl

    return HourControl(
        grid_charge_kw=float(slot.get("grid_charge_kw") or 0.0),
        battery_export_kwh=float(
            slot.get("ctrl_battery_export_kwh")
            or slot.get("battery_export_kwh")
            or 0.0
        ),
        load_from_grid=bool(slot.get("load_from_grid")),
    )


def _actual_q15_battery_grid(
    series_10min: dict[str, list[float | None]] | None,
    hour: int,
    q: int,
) -> tuple[float, float, float]:
    """Per-q15 battery delta and grid kWh from 10-min Influx (same windows as PV blend).

    When bat_charge/bat_discharge are missing in series_10min, derive battery_delta from
    the hourly energy balance on the same q15 window:

        PV + grid_import + bat_discharge = load + grid_export + bat_charge
        => battery_delta = PV - load + grid_import - grid_export
    """
    s = series_10min or {}
    bat_in = actual_q15_slice_kwh(s.get("bat_charge"), hour, q)
    bat_out = actual_q15_slice_kwh(s.get("bat_discharge"), hour, q)
    grid_import = actual_q15_slice_kwh(
        s.get("grid_buy"), hour, q, grid_mode="import",
    )
    grid_export = actual_q15_slice_kwh(
        s.get("grid_sell"), hour, q, grid_mode="export",
    )
    bat_delta = bat_in - bat_out
    if abs(bat_delta) < 1e-6:
        pv = actual_q15_slice_kwh(s.get("pv"), hour, q)
        load = actual_q15_slice_kwh(s.get("load"), hour, q)
        if (
            abs(pv) > 1e-6
            or abs(load) > 1e-6
            or grid_import > 1e-6
            or grid_export > 1e-6
        ):
            bat_delta = pv - load + grid_import - grid_export
    return round(bat_delta, 4), round(grid_import, 4), round(grid_export, 4)


def _soc_kwh_after_battery_delta(
    soc_kwh: float,
    battery_delta_kwh: float,
    *,
    min_kwh: float,
    battery_cap: float,
) -> float:
    """Integrate meter battery ΔSOC. Do not lift below the plan min floor."""
    del min_kwh
    return min(float(battery_cap), max(0.0, float(soc_kwh) + float(battery_delta_kwh)))


def simulate_blended_current_hour_q15(
    soc_start_kwh: float,
    hour: int,
    now: datetime,
    pv_by_q: list[float],
    load_by_q: list[float],
    opt_slots: list[dict[str, Any]],
    series_10min: dict[str, list[float | None]] | None,
    cfg: dict,
    *,
    sa_timer_txt: str | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """Blended hour: frozen Influx, open pull (fact+forecast), sim tail; SOC chains."""
    from .plan_optimizer import simulate_hour
    from .plan_timer_override import hour_control_from_timer_override
    from .timer_plan import timer_covers_quarter
    from .simulation_config import (
        get_simulation_params,
        plan_min_soc_kwh,
        plan_min_soc_pct,
        plan_timer_discharge_power_kw,
    )

    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = plan_min_soc_pct(cfg)
    min_kwh = plan_min_soc_kwh(cfg)
    discharge_dc_kw = plan_timer_discharge_power_kw(cfg)
    inverter_ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    epsilon = float(params["epsilon_kwh"])
    eps_q = max(epsilon / Q15_PER_HOUR, 0.001)
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])

    freeze_through = _freeze_through_index(now, hour)
    pull = _refresh_slot_index(now, hour)
    soc_kwh = soc_start_kwh
    q15: list[dict[str, Any]] = []

    for q in range(Q15_PER_HOUR):
        pv = float(pv_by_q[q]) if q < len(pv_by_q) else 0.0
        load = float(load_by_q[q]) if q < len(load_by_q) else 0.0

        if freeze_through >= 0 and q <= freeze_through:
            bat_delta, grid_import, grid_export = _actual_q15_battery_grid(
                series_10min, hour, q,
            )
            soc_kwh = _soc_kwh_after_battery_delta(
                soc_kwh, bat_delta, min_kwh=min_kwh, battery_cap=battery_cap,
            )
            meter_pct = meter_soc_pct_for_q15(series_10min, hour, q)
            q15.append({
                "quarter": q,
                "production": round(pv, 4),
                "consumption": round(load, 4),
                "soc": meter_pct if meter_pct is not None else _bound_soc_pct(
                    (soc_kwh / battery_cap) * 100.0
                ),
                "battery": bat_delta,
                "grid_import": grid_import,
                "grid_export": grid_export,
                "from_actual": True,
            })
            if meter_pct is not None:
                soc_kwh = (meter_pct / 100.0) * battery_cap
            continue

        if sa_timer_txt and (
            timer_covers_quarter(sa_timer_txt, hour, q)
            or (
                q > pull >= 0
                and any(
                    float(s.get("grid_export") or 0) > epsilon
                    for s in q15
                    if s.get("from_actual")
                )
            )
        ):
            ctrl = hour_control_from_timer_override(hour, q, sa_timer_txt)
            reserve = None
        else:
            opt = opt_slots[q] if q < len(opt_slots) else {}
            ctrl = _hour_control_from_slot(opt)
            reserve = opt.get("reserve_kwh")
        phys = simulate_hour(
            soc_kwh, pv, load, ctrl,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kw=inverter_ac_kw / Q15_PER_HOUR,
            discharge_dc_cap_kwh=discharge_dc_kw / Q15_PER_HOUR,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            epsilon=eps_q,
            reserve_soc_kwh=float(reserve) if reserve is not None else None,
        )

        if pull >= 0 and q == pull:
            s10 = series_10min or {}
            ref = s10.get("pv") or s10.get("load")
            missing = _open_quarter_missing_frac(ref, hour, q) if ref else 1.0
            if missing < 1e-9:
                bat_delta, grid_import, grid_export = _open_q15_battery_grid(
                    series_10min,
                    hour,
                    q,
                    forecast_bat=0.0,
                    forecast_import=0.0,
                    forecast_export=0.0,
                )
            else:
                # Recover full-quarter forecast from blended PV/load, then fill bat/grid.
                partial_pv = _open_quarter_partial_kwh(s10.get("pv"), hour, q)
                partial_load = _open_quarter_partial_kwh(s10.get("load"), hour, q)
                forecast_pv = (pv - partial_pv) / missing
                forecast_load = (load - partial_load) / missing
                phys_fc = simulate_hour(
                    soc_kwh, forecast_pv, forecast_load, ctrl,
                    battery_cap=battery_cap,
                    min_kwh=min_kwh,
                    ac_cap_kw=inverter_ac_kw / Q15_PER_HOUR,
                    discharge_dc_cap_kwh=discharge_dc_kw / Q15_PER_HOUR,
                    eta_grid=eta_grid,
                    eta_out=eta_out,
                    eta_pv_load=eta_pv_load,
                    eta_pv_grid=eta_pv_grid,
                    eta_pv_battery=eta_pv_battery,
                    epsilon=eps_q,
                    reserve_soc_kwh=float(reserve) if reserve is not None else None,
                )
                bat_delta, grid_import, grid_export = _open_q15_battery_grid(
                    series_10min,
                    hour,
                    q,
                    forecast_bat=phys_fc.battery_delta,
                    forecast_import=phys_fc.grid_import,
                    forecast_export=phys_fc.grid_export,
                )
            soc_kwh = _soc_kwh_after_battery_delta(
                soc_kwh, bat_delta, min_kwh=min_kwh, battery_cap=battery_cap,
            )
            q15.append({
                "quarter": q,
                "production": round(pv, 4),
                "consumption": round(load, 4),
                "soc": _clamp_soc_pct((soc_kwh / battery_cap) * 100.0, min_soc_pct),
                "battery": bat_delta,
                "grid_import": grid_import,
                "grid_export": grid_export,
                "from_actual": False,
            })
            continue

        soc_kwh = phys.soc_end
        q15.append({
            "quarter": q,
            "production": round(pv, 4),
            "consumption": round(load, 4),
            "soc": _clamp_soc_pct((soc_kwh / battery_cap) * 100.0, min_soc_pct),
            "battery": round(phys.battery_delta, 4),
            "grid_import": round(phys.grid_import, 4),
            "grid_export": round(phys.grid_export, 4),
            "from_actual": False,
        })

    return q15, soc_kwh


def simulate_q15_slots(
    soc_start_kwh: float,
    hour: int,
    pv_by_q: list[float],
    load_by_q: list[float],
    opt_slots: list[dict[str, Any]],
    cfg: dict,
) -> tuple[list[dict[str, Any]], float]:
    """Forward sim one hour: blended/forecast PV/load → battery/grid → SOC chain."""
    from .plan_optimizer import simulate_hour
    from .simulation_config import (
        get_simulation_params,
        plan_min_soc_kwh,
        plan_min_soc_pct,
        plan_timer_discharge_power_kw,
    )

    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = plan_min_soc_pct(cfg)
    min_kwh = plan_min_soc_kwh(cfg)
    discharge_dc_kw = plan_timer_discharge_power_kw(cfg)
    inverter_ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    epsilon = float(params["epsilon_kwh"])
    eps_q = max(epsilon / Q15_PER_HOUR, 0.001)
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])

    soc_kwh = soc_start_kwh
    q15: list[dict[str, Any]] = []
    for q in range(Q15_PER_HOUR):
        pv = float(pv_by_q[q]) if q < len(pv_by_q) else 0.0
        load = float(load_by_q[q]) if q < len(load_by_q) else 0.0
        opt = opt_slots[q] if q < len(opt_slots) else {}
        ctrl = _hour_control_from_slot(opt)
        reserve = opt.get("reserve_kwh")
        phys = simulate_hour(
            soc_kwh, pv, load, ctrl,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kw=inverter_ac_kw / Q15_PER_HOUR,
            discharge_dc_cap_kwh=discharge_dc_kw / Q15_PER_HOUR,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            epsilon=eps_q,
            reserve_soc_kwh=float(reserve) if reserve is not None else None,
        )
        soc_kwh = phys.soc_end
        q15.append({
            "quarter": q,
            "production": round(pv, 4),
            "consumption": round(load, 4),
            "soc": _clamp_soc_pct((soc_kwh / battery_cap) * 100.0, min_soc_pct),
            "battery": round(phys.battery_delta, 4),
            "grid_import": round(phys.grid_import, 4),
            "grid_export": round(phys.grid_export, 4),
        })
    return q15, soc_kwh


def apply_open_pull_quarter_to_row(
    row: dict[str, Any],
    hour: int,
    quarter: int,
    *,
    series_10min: dict[str, list[float | None]] | None,
    cfg: dict,
) -> bool:
    """Refresh one open quarter from Influx + forecast fill; keep from_actual=False.

    Used for previous-hour q3 at :00 (freeze at :15). Skips already-frozen slots.
    Forecast is the slot's current production/consumption/battery/grid values.
    """
    if not (0 <= quarter < Q15_PER_HOUR):
        return False

    q15 = list(row.get("q15") or [])
    while len(q15) < Q15_PER_HOUR:
        prev_soc = q15[-1].get("soc", 0.0) if q15 else 0.0
        q15.append({
            "quarter": len(q15),
            "production": 0.0,
            "consumption": 0.0,
            "soc": prev_soc,
            "battery": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "from_actual": False,
        })
    slot = q15[quarter]
    if slot.get("from_actual"):
        return False

    from .simulation_config import plan_min_soc_kwh, plan_min_soc_pct

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = plan_min_soc_pct(cfg)
    min_kwh = plan_min_soc_kwh(cfg)

    forecast_pv = float(slot.get("production") or 0.0)
    forecast_load = float(slot.get("consumption") or 0.0)
    forecast_bat = float(slot.get("battery") or 0.0)
    forecast_gi = float(slot.get("grid_import") or 0.0)
    forecast_ge = float(slot.get("grid_export") or 0.0)

    s10 = series_10min or {}
    pv = _open_quarter_blend_kwh(s10.get("pv"), hour, quarter, forecast_pv)
    load = _open_quarter_blend_kwh(s10.get("load"), hour, quarter, forecast_load)
    bat_delta, grid_import, grid_export = _open_q15_battery_grid(
        series_10min,
        hour,
        quarter,
        forecast_bat=forecast_bat,
        forecast_import=forecast_gi,
        forecast_export=forecast_ge,
    )

    if quarter > 0:
        soc_start_kwh = (float(q15[quarter - 1].get("soc") or 0) / 100.0) * battery_cap
    else:
        soc_start_kwh = (float(slot.get("soc") or 0) / 100.0) * battery_cap - bat_delta

    soc_kwh = _soc_kwh_after_battery_delta(
        soc_start_kwh, bat_delta, min_kwh=min_kwh, battery_cap=battery_cap,
    )
    q15[quarter] = {
        "quarter": quarter,
        "production": round(pv, 4),
        "consumption": round(load, 4),
        "soc": _clamp_soc_pct((soc_kwh / battery_cap) * 100.0, min_soc_pct),
        "battery": bat_delta,
        "grid_import": grid_import,
        "grid_export": grid_export,
        "from_actual": False,
    }
    apply_q15_physics_to_row(row, q15)
    refresh_row_grid_cash(row, cfg)
    return True


def blend_current_hour_end(
    hour: int,
    now: datetime,
    *,
    forecast_pv_q15: list[float] | None,
    forecast_load_q15: list[float] | None,
    forecast_pv_hourly: float,
    forecast_load_hourly: float,
    series_10min: dict[str, list[float | None]] | None,
) -> tuple[float, float]:
    """Project PV/Load kWh for the in-progress hour (sum of per-q15 blended slots)."""
    slots = blended_q15_pv_load_slots(
        hour,
        now,
        forecast_pv_q15=forecast_pv_q15,
        forecast_load_q15=forecast_load_q15,
        series_10min=series_10min,
        pv_hourly=float(forecast_pv_hourly),
        load_hourly=float(forecast_load_hourly),
    )
    return round(sum(s[0] for s in slots), 3), round(sum(s[1] for s in slots), 3)


def _bound_soc_pct(pct: float) -> float:
    """Bound meter/display SOC to 0–100%. Do not lift to the plan min floor."""
    return round(max(0.0, min(100.0, float(pct))), 1)


def _clamp_soc_pct(pct: float, min_soc_pct: float) -> float:
    """Bound SOC to 0–100% for EA display and meter freeze.

    *min_soc_pct* is unused — plan min is a discharge reserve, not an inverter
    SOC floor. Meter readings below the plan min stay as reported.
    """
    del min_soc_pct
    return _bound_soc_pct(pct)


def meter_soc_pct_for_q15(
    series_10min: dict[str, list[float | None]] | None,
    hour: int,
    quarter: int,
    *,
    today_hourly: dict[str, list[float | None]] | None = None,
) -> float | None:
    """Inverter SOC % at the end of a 15-min slot (10-min series, else hourly)."""
    if not (0 <= int(hour) < 24 and 0 <= int(quarter) < Q15_PER_HOUR):
        return None
    arr = (series_10min or {}).get("soc") or []
    end_min = (int(quarter) + 1) * 15
    # Prefer the sample at the quarter end when the 10-min grid lands on it (:30).
    if end_min % 10 == 0:
        preferred = end_min // 10
    else:
        preferred = (end_min - 1) // 10
    fallback = (end_min - 1) // 10
    for ten_idx in (preferred, fallback):
        ten_idx = min(SLOTS_PER_HOUR_10M - 1, max(0, ten_idx))
        idx = int(hour) * SLOTS_PER_HOUR_10M + ten_idx
        if 0 <= idx < len(arr) and arr[idx] is not None:
            return _bound_soc_pct(float(arr[idx]))
    hourly = (today_hourly or {}).get("soc") or []
    if int(quarter) == Q15_PER_HOUR - 1 and int(hour) < len(hourly) and hourly[hour] is not None:
        return _bound_soc_pct(float(hourly[hour]))
    return None


def overlay_meter_soc_on_rows(
    rows: list[dict[str, Any]] | None,
    *,
    today_str: str,
    today_hourly: dict[str, list[float | None]] | None,
    series_10min: dict[str, list[float | None]] | None,
    current_hour: int | None = None,
) -> None:
    """Write Influx/inverter SOC onto EA rows. Timers and energy stay unchanged."""
    if not rows:
        return
    for row in rows:
        if str(row.get("plan_date") or "") != today_str:
            continue
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= hour < 24):
            continue
        q15 = row.get("q15") or []
        history = bool(row.get("history_hour"))
        last = None
        for slot in q15:
            try:
                q = int(slot.get("quarter", 0))
            except (TypeError, ValueError):
                continue
            if current_hour is not None and hour == current_hour and not history:
                if not slot.get("from_actual"):
                    continue
            elif current_hour is not None and hour > current_hour:
                continue
            pct = meter_soc_pct_for_q15(
                series_10min, hour, q, today_hourly=today_hourly,
            )
            if pct is None:
                continue
            slot["soc"] = pct
            last = pct
        if last is not None:
            row["soc"] = last


def _q15_hour_energy_slots(q15: list[float] | None, hour: int) -> list[float]:
    """Four merged q15 kWh values for one hour (replay always passes full q15)."""
    if not q15:
        return [0.0] * Q15_PER_HOUR
    base = hour * Q15_PER_HOUR
    return [
        float(q15[base + q]) if base + q < len(q15) else 0.0
        for q in range(Q15_PER_HOUR)
    ]


def _q15_slot_energy(
    q15: list[float] | None,
    hour: int,
    quarter: int,
    hourly_fallback: float,
) -> float:
    idx = hour * Q15_PER_HOUR + quarter
    if q15 and 0 <= idx < len(q15):
        return float(q15[idx])
    return float(hourly_fallback) / Q15_PER_HOUR


def build_history_hour_q15(
    hour: int,
    hourly: dict[str, list[float | None]],
    *,
    battery_cap: float,
    min_soc_pct: float,
    bat_in_kwh: float,
    bat_out_kwh: float,
    grid_import: float,
    grid_export: float,
) -> list[dict[str, Any]]:
    """Completed hour: equal q15 energy split; SOC linear between hour boundaries."""
    pv_h = float(_hourly_slot(hourly, hour, "pv") or 0.0)
    load_h = float(_hourly_slot(hourly, hour, "load") or 0.0)
    soc_end = _hourly_slot(hourly, hour, "soc")
    soc_end_pct = (
        _bound_soc_pct(float(soc_end))
        if soc_end is not None
        else None
    )
    if hour > 0:
        soc_prev = _hourly_slot(hourly, hour - 1, "soc")
        soc_start_pct = (
            _bound_soc_pct(float(soc_prev))
            if soc_prev is not None
            else soc_end_pct
        )
    else:
        soc_start_pct = soc_end_pct
    if soc_end_pct is None and soc_start_pct is None:
        soc_end_pct = 0.0
        soc_start_pct = 0.0
    elif soc_end_pct is None:
        soc_end_pct = soc_start_pct
    elif soc_start_pct is None:
        soc_start_pct = soc_end_pct

    battery_delta = bat_in_kwh - bat_out_kwh
    q15: list[dict[str, Any]] = []
    for q in range(Q15_PER_HOUR):
        t = (q + 1) / Q15_PER_HOUR
        soc = soc_start_pct + (soc_end_pct - soc_start_pct) * t
        q15.append({
            "quarter": q,
            "production": round(pv_h / Q15_PER_HOUR, 4),
            "consumption": round(load_h / Q15_PER_HOUR, 4),
            "soc": _bound_soc_pct(soc),
            "battery": round(battery_delta / Q15_PER_HOUR, 4),
            "grid_import": round(grid_import / Q15_PER_HOUR, 4),
            "grid_export": round(grid_export / Q15_PER_HOUR, 4),
        })
    return q15


def ea_q15_from_optimizer_slots(
    slots: list[dict[str, Any]],
    battery_cap: float,
    *,
    min_soc_pct: float = 15.0,
) -> list[dict[str, Any]]:
    """Convert optimizer q15 slots to Energy arbitrage row storage."""
    out: list[dict[str, Any]] = []
    for slot in slots:
        q = int(slot.get("quarter", len(out)))
        out.append({
            "quarter": q,
            "production": round(float(slot.get("pv") or 0), 4),
            "consumption": round(float(slot.get("load") or 0), 4),
            "soc": _clamp_soc_pct(float(slot.get("soc_pct") or 0), min_soc_pct),
            "battery": round(float(slot.get("battery_delta") or 0), 4),
            "grid_import": round(float(slot.get("grid_import") or 0), 4),
            "grid_export": round(float(slot.get("grid_export") or 0), 4),
        })
    while len(out) < Q15_PER_HOUR:
        out.append({
            "quarter": len(out),
            "production": 0.0,
            "consumption": 0.0,
            "soc": out[-1]["soc"] if out else 0.0,
            "battery": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
        })
    return out[:Q15_PER_HOUR]


def build_blended_current_hour_q15(
    hour: int,
    now: datetime,
    *,
    forecast_pv_q15: list[float] | None,
    forecast_load_q15: list[float] | None,
    series_10min: dict[str, list[float | None]] | None,
    soc_start_kwh: float,
    opt_slots: list[dict[str, Any]],
    cfg: dict,
    pv_hourly: float = 0.0,
    load_hourly: float = 0.0,
    sa_timer_txt: str | None = None,
) -> list[dict[str, Any]]:
    """q15 for in-progress hour: blended PV/load per slot → sim chain for bat/grid/SOC."""
    slots = blended_q15_pv_load_slots(
        hour,
        now,
        forecast_pv_q15=forecast_pv_q15,
        forecast_load_q15=forecast_load_q15,
        series_10min=series_10min,
        pv_hourly=pv_hourly,
        load_hourly=load_hourly,
    )
    pv_by_q = [s[0] for s in slots]
    load_by_q = [s[1] for s in slots]
    q15, _ = simulate_blended_current_hour_q15(
        soc_start_kwh,
        hour,
        now,
        pv_by_q,
        load_by_q,
        opt_slots,
        series_10min,
        cfg,
        sa_timer_txt=sa_timer_txt,
    )
    return q15


def apply_q15_physics_to_row(row: dict[str, Any], q15: list[dict[str, Any]]) -> None:
    """Refresh hourly energy columns from q15 sums (PV/load/battery/grid/SOC)."""
    row["q15"] = q15
    row["production"] = round(sum(float(s.get("production") or 0) for s in q15), 3)
    row["consumption"] = round(sum(float(s.get("consumption") or 0) for s in q15), 3)
    row["battery"] = round(sum(float(s.get("battery") or 0) for s in q15), 3)
    row["bat_charge"] = round(sum(max(0.0, float(s.get("battery") or 0)) for s in q15), 3)
    row["bat_discharge"] = round(sum(max(0.0, -float(s.get("battery") or 0)) for s in q15), 3)
    row["grid_import"] = round(sum(float(s.get("grid_import") or 0) for s in q15), 3)
    row["grid_export"] = round(sum(float(s.get("grid_export") or 0) for s in q15), 3)
    if q15:
        row["soc"] = q15[-1].get("soc")


def refresh_row_grid_cash(
    row: dict[str, Any],
    cfg: dict,
    *,
    epsilon: float | None = None,
) -> None:
    """Recompute import/export cash columns from row grid flows."""
    from .plan_cost import hour_grid_cash_pln, infer_battery_export_kwh
    from .simulation_config import get_simulation_params

    if epsilon is None:
        epsilon = float(get_simulation_params(cfg)["epsilon_kwh"])

    batt_exp = infer_battery_export_kwh(
        float(row.get("grid_export") or 0),
        float(row.get("battery") or 0),
        epsilon=epsilon,
        bat_discharge=float(row.get("bat_discharge") or 0),
    )
    cash = hour_grid_cash_pln(
        float(row.get("grid_import") or 0),
        float(row.get("grid_export") or 0),
        float(row.get("buy_price") or 0),
        row.get("rce_price"),
        cfg,
        battery_export=batt_exp,
        g12_zone=str(row.get("g12_zone") or "offpeak"),
    )
    row["import_cost"] = cash["import_cost"]
    row["export_revenue"] = cash["export_revenue"]
    row["energy_cost"] = cash["energy_cost"]
    row["service_cost"] = cash["service_cost"]
    row["cost"] = cash["cost"]
    row["export_credit"] = cash["export_credit"]
    row["export_planned"] = float(row.get("grid_export") or 0) >= epsilon


def sync_blended_current_hour_row(
    row: dict[str, Any],
    q15: list[dict[str, Any]],
    *,
    production: float,
    consumption: float,
    soc: float,
    cfg: dict,
    epsilon: float,
    hour: int | None = None,
    opt_slots: list[dict[str, Any]] | None = None,
    sa_timer_txt: str | None = None,
    now: datetime | None = None,
) -> None:
    """Apply blended q15 battery/grid to an in-progress EA row; keep display PV/load/SOC."""
    from .timer_plan import (
        classify_action,
    )

    apply_q15_physics_to_row(row, q15)
    row["production"] = round(production, 3)
    row["consumption"] = round(consumption, 3)
    row["soc"] = round(soc, 1)
    row["soc_blended"] = True

    refresh_row_grid_cash(row, cfg, epsilon=epsilon)
    row["action"] = classify_action(
        bat_charge=float(row.get("bat_charge") or 0),
        bat_discharge=float(row.get("bat_discharge") or 0),
        grid_import=float(row.get("grid_import") or 0),
        grid_export=float(row.get("grid_export") or 0),
        production=float(row.get("production") or 0),
        epsilon=epsilon,
    )
    if row.get("timer_schedule_manual"):
        return
    # IMPORTANT: Timer Schedule for the current hour must not change mid-hour.
    # It is computed at :00 and then stays frozen for display until the hour ends.
    return


def _ea_q15_slots_for_timer(
    q15: list[dict[str, Any]],
    opt_slots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map EA q15 rows to optimizer-slot shape for timer derivation."""
    out: list[dict[str, Any]] = []
    for i, q in enumerate(q15):
        opt = opt_slots[i] if i < len(opt_slots) else {}
        out.append({
            "quarter": int(q.get("quarter", i)),
            "pv": float(q.get("production") or 0),
            "load": float(q.get("consumption") or 0),
            "battery_delta": float(q.get("battery") or 0),
            "grid_import": float(q.get("grid_import") or 0),
            "grid_export": float(q.get("grid_export") or 0),
            "soc_pct": q.get("soc"),
            "reserve_kwh": opt.get("reserve_kwh"),
        })
    return out


def _refresh_row_labels_from_replay(
    row: dict[str, Any],
    q15: list[dict[str, Any]],
    opt_slots: list[dict[str, Any]],
    *,
    cfg: dict,
    epsilon: float,
) -> None:
    """Recompute action/timer from replayed q15 (skip locked/manual timer)."""
    hour = int(row.get("hour", 0))
    row["action"] = classify_action(
        bat_charge=float(row.get("bat_charge") or 0),
        bat_discharge=float(row.get("bat_discharge") or 0),
        grid_import=float(row.get("grid_import") or 0),
        grid_export=float(row.get("grid_export") or 0),
        production=float(row.get("production") or 0),
        epsilon=epsilon,
    )
    if row.get("timer_schedule_manual") or row.get("hour_labels_locked"):
        return
    timer_slots = _ea_q15_slots_for_timer(q15, opt_slots)
    row["timer_schedule"] = build_hour_timer_schedule(
        hour,
        timer_slots,
        cfg,
        action=row["action"],
        grid_export=float(row.get("grid_export") or 0),
        bat_charge=float(row.get("bat_charge") or 0),
        epsilon=epsilon,
    )


def replay_forward_soc_on_rows(
    rows: list[dict[str, Any]],
    *,
    anchor_soc_kwh: float,
    q15_plan_by_date: dict[str, dict[int, list[dict[str, Any]]]],
    pv_q15_by_date: dict[str, list[float] | None],
    load_q15_by_date: dict[str, list[float] | None],
    cfg: dict,
) -> None:
    """Forward replay from anchor SOC: merged q15 PV/load → sim chain."""
    from .simulation_config import get_simulation_params

    if not rows:
        return

    epsilon = float(get_simulation_params(cfg)["epsilon_kwh"])
    soc_kwh = anchor_soc_kwh
    for row in rows:
        date_key = str(row.get("plan_date") or "")
        hour = int(row.get("hour", 0))
        plan = q15_plan_by_date.get(date_key) or {}
        opt_slots = plan.get(hour) or []
        pv_q15 = pv_q15_by_date.get(date_key)
        load_q15 = load_q15_by_date.get(date_key)

        pv_by_q = _q15_hour_energy_slots(pv_q15, hour)
        load_by_q = _q15_hour_energy_slots(load_q15, hour)
        q15_out, soc_kwh = simulate_q15_slots(
            soc_kwh, hour, pv_by_q, load_by_q, opt_slots, cfg,
        )
        apply_q15_physics_to_row(row, q15_out)
        refresh_row_grid_cash(row, cfg, epsilon=epsilon)
        _refresh_row_labels_from_replay(
            row, q15_out, opt_slots, cfg=cfg, epsilon=epsilon,
        )


def apply_current_hour_blend(
    pv_hourly: list[float],
    load_hourly: list[float],
    hour: int,
    now: datetime,
    *,
    forecast_pv_q15: list[float] | None,
    forecast_load_q15: list[float] | None,
    series_10min: dict[str, list[float | None]] | None,
) -> tuple[list[float], list[float]]:
    """Patch *pv_hourly*/*load_hourly* at *hour* with blended end-of-hour estimates."""
    if not (0 <= hour < len(pv_hourly)):
        return pv_hourly, load_hourly
    pv_b, load_b = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=forecast_pv_q15,
        forecast_load_q15=forecast_load_q15,
        forecast_pv_hourly=float(pv_hourly[hour]),
        forecast_load_hourly=float(load_hourly[hour]),
        series_10min=series_10min,
    )
    pv_out = list(pv_hourly)
    load_out = list(load_hourly)
    pv_out[hour] = pv_b
    load_out[hour] = load_b
    return pv_out, load_out
