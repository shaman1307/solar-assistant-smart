"""Offpeak grid→battery fill after DP (last hour before peak, min hourly block)."""

from __future__ import annotations

from typing import Any, Callable

from .plan_physics import HourControl, simulate_hour, slots_per_hour_from_scale

# Smart+ leaves the morning charge unchanged when SOC at the first counted
# super hour is already above this. The added DC is cut so that hour stays
# at or under the cap.
SUPERCHARGE_SKIP_ABOVE_SOC_PCT = 90.0
SUPERCHARGE_CAP_SOC_PCT = 95.0

def grid_charge_ac_kw(
    soc_kwh: float,
    *,
    buy_p: float,
    offpeak_buy: float,
    charge_target_soc_kwh: float,
    head_room_kwh: float,
    charge_ac_cap_kw: float,
    eta_grid: float,
    epsilon: float,
) -> float:
    """Single grid→battery decision: offpeak + below peak-cover target + headroom.

    Returns AC kWh to charge this step (0 if not allowed). Uses only the AC
    still needed to reach *charge_target_soc_kwh* (not always the hardware cap).
    """
    if buy_p > offpeak_buy + epsilon:
        return 0.0
    if charge_target_soc_kwh <= soc_kwh + epsilon:
        return 0.0
    if head_room_kwh <= epsilon:
        return 0.0
    need_dc = charge_target_soc_kwh - soc_kwh
    need_ac = need_dc / eta_grid if eta_grid > 0 else need_dc
    max_ac = head_room_kwh / eta_grid if eta_grid > 0 else head_room_kwh
    return min(charge_ac_cap_kw, max_ac, max(0.0, need_ac))

def battery_grid_charge_step_ac(
    budget_ac: float,
    *,
    charge_ac_step: float,
    step_scale: float,
    min_block_minutes: int,
    eps_step: float,
) -> float:
    """AC kWh per fill step: pack at hardware max (dense, one clock hour).

    Do not dilute power across extra quarters just to hit ``min_block`` — that
    splits one budget into multiple half-hour Chg rows (e.g. 01:00-01:30 then
    02:00-02:30). Fill consecutive steps at max until the budget is spent so
    ~4 kWh fits in one clock hour when inverter/battery allow it. Timer
    ``min_block`` / ``min_hourly_transfer_kwh`` stay enforced in timer_plan /
    ``enforce_min_hourly_battery_grid_limits``.
    """
    del step_scale, min_block_minutes  # unused here; callers may still pass them
    if budget_ac <= eps_step or charge_ac_step <= eps_step:
        return 0.0
    return float(charge_ac_step)


def offpeak_windows_before_peak(
    n_steps: int,
    is_peak: Callable[[int], bool],
    scan_from: int,
) -> list[tuple[int, int]]:
    """Offpeak runs that end at a peak step. A run with no following peak is omitted."""
    windows: list[tuple[int, int]] = []
    i = max(0, int(scan_from))
    n = int(n_steps)
    while i < n:
        if is_peak(i):
            i += 1
            continue
        start = i
        while i < n and not is_peak(i):
            i += 1
        if i >= n:
            break
        windows.append((start, i))
        while i < n and is_peak(i):
            i += 1
    return windows


