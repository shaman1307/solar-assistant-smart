"""Offpeak grid→battery fill after DP (last hour before peak, min hourly block)."""

from __future__ import annotations

from .plan_physics import HourControl, simulate_hour, slots_per_hour_from_scale

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
) -> list[HourControl]:
    """Plan battery grid-charge slots: fill the last offpeak hour(s) before peak.

    Default: skip the first clock hour of the horizon (current hour) so Chg is
    not placed in the in-progress hour. Pass ``skip_leading_slots=0`` when the
    horizon already starts after a committed current hour (future-only replan).

    Size the AC budget so idle drain until the first peak still leaves
    ``charge_targets`` at peak start. When targets are at the floor (or unused),
    relocate the DP pre-peak AC volume instead.

    Pack at hardware max in the last offpeak clock hour before peak, spilling
    into earlier offpeak hours only if the last hour cannot hold the budget.

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

    first_peak = len(controls)
    peak_buy = float(offpeak_buy)
    for i, p in enumerate(buy_prices):
        if i >= len(controls):
            break
        price = float(p)
        if price > offpeak_buy + eps_step:
            first_peak = i
            peak_buy = price
            break

    dp_budget_ac = sum(
        float(controls[i].grid_charge_kw)
        for i in range(min(first_peak, len(controls)))
        if float(buy_prices[i] if i < len(buy_prices) else offpeak_buy)
        <= offpeak_buy + eps_step
    )

    idle_soc = float(initial_soc_kwh)
    for step in range(min(first_peak, len(controls))):
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
        idle_soc = idle_phys.soc_end

    prepeak_target = 0.0
    if charge_targets and first_peak > 0:
        idx = min(first_peak - 1, len(charge_targets) - 1)
        if idx >= 0:
            prepeak_target = float(charge_targets[idx])

    if prepeak_target > min_kwh + eps_step:
        need_dc = max(0.0, prepeak_target - idle_soc)
        budget_ac = need_dc / eta_grid if eta_grid > 0 else need_dc
    else:
        budget_ac = dp_budget_ac

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
    if budget_ac <= eps_step:
        out_clear: list[HourControl] = []
        for step, prev in enumerate(controls):
            buy_p = float(buy_prices[step]) if step < len(buy_prices) else offpeak_buy
            clear_chg = (
                step < first_peak
                and buy_p <= offpeak_buy + eps_step
                and float(prev.grid_charge_kw) > eps_step
            )
            if clear_chg:
                out_clear.append(
                    HourControl(0.0, prev.battery_export_kwh, prev.load_from_grid)
                )
            else:
                out_clear.append(prev)
        return out_clear

    step_ac = battery_grid_charge_step_ac(
        budget_ac,
        charge_ac_step=charge_ac_step,
        step_scale=step_scale,
        min_block_minutes=int(min_block_minutes),
        eps_step=eps_step,
    )

    fill_kw = [0.0] * len(controls)
    budget_left = float(budget_ac)
    hour_steps: dict[int, list[int]] = {}
    for step in range(fill_from_step, min(first_peak, len(controls))):
        buy_p = float(buy_prices[step]) if step < len(buy_prices) else offpeak_buy
        if buy_p > offpeak_buy + eps_step:
            continue
        hour_steps.setdefault(step // slots_per_hour, []).append(step)
    for hour in sorted(hour_steps.keys(), reverse=True):
        if budget_left <= eps_step:
            break
        for step in hour_steps[hour]:
            if budget_left <= eps_step:
                break
            chunk = min(step_ac, charge_ac_step, budget_left)
            if chunk <= eps_step:
                continue
            fill_kw[step] = chunk
            budget_left = max(0.0, budget_left - chunk)

    out: list[HourControl] = []
    soc = float(initial_soc_kwh)
    for step, prev in enumerate(controls):
        pv = float(pv_series[step]) if step < len(pv_series) else 0.0
        load = float(load_series[step]) if step < len(load_series) else 0.0
        buy_p = float(buy_prices[step]) if step < len(buy_prices) else offpeak_buy
        reserve = float(reserves[step]) if step < len(reserves) else min_kwh

        if step >= first_peak:
            ctrl = HourControl(prev.grid_charge_kw, 0.0, prev.load_from_grid)
        elif step < fill_from_step:
            ctrl = HourControl(0.0, 0.0, False)
        else:
            charge_kw = float(fill_kw[step])
            if charge_kw > eps_step:
                head_room = max(0.0, battery_cap - soc)
                max_ac = head_room / eta_grid if eta_grid > 0 else head_room
                charge_kw = min(charge_kw, max_ac)
                if charge_kw <= eps_step:
                    charge_kw = 0.0
            ctrl = HourControl(charge_kw, 0.0, False)

        phys = simulate_hour(
            soc, pv, load, ctrl,
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
        out.append(ctrl)
        soc = phys.soc_end
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

