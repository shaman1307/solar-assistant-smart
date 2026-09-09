"""Overnight survive reserve and charge-target walks (through next-day PV)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .g12_pricing import get_buy_price
from .plan_physics import (
    HourControl,
    eps_step_kwh,
    pv_load_energy_split,
    slots_per_hour_from_scale,
)

HOURS_PER_DAY = 24
# Calendar AM bound only when that day has no peak hours at all (e.g. G12w weekend).
_ALL_OFFPEAK_COVER_HOUR_END = 12

def morning_cover_bound_from_hour_buys(
    hour_buys: list[float],
    *,
    offpeak_buy: float,
    epsilon: float = 0.0,
) -> int | None:
    """Exclusive clock hour when daytime PV cover may end overnight need.

    Derived from this calendar day's buy prices (not hardcoded 6–13):
    - two+ peak blocks → end of the first (morning) block;
    - one block starting before noon → its end;
    - one block starting late (evening only, e.g. truncated series) → its start
      so evening PV does not look like morning cover;
    - all offpeak (G12w weekend) → None.
    """
    n = min(HOURS_PER_DAY, len(hour_buys))
    if n <= 0:
        return None
    off = float(offpeak_buy)
    eps = float(epsilon)
    is_peak = [float(hour_buys[h]) > off + eps for h in range(n)]
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not is_peak[i]:
            i += 1
            continue
        j = i + 1
        while j < n and is_peak[j]:
            j += 1
        blocks.append((i, j))
        i = j
    if not blocks:
        return None
    if len(blocks) >= 2:
        return blocks[0][1]
    start, end = blocks[0]
    if start < _ALL_OFFPEAK_COVER_HOUR_END:
        return end
    # Single late block only (evening peak visible; morning absent from series).
    return start


def _day_hour_buys_from_series(
    buy_series: list[float] | None,
    day_index: int,
    *,
    slots_per_hour: int,
    global_step_offset: int,
    offpeak_buy: float,
) -> list[float]:
    """24 hourly buy samples for calendar day_index in the step timeline."""
    slots_per_day = HOURS_PER_DAY * slots_per_hour
    off = float(offpeak_buy)
    out: list[float] = []
    for h in range(HOURS_PER_DAY):
        global_step = day_index * slots_per_day + h * slots_per_hour
        si = global_step - global_step_offset
        if buy_series is None or si < 0 or si >= len(buy_series):
            out.append(off)
        else:
            out.append(float(buy_series[si]))
    return out


def pv_cover_ends_overnight_need(
    *,
    local_hour: int,
    start_local_hour: int,
    crossed_midnight: bool,
    seen_insufficient: bool,
    cover_bound: int | None,
) -> bool:
    """Whether a PV-cover hour ends the overnight need walk.

    Evening-started walks (from noon onward) must reach the *next* calendar
    day's first PV-cover hour — same-day afternoon/evening sun must not end
    the survive-until-morning reserve.
    """
    started_evening = int(start_local_hour) >= _ALL_OFFPEAK_COVER_HOUR_END
    if seen_insufficient:
        if crossed_midnight:
            return True
        # Still today: evening-started walks keep going through tonight.
        if started_evening:
            return False
        if cover_bound is not None:
            return int(local_hour) < int(cover_bound)
        # All-offpeak day: only a morning-started walk may stop before midnight.
        return int(local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
    # Already self-sufficient — no overnight gap ahead today.
    if started_evening:
        return False
    if cover_bound is not None:
        return (
            int(start_local_hour) < int(cover_bound)
            and int(local_hour) < int(cover_bound)
        )
    return (
        int(start_local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
        and int(local_hour) < _ALL_OFFPEAK_COVER_HOUR_END
    )

def reserve_soc_kwh_from_step(
    step: int,
    pv_series: list[float],
    load_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    *,
    buy_series: list[float] | None = None,
    offpeak_buy: float | None = None,
    slots_per_hour: int = 4,
    global_step_offset: int = 0,
) -> float:
    """Battery kWh to keep after *step* for self-use until morning PV covers house.

    Sums load deficits from step+1 until the first next-day hour where PV covers
    load (evening-started walks must cross midnight), then adds
    *reserve_floor_kwh* (min SOC). At the end of today's last discharge hour this
    is the SOC that must remain in the battery.

    Does **not** by itself justify grid→battery charging — see
    `grid_charge_target_soc_kwh_from_step`.
    """
    return _forward_soc_need_from_step(
        step, pv_series, load_series, reserve_floor_kwh, eta_out, eta_pv_load, epsilon,
        buy_series=buy_series, offpeak_buy=offpeak_buy, peak_deficits_only=False,
        slots_per_hour=slots_per_hour, global_step_offset=global_step_offset,
    )


def apply_post_discharge_reserve_floor(
    reserves: list[float],
    controls: list[HourControl],
    *,
    pv_series: list[float],
    load_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    rce_step_offset: int,
    slots_per_hour: int,
    buy_series: list[float] | None = None,
    offpeak_buy: float | None = None,
) -> tuple[list[float], float | None]:
    """Raise per-hour Dis reserves to the survive floor *if that hour were last*.

    For each export hour H in a contiguous run, steps in H must leave
    ``post_discharge_reserve_soc_kwh(H)`` (load after H until next-day PV cover
    + min SOC). A later hour in the run may go lower — its own post_dis(H) —
    so chrono fill can sell leftover above post_dis(prev) without shaving the
    overnight stock below the true end-of-window need. Mid-hour Dis tails also
    raise the last export step via ``reserve_soc_kwh_from_step``.

    Returns (reserves, end_floor_kwh or None when no export).
    """
    if not controls or not reserves:
        return list(reserves), None
    eps = float(epsilon)
    slots = max(1, int(slots_per_hour))
    export_steps = [
        i for i, c in enumerate(controls)
        if i < len(reserves) and c.battery_export_kwh > eps
    ]
    if not export_steps:
        return list(reserves), None

    export_hours = sorted({
        (rce_step_offset + i) // slots for i in export_steps
    })
    out = list(reserves)
    global_floor: float | None = None

    for h in export_hours:
        hour_steps = [
            i for i in export_steps
            if (rce_step_offset + i) // slots == h
        ]
        if not hour_steps:
            continue
        floor_h = post_discharge_reserve_soc_kwh(
            h,
            pv_series,
            load_series,
            reserve_floor_kwh,
            eta_out,
            eta_pv_load,
            eps,
            buy_series=buy_series,
            offpeak_buy=offpeak_buy,
            slots_per_hour=slots,
            global_step_offset=rce_step_offset,
        )
        last_step = max(hour_steps)
        floor_tail = reserve_soc_kwh_from_step(
            last_step,
            pv_series,
            load_series,
            reserve_floor_kwh,
            eta_out,
            eta_pv_load,
            eps,
            buy_series=buy_series,
            offpeak_buy=offpeak_buy,
            slots_per_hour=slots,
            global_step_offset=rce_step_offset,
        )
        end_floor = max(float(floor_h), float(floor_tail))
        # Window end target tracks the last export hour processed (sorted).
        global_floor = end_floor
        for i in range(len(out)):
            if (rce_step_offset + i) // slots == h and i <= last_step and out[i] < end_floor:
                out[i] = end_floor

    return out, global_floor


def post_discharge_reserve_soc_kwh(
    last_discharge_hour: int,
    pv_series: list[float],
    load_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    *,
    buy_series: list[float] | None = None,
    offpeak_buy: float | None = None,
    slots_per_hour: int = 4,
    global_step_offset: int = 0,
) -> float:
    """SOC to leave at end of a full-hour Dis (survive until next-day PV).

    Counts house deficits from the first hour *after* *last_discharge_hour*
    through the first next-day hour that covers load, then adds min-SOC floor.
    Mid-hour Dis ends use ``apply_post_discharge_reserve_floor`` (last export
    step) so load after the Dis window stays inside the budget.
    """
    slots = max(1, int(slots_per_hour))
    # from_step = last q15 of last_discharge_hour → walk starts at next hour.
    last_global = int(last_discharge_hour) * slots + (slots - 1)
    from_step = last_global - int(global_step_offset)
    if from_step < -1:
        return float(reserve_floor_kwh)
    return reserve_soc_kwh_from_step(
        max(-1, from_step),
        pv_series,
        load_series,
        reserve_floor_kwh,
        eta_out,
        eta_pv_load,
        epsilon,
        buy_series=buy_series,
        offpeak_buy=offpeak_buy,
        slots_per_hour=slots,
        global_step_offset=global_step_offset,
    )


def grid_charge_target_soc_kwh_from_step(
    step: int,
    pv_series: list[float],
    load_series: list[float],
    buy_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    offpeak_buy: float,
    *,
    slots_per_hour: int = 4,
    global_step_offset: int = 0,
) -> float:
    """SOC worth buying from the grid: floor + future *peak* house deficits only.

    Offpeak deficits are not part of the *purchase* budget — reserve/discharge
    already plans to avoid offpeak import (including midnight→morning peak
    selection). Grid→battery covers only missing kWh for the nearest peak hours
    until PV covers within the morning tariff horizon.
    Weekend / all-offpeak: floor only.
    """
    hour_buys = _day_hour_buys_from_series(
        buy_series,
        (global_step_offset + step) // max(1, slots_per_hour * HOURS_PER_DAY),
        slots_per_hour=slots_per_hour,
        global_step_offset=global_step_offset,
        offpeak_buy=offpeak_buy,
    )
    if morning_cover_bound_from_hour_buys(
        hour_buys, offpeak_buy=offpeak_buy, epsilon=epsilon,
    ) is None:
        return float(reserve_floor_kwh)

    return _forward_soc_need_from_step(
        step, pv_series, load_series, reserve_floor_kwh, eta_out, eta_pv_load, epsilon,
        buy_series=buy_series, offpeak_buy=offpeak_buy, peak_deficits_only=True,
        slots_per_hour=slots_per_hour, global_step_offset=global_step_offset,
    )


def _forward_soc_need_from_step(
    step: int,
    pv_series: list[float],
    load_series: list[float],
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    *,
    buy_series: list[float] | None,
    offpeak_buy: float | None,
    peak_deficits_only: bool,
    slots_per_hour: int = 4,
    global_step_offset: int = 0,
) -> float:
    """Walk forward until PV covers load in that day's tariff morning horizon.

    Cover bound comes from each calendar day's buy prices (morning peak end,
    or evening-peak start, or all-offpeak weekend). Afternoon PV cover must
    not end the walk before tonight's deficits. With peak_deficits_only, only
    peak-priced deficits are summed.
    """
    need = 0.0
    j = step + 1
    slots_per_hour = max(1, slots_per_hour)
    slots_per_day = HOURS_PER_DAY * slots_per_hour
    start_day = (global_step_offset + step) // slots_per_day
    start_local_hour = ((global_step_offset + step) % slots_per_day) // slots_per_hour
    off = float(offpeak_buy or 0.0)
    bound_cache: dict[int, int | None] = {}
    seen_insufficient_hour = False

    def cover_bound_for_day(day_index: int) -> int | None:
        if day_index not in bound_cache:
            hour_buys = _day_hour_buys_from_series(
                buy_series, day_index,
                slots_per_hour=slots_per_hour,
                global_step_offset=global_step_offset,
                offpeak_buy=off,
            )
            bound_cache[day_index] = morning_cover_bound_from_hour_buys(
                hour_buys, offpeak_buy=off, epsilon=epsilon,
            )
        return bound_cache[day_index]

    while j < len(pv_series):
        deficit, _ = pv_load_energy_split(
            pv_series[j], load_series[j], eta_pv_load=eta_pv_load,
        )
        if deficit > epsilon:
            count = True
            if peak_deficits_only:
                buy_p = float(buy_series[j]) if buy_series is not None and j < len(buy_series) else 0.0
                count = buy_p > off + epsilon
            if count:
                need += deficit / eta_out if eta_out > 0 else deficit
            seen_insufficient_hour = True
        j += 1
        if j % slots_per_hour == 0:
            h_start = j - slots_per_hour
            pv_h = sum(pv_series[h_start:j])
            load_h = sum(load_series[h_start:j])
            if pv_h * eta_pv_load < load_h - epsilon:
                continue
            hour_day = (global_step_offset + j - 1) // slots_per_day
            local_hour = ((global_step_offset + j - 1) % slots_per_day) // slots_per_hour
            crossed_midnight = hour_day > start_day
            if pv_cover_ends_overnight_need(
                local_hour=local_hour,
                start_local_hour=start_local_hour,
                crossed_midnight=crossed_midnight,
                seen_insufficient=seen_insufficient_hour,
                cover_bound=cover_bound_for_day(hour_day),
            ):
                break
    return need + reserve_floor_kwh

def _should_extend_forecast_lookahead(
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
) -> bool:
    """True when q15 reserve/charge should append forecast through end of tomorrow."""
    forecast_data = forecast or {}
    tomorrow = (forecast_data.get("tomorrow") or {})
    if step_scale >= 1.0:
        return False
    if not tomorrow.get("pv") or not tomorrow.get("load"):
        return False
    tomorrow_date = today_date + timedelta(days=1)
    return end_dt.date() <= tomorrow_date


def tomorrow_lookahead_start_hour(
    *,
    end_dt: datetime,
    today_date,
    series_len: int = 0,
    global_step_offset: int = 0,
    step_scale: float = 0.25,
) -> int | None:
    """First clock hour of tomorrow not yet in the optimized series (0..23), or None.

    Prefer series coverage (offset + length) over *end_dt* alone so a mismatched
    end timestamp cannot invent or skip tomorrow hours.
    """
    tomorrow_date = today_date + timedelta(days=1)
    slots = slots_per_hour_from_scale(step_scale)
    if series_len > 0:
        last_abs_hour = (global_step_offset + series_len - 1) // slots
        if last_abs_hour < HOURS_PER_DAY:
            # Series still on today — classic overnight append when end is today.
            if end_dt.date() == today_date:
                return 0
            return None
        last_tom_h = last_abs_hour - HOURS_PER_DAY
        nxt = last_tom_h + 1
        return nxt if nxt < HOURS_PER_DAY else None

    # Fallback when callers omit series length: end_dt marks the last plan slot.
    if end_dt.date() == today_date:
        return 0
    if end_dt.date() == tomorrow_date:
        nxt = int(end_dt.hour) + 1
        if nxt >= HOURS_PER_DAY:
            return None
        return nxt
    return None


def build_extended_pv_load_for_reserve(
    pv_series: list[float],
    load_series: list[float],
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
    global_step_offset: int = 0,
) -> tuple[list[float], list[float]]:
    """Append PV/load through end of tomorrow for reserve / charge-target walks.

    Optimized steps stay unchanged. Only hours after the series up to tomorrow
    23:00 are appended (full tomorrow when the plan still ends today; otherwise
    the missing tomorrow tail after a rolling 24h window).
    """
    forecast_data = forecast or {
        "today": {"pv": [], "load": []},
        "tomorrow": {"pv": [], "load": []},
    }
    if not _should_extend_forecast_lookahead(
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
    ):
        return pv_series, load_series
    start_h = tomorrow_lookahead_start_hour(
        end_dt=end_dt,
        today_date=today_date,
        series_len=len(pv_series),
        global_step_offset=global_step_offset,
        step_scale=step_scale,
    )
    if start_h is None:
        return pv_series, load_series
    rep = slots_per_hour_from_scale(step_scale)
    pv_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["pv"] or [])][:HOURS_PER_DAY]
    load_tomorrow = [float(v) for v in (forecast_data["tomorrow"]["load"] or [])][:HOURS_PER_DAY]
    pv_ext = list(pv_series)
    load_ext = list(load_series)
    for h in range(start_h, HOURS_PER_DAY):
        pv_h = pv_tomorrow[h] if h < len(pv_tomorrow) else 0.0
        load_h = load_tomorrow[h] if h < len(load_tomorrow) else 0.0
        pv_ext.extend([pv_h * step_scale] * rep)
        load_ext.extend([load_h * step_scale] * rep)
    return pv_ext, load_ext


def build_extended_buy_for_reserve(
    buy_series: list[float],
    *,
    step_scale: float,
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any] | None,
    cfg: dict,
    global_step_offset: int = 0,
) -> list[float]:
    """Extend buy prices through end of tomorrow in lockstep with PV/load lookahead."""
    forecast_data = forecast or {
        "today": {"pv": [], "load": []},
        "tomorrow": {"pv": [], "load": []},
    }
    buy_for_reserve = list(buy_series)
    if not _should_extend_forecast_lookahead(
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
    ):
        return buy_for_reserve
    start_h = tomorrow_lookahead_start_hour(
        end_dt=end_dt,
        today_date=today_date,
        series_len=len(buy_series),
        global_step_offset=global_step_offset,
        step_scale=step_scale,
    )
    if start_h is None:
        return buy_for_reserve
    rep = slots_per_hour_from_scale(step_scale)
    tomorrow = today_date + timedelta(days=1)
    base = datetime(tomorrow.year, tomorrow.month, tomorrow.day)
    for h in range(start_h, HOURS_PER_DAY):
        price, _ = get_buy_price(base.replace(hour=h), cfg)
        buy_for_reserve.extend([float(price)] * rep)
    return buy_for_reserve


# Back-compat alias for callers/tests that still use the old name.
_should_extend_reserve_horizon = _should_extend_forecast_lookahead


def reserve_soc_per_step(
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    *,
    reserve_floor_kwh: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    step_scale: float = 1.0,
    end_dt: datetime | None = None,
    today_date=None,
    forecast: dict[str, Any] | None = None,
    global_step_offset: int = 0,
    buy_prices: list[float] | None = None,
    cfg: dict | None = None,
    offpeak_buy: float | None = None,
) -> list[float]:
    """Reserve floor (kWh) after each step — through midnight until next-day PV."""
    end = end_dt or datetime.now()
    pv_r, load_r = build_extended_pv_load_for_reserve(
        pv_series, load_series,
        step_scale=step_scale, end_dt=end, today_date=today_date, forecast=forecast,
        global_step_offset=global_step_offset,
    )
    buy_r = list(buy_prices) if buy_prices is not None else []
    if cfg is not None and buy_prices is not None:
        buy_r = build_extended_buy_for_reserve(
            buy_prices,
            step_scale=step_scale, end_dt=end, today_date=today_date,
            forecast=forecast, cfg=cfg,
            global_step_offset=global_step_offset,
        )
    off = float(offpeak_buy) if offpeak_buy is not None else (
        float(cfg["grid"]["g12"]["offpeak_price_pln_kwh"]) if cfg is not None else 0.0
    )
    eps_step = eps_step_kwh(epsilon, step_scale)
    slots_per_hour = slots_per_hour_from_scale(step_scale)
    return [
        reserve_soc_kwh_from_step(
            s, pv_r, load_r, reserve_floor_kwh, eta_out, eta_pv_load, eps_step,
            buy_series=buy_r if buy_r else None,
            offpeak_buy=off,
            slots_per_hour=slots_per_hour,
            global_step_offset=global_step_offset,
        )
        for s in range(steps)
    ]