def plan_battery_grid_charge(
    controls: list[HourControl],
    *,
    pv_series: list[float],
    load_series: list[float],
    buy_prices: list[float],
    offpeak_buy: float,
    charge_targets: list[float],
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    charge_ac_step: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
    step_scale: float = 1.0,
    skip_leading_slots: int | None = None,
    min_block_minutes: int | None = None,
    min_hourly_kwh: float = 0.0,
    preserve_export: bool = False,
) -> list[HourControl]:
    """Plan battery grid-charge slots: fill the last offpeak hour(s) before each peak.

    Default: skip the first clock hour of the horizon (current hour) so Chg is
    not placed in the in-progress hour. Pass ``skip_leading_slots=0`` when the
    horizon already starts after a committed current hour (future-only replan).

    Each offpeak run that ends at a peak is packed from the end of that run.
    The cheap window is the buy-price offpeak before the peak, not a fixed clock hour.
    Size the AC budget so idle drain until that peak still leaves
    ``charge_targets`` at peak start. House load on every filled step is on
    the battery during the fill, so it is bought with the SOC gap.
    When targets are at the floor (or unused), relocate the DP AC volume
    of that same offpeak instead.

    Pack at hardware max in the last offpeak clock hour before the peak, spilling
    into earlier offpeak hours only if the last hour cannot hold the budget.
    A partial earlier hour is packed at the end of that hour so the charge
    block stays contiguous up to the peak.

    Drop the budget when it is below ``min_hourly_kwh`` and forcing a min block
    would cost more than buying the same house energy at peak.

    House load stays on the battery during the fill.
    """
    if not controls:
        return controls

    slots_per_hour = slots_per_hour_from_scale(step_scale)
    if skip_leading_slots is None:
        skip_leading_slots = slots_per_hour
    fill_from_step = min(len(controls), max(0, int(skip_leading_slots)))
    if min_block_minutes is None:
        min_block_minutes = 30

    def _price(step: int) -> float:
        return float(buy_prices[step] if step < len(buy_prices) else offpeak_buy)

    def _is_peak(step: int) -> bool:
        return _price(step) > offpeak_buy + eps_step

    def _play(soc_kwh: float, step: int, ctrl: HourControl):
        pv = float(pv_series[step]) if step < len(pv_series) else 0.0
        load = float(load_series[step]) if step < len(load_series) else 0.0
        reserve = float(reserves[step]) if step < len(reserves) else min_kwh
        phys = simulate_hour(
            soc_kwh, pv, load, ctrl,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kw=inverter_ac_step,
            discharge_dc_cap_kwh=discharge_dc_step,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            epsilon=eps_step,
            reserve_soc_kwh=reserve,
        )
        return phys.soc_end

    def _kept_export(prev: HourControl) -> float:
        return float(prev.battery_export_kwh) if preserve_export else 0.0

    # Skip a peak the horizon already sits in, then each offpeak that ends at a peak.
    scan_from = fill_from_step
    while scan_from < len(controls) and _is_peak(scan_from):
        scan_from += 1
    windows = offpeak_windows_before_peak(len(controls), _is_peak, scan_from)

    step_ac = battery_grid_charge_step_ac(
        charge_ac_step,
        charge_ac_step=charge_ac_step,
        step_scale=step_scale,
        min_block_minutes=int(min_block_minutes),
        eps_step=eps_step,
    )
    fill_kw = [0.0] * len(controls)
    managed = [False] * len(controls)
    soc = float(initial_soc_kwh)
    cursor = 0
    for start, peak in windows:
        for step in range(cursor, start):
            prev = controls[step]
            charge_kw = 0.0 if step < fill_from_step else float(prev.grid_charge_kw)
            soc = _play(
                soc, step, HourControl(charge_kw, _kept_export(prev), prev.load_from_grid),
            )
        idle_soc = soc
        idle_grid: dict[int, float] = {}
        for step in range(start, peak):
            pv = float(pv_series[step]) if step < len(pv_series) else 0.0
            load = float(load_series[step]) if step < len(load_series) else 0.0
            reserve = float(reserves[step]) if step < len(reserves) else min_kwh
            idle_phys = simulate_hour(
                idle_soc, pv, load, HourControl(0.0, 0.0, False),
                battery_cap=battery_cap,
                min_kwh=min_kwh,
                ac_cap_kw=inverter_ac_step,
                discharge_dc_cap_kwh=discharge_dc_step,
                eta_grid=eta_grid,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                eta_pv_grid=eta_pv_grid,
                eta_pv_battery=eta_pv_battery,
                epsilon=eps_step,
                reserve_soc_kwh=reserve,
            )
            idle_grid[step] = float(idle_phys.grid_import)
            idle_soc = idle_phys.soc_end
        prepeak_target = 0.0
        if charge_targets and peak > 0:
            idx = min(peak - 1, len(charge_targets) - 1)
            if idx >= 0:
                prepeak_target = float(charge_targets[idx])
        dp_budget_ac = sum(
            float(controls[step].grid_charge_kw)
            for step in range(max(start, fill_from_step), peak)
        )
        refine_house = prepeak_target > min_kwh + eps_step
        if refine_house:
            need_battery = max(0.0, prepeak_target - idle_soc)
            house_dc = 0.0
            budget_ac = 0.0
        else:
            need_battery = 0.0
            house_dc = 0.0
            budget_ac = dp_budget_ac
        passes = (peak - start + 1) if refine_house else 1
        for _pass in range(passes):
            if refine_house:
                budget_ac = (
                    (need_battery + house_dc) / eta_grid
                    if eta_grid > 0
                    else need_battery + house_dc
                )
            for step in range(start, peak):
                fill_kw[step] = 0.0
            if budget_ac <= eps_step:
                break
            budget_left = float(budget_ac)
            hour_steps: dict[int, list[int]] = {}
            for step in range(max(start, fill_from_step), peak):
                hour_steps.setdefault(step // slots_per_hour, []).append(step)
            hours_desc = sorted(hour_steps.keys(), reverse=True)
            for hour_i, hour in enumerate(hours_desc):
                if budget_left <= eps_step:
                    break
                steps = hour_steps[hour]
                # The hour nearest the peak keeps a short block at :00. Earlier hours
                # fill from :45 backward so the leftover meets the next charge hour.
                if hour_i > 0:
                    steps = list(reversed(steps))
                for step in steps:
                    if budget_left <= eps_step:
                        break
                    chunk = min(step_ac, charge_ac_step, budget_left)
                    if chunk <= eps_step:
                        continue
                    fill_kw[step] = chunk
                    budget_left = max(0.0, budget_left - chunk)
            if not refine_house:
                break
            new_house = 0.0
            for step in range(max(start, fill_from_step), peak):
                if fill_kw[step] <= eps_step:
                    continue
                new_house += float(idle_grid.get(step, 0.0))
            new_house = new_house / eta_out if eta_out > 0 else new_house
            if abs(new_house - house_dc) <= eps_step:
                break
            house_dc = new_house
        peak_buy = _price(peak)
        if budget_ac > eps_step and not offpeak_min_block_charge_is_worth(
            need_ac_kwh=budget_ac,
            min_hourly_kwh=float(min_hourly_kwh),
            offpeak_buy=float(offpeak_buy),
            peak_buy=float(peak_buy),
            eta_grid=float(eta_grid),
            eta_out=float(eta_out),
            epsilon=float(eps_step),
        ):
            budget_ac = 0.0
            for step in range(start, peak):
                fill_kw[step] = 0.0
        for step in range(start, peak):
            managed[step] = True
            if budget_ac <= eps_step:
                fill_kw[step] = 0.0
        for step in range(start, peak):
            charge_kw = 0.0 if step < fill_from_step else float(fill_kw[step])
            if charge_kw > eps_step:
                head_room = max(0.0, battery_cap - soc)
                max_ac = head_room / eta_grid if eta_grid > 0 else head_room
                charge_kw = min(charge_kw, max_ac)
                if charge_kw <= eps_step:
                    charge_kw = 0.0
                fill_kw[step] = charge_kw
            soc = _play(soc, step, HourControl(charge_kw, 0.0, False))
        cursor = peak

    out: list[HourControl] = []
    soc = float(initial_soc_kwh)
    for step, prev in enumerate(controls):
        export_kwh = _kept_export(prev)
        if step < fill_from_step:
            ctrl = HourControl(0.0, export_kwh, False)
        elif managed[step]:
            ctrl = HourControl(float(fill_kw[step]), export_kwh, False)
        else:
            ctrl = HourControl(prev.grid_charge_kw, export_kwh, prev.load_from_grid)
        soc = _play(soc, step, ctrl)
        out.append(ctrl)
    return out



def enforce_min_hourly_battery_grid_limits(
    controls: list[HourControl],
    *,
    rce_step_offset: int,
    step_scale: float,
    min_hourly_kwh: float,
    epsilon: float,
) -> list[HourControl]:
    """Enforce min_hourly_transfer_kwh on battery↔grid flows per clock hour.

    Export or charge below the floor is cleared. Do not scale a thin overnight
    top-up up to the floor — that forces an uneconomic min block (e.g. 2 kWh
    Chg to cover a 0.3 kWh morning gap).
    """
    if min_hourly_kwh <= epsilon or not controls:
        return controls
    slots_per_hour = slots_per_hour_from_scale(step_scale)
    out = list(controls)
    by_hour: dict[int, list[int]] = {}
    for i in range(len(out)):
        hour = (rce_step_offset + i) // slots_per_hour
        by_hour.setdefault(hour, []).append(i)

    for idxs in by_hour.values():
        export_h = sum(out[i].battery_export_kwh for i in idxs)
        charge_h = sum(out[i].grid_charge_kw for i in idxs)
        if epsilon < export_h < min_hourly_kwh:
            for i in idxs:
                c = out[i]
                out[i] = HourControl(c.grid_charge_kw, 0.0, c.load_from_grid)
        if epsilon < charge_h < min_hourly_kwh:
            for i in idxs:
                c = out[i]
                out[i] = HourControl(0.0, c.battery_export_kwh, c.load_from_grid)
    return out




def offpeak_min_block_charge_is_worth(
    *,
    need_ac_kwh: float,
    min_hourly_kwh: float,
    offpeak_buy: float,
    peak_buy: float,
    eta_grid: float,
    eta_out: float,
    epsilon: float,
) -> bool:
    """Whether an offpeak grid→battery block pays for itself vs peak house buy.

    When *need_ac_kwh* is below *min_hourly_kwh*, the timer must take the full
    min block (or nothing). Skip the block when its offpeak cost exceeds the
    peak-tariff cost of buying only the needed house energy.
    """
    need = max(0.0, float(need_ac_kwh))
    if need <= epsilon:
        return False
    floor = max(0.0, float(min_hourly_kwh))
    off = max(0.0, float(offpeak_buy))
    peak = max(off, float(peak_buy))
    eta_g = max(1e-9, float(eta_grid))
    eta_o = max(1e-9, float(eta_out))
    # Battery DC from AC charge ≈ need*eta_grid; that DC serves peak AC load *eta_out.
    avoided_peak_ac = need * eta_g * eta_o
    cost_avoided = avoided_peak_ac * peak
    charge_ac = need if need + epsilon >= floor or floor <= epsilon else floor
    cost_charge = charge_ac * off
    return cost_charge <= cost_avoided + epsilon


def supercharge_counted_hours(
    *,
    end_hour: int,
    rce_series: list[float | None],
    slots_per_hour: int,
    horizon_hours: list[int],
    min_price: float,
    export_window_start_hour: int,
    cfg: dict[str, Any] | None,
    today_date,
) -> list[int]:
    """Hours in the 24h after *end_hour* whose mean RCE is at least *min_price*.

    Morning-peak hours that a later evening hour outranks are omitted. The
    drop uses the same ratings as battery export, including published RCE
    past the horizon.
    """
    from .plan_export import (
        hour_rce_rating,
        hourly_avg_rce,
        morning_peak_hours_below_later_evening,
    )

    slots = max(1, int(slots_per_hour))
    start = int(end_hour)
    stop = start + 24
    ratings: dict[int, float] = {}
    priced: list[int] = []
    for hour in range(start, stop):
        avg = hourly_avg_rce(rce_series, hour, slots_per_hour=slots)
        rating = hour_rce_rating(rce_series, hour, slots_per_hour=slots)
        if avg is None or rating is None or float(avg) + 1e-9 < float(min_price):
            continue
        ratings[int(hour)] = float(rating)
        priced.append(int(hour))
    window_start = int(export_window_start_hour)
    for hour in horizon_hours:
        clock = int(hour) % 24
        if clock < window_start or int(hour) in ratings:
            continue
        rating = hour_rce_rating(rce_series, int(hour), slots_per_hour=slots)
        if rating is not None:
            ratings[int(hour)] = float(rating)
    if not ratings:
        return []
    dropped = morning_peak_hours_below_later_evening(
        ratings,
        hours=list(horizon_hours),
        rce_series=rce_series,
        slots_per_hour=slots,
        export_window_start_hour=window_start,
        cfg=cfg,
        today_date=today_date,
    )
    return [hour for hour in priced if hour not in dropped]


def apply_morning_supercharge(
    controls: list[HourControl],
    *,
    pv_series: list[float],
    load_series: list[float],
    buy_prices: list[float],
    offpeak_buy: float,
    charge_targets: list[float],
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    charge_ac_step: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
    step_scale: float,
    rce_series: list[float | None],
    rce_step_offset: int,
    export_window_start_hour: int,
    cfg: dict[str, Any],
    today_date,
    skip_leading_slots: int | None = None,
    min_block_minutes: int | None = None,
    min_hourly_kwh: float = 0.0,
) -> list[HourControl]:
    """Add Smart+ DC on a morning offpeak window that already has grid charge.

    Counted hours are the next 24h after that window's peak with mean RCE at
    or above grid.super_rce_min_pln_kwh, minus morning peaks a later evening
    hour outranks. Extra battery DC is timer discharge kW times that count,
    cut so SOC at the first counted hour stays at or under 95%. Grid AC for
    the extra DC is DC / eta_grid inside the charge pass. Skip the window
    when that SOC is already above 90%, or when the window has no charge.
    """
    from .g12_pricing import get_g12_zone
    from .grid_config import super_rce_min_pln_kwh
    from .plan_dp import dp_step_clock
    from .simulation_config import plan_timer_discharge_power_kw

    if not controls or not cfg.get("smart_plus_enabled"):
        return controls

    slots = slots_per_hour_from_scale(step_scale)
    if skip_leading_slots is None:
        fill_from = slots
    else:
        fill_from = min(len(controls), max(0, int(skip_leading_slots)))

    def _is_peak(step: int) -> bool:
        price = float(buy_prices[step] if step < len(buy_prices) else offpeak_buy)
        return price > offpeak_buy + eps_step

    def _play(soc_kwh: float, step: int, ctrl: HourControl) -> float:
        pv = float(pv_series[step]) if step < len(pv_series) else 0.0
        load = float(load_series[step]) if step < len(load_series) else 0.0
        reserve = float(reserves[step]) if step < len(reserves) else min_kwh
        phys = simulate_hour(
            soc_kwh, pv, load, ctrl,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kw=inverter_ac_step,
            discharge_dc_cap_kwh=discharge_dc_step,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            epsilon=eps_step,
            reserve_soc_kwh=reserve,
        )
        return phys.soc_end

    scan_from = fill_from
    while scan_from < len(controls) and _is_peak(scan_from):
        scan_from += 1
    windows = offpeak_windows_before_peak(len(controls), _is_peak, scan_from)
    horizon_hours = sorted({
        (int(rce_step_offset) + i) // slots for i in range(len(controls))
    })
    # Morning pre-peak target stays on this list so a later charge pass
    # (after the sale) still packs the supercharge.
    targets = charge_targets
    min_price = super_rce_min_pln_kwh(cfg)
    discharge_kw = plan_timer_discharge_power_kw(cfg)
    out = controls

    for start, peak in windows:
        clock = dp_step_clock(
            today_date, peak,
            rce_step_offset=int(rce_step_offset), slots_per_hour=slots,
        )
        if clock.hour >= 12 or get_g12_zone(clock, cfg) != "peak":
            continue
        charged = sum(
            float(out[step].grid_charge_kw)
            for step in range(max(start, fill_from), peak)
            if step < len(out)
        )
        if charged <= eps_step:
            continue
        end_hour = (int(rce_step_offset) + peak) // slots
        counted = supercharge_counted_hours(
            end_hour=end_hour,
            rce_series=rce_series,
            slots_per_hour=slots,
            horizon_hours=horizon_hours,
            min_price=min_price,
            export_window_start_hour=int(export_window_start_hour),
            cfg=cfg,
            today_date=today_date,
        )
        if not counted:
            continue
        first_step = counted[0] * slots - int(rce_step_offset)
        soc = float(initial_soc_kwh)
        for step in range(max(0, min(first_step, len(out)))):
            prev = out[step]
            soc = _play(soc, step, HourControl(
                float(prev.grid_charge_kw), 0.0, prev.load_from_grid,
            ))
        if battery_cap > 0 and (soc / battery_cap) * 100.0 > SUPERCHARGE_SKIP_ABOVE_SOC_PCT:
            continue
        headroom = max(0.0, battery_cap * SUPERCHARGE_CAP_SOC_PCT / 100.0 - soc)
        extra_dc = min(discharge_kw * len(counted), headroom)
        if extra_dc <= eps_step:
            continue
        idx = min(peak - 1, len(targets) - 1)
        if idx < 0:
            continue
        targets[idx] = min(float(battery_cap), float(targets[idx]) + extra_dc)
        out = plan_battery_grid_charge(
            out,
            pv_series=pv_series,
            load_series=load_series,
            buy_prices=buy_prices,
            offpeak_buy=offpeak_buy,
            charge_targets=targets,
            initial_soc_kwh=initial_soc_kwh,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            charge_ac_step=charge_ac_step,
            discharge_dc_step=discharge_dc_step,
            inverter_ac_step=inverter_ac_step,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            eps_step=eps_step,
            reserves=reserves,
            step_scale=step_scale,
            skip_leading_slots=skip_leading_slots,
            min_block_minutes=min_block_minutes,
            min_hourly_kwh=min_hourly_kwh,
        )
    return out

