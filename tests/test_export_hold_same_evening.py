"""Evening export must not hold SOC for a later cheaper evening."""

from __future__ import annotations

from src.plan_optimizer import (
    HourControl,
    BatteryGridExportHourClaim,
    hold_soc_for_later_battery_grid_export_claims,
    plan_battery_grid_export,
)
from tests.test_ranked_export import _cfg
from src.simulation_config import get_simulation_params


def test_hold_ignores_later_cheaper_and_next_evening():
    """Hold only richer hours in the same run, not tomorrow evening."""
    # Tonight H19 + H20, tomorrow evening H44 (= next day H20)
    claims = {
        20: BatteryGridExportHourClaim(20, (0, 4), (2.0, 2.0, 2.0, 2.0), 8.0),
        44: BatteryGridExportHourClaim(44, (0, 4), (2.0, 2.0, 2.0, 2.0), 8.0),
    }
    ratings = {19: 1.73, 20: 1.78, 21: 1.30, 44: 1.67}
    # Claiming H19: hold for richer same-run H20 only (~8 kWh AC)
    hold = hold_soc_for_later_battery_grid_export_claims(
        claims, from_hour=19, eta_out=1.0, ratings=ratings,
    )
    assert abs(hold - 8.0) < 1e-6
    # Claiming H20 (richest tonight): no hold for cheaper H44 next evening
    hold20 = hold_soc_for_later_battery_grid_export_claims(
        claims, from_hour=20, eta_out=1.0, ratings=ratings,
    )
    assert hold20 == 0.0


def test_richest_tonight_not_starved_by_next_evening_draft():
    """Pass-2 must still export peak H20 when tomorrow evening is also selected."""
    cfg = _cfg(min_hourly_transfer_kwh=2.0)
    get_simulation_params(cfg)
    slots = 4
    start_h = 19
    # H19..H23 today + H0..H23 tomorrow → absolute hours 19..42
    hours = list(range(start_h, start_h + 24))
    offset = start_h * slots
    steps = len(hours) * slots

    today_rce = {19: 1.73, 20: 1.78, 21: 1.30, 22: 1.01, 23: 0.92}
    tom_rce = {17: 0.76, 18: 1.03, 19: 1.52, 20: 1.67, 21: 1.29}
    rce: list[float | None] = [None] * (offset + steps)
    for abs_h in hours:
        clock = abs_h % 24
        is_tom = abs_h >= 24
        price = tom_rce.get(clock, 0.4) if is_tom else today_rce.get(clock, 0.4)
        for q in range(slots):
            rce[abs_h * slots + q] = price

    base = [HourControl(0.0, 0.0, False) for _ in range(steps)]
    # Limited SOC so stacking tonight+tomorrow holds would starve H20
    initial = 22.0
    min_kwh = 8.6
    reserves = [min_kwh + 2.0] * steps

    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.25] * steps,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=initial,
        battery_cap=48.0,
        min_kwh=min_kwh,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.95,
        eta_out=0.95,
        eta_pv_load=0.95,
        eta_pv_grid=0.95,
        eta_pv_battery=0.95,
        eps_step=0.01,
        reserves=reserves,
        export_floor=0.5,
        min_hourly_kwh=2.0,
    )

    def hour_export(abs_hour: int) -> float:
        return sum(
            controls[step].battery_export_kwh
            for step in range(steps)
            if (offset + step) // slots == abs_hour
        )

    exp20 = hour_export(20)
    exp21 = hour_export(21)
    assert exp20 >= 2.0, f"peak H20 must export, got {exp20}"
    # No gap: H21 without H20
    if exp21 >= 2.0:
        assert exp20 >= 2.0


def test_tonight_exports_when_next_evening_in_horizon_is_richer():
    """01.09 19:00: tomorrow H18 (1.29) enters the 24h window richer than tonight H20 (1.24).

    Tonight still sells; the later evening is a separate start-hour→morning window.
    """
    cfg = _cfg(min_hourly_transfer_kwh=2.0)
    get_simulation_params(cfg)
    slots = 4
    start_h = 19
    hours = list(range(start_h, start_h + 24))
    offset = start_h * slots
    steps = len(hours) * slots

    today_rce = {19: 1.206, 20: 1.241, 21: 1.120, 22: 1.022, 23: 0.898}
    tom_rce = {17: 0.953, 18: 1.285}
    rce: list[float | None] = [None] * (offset + steps)
    for abs_h in hours:
        clock = abs_h % 24
        is_tom = abs_h >= 24
        price = tom_rce.get(clock, 0.40) if is_tom else today_rce.get(clock, 0.40)
        for q in range(slots):
            rce[abs_h * slots + q] = price

    base = [HourControl(0.0, 0.0, False) for _ in range(steps)]
    initial = 24.5
    min_kwh = 7.7
    reserves = [min_kwh + 2.0] * steps

    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.25] * steps,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=initial,
        battery_cap=48.0,
        min_kwh=min_kwh,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.95,
        eta_out=0.95,
        eta_pv_load=0.95,
        eta_pv_grid=0.95,
        eta_pv_battery=0.95,
        eps_step=0.01,
        reserves=reserves,
        export_floor=0.62,
        min_hourly_kwh=2.0,
    )

    def hour_export(abs_hour: int) -> float:
        return sum(
            controls[step].battery_export_kwh
            for step in range(steps)
            if (offset + step) // slots == abs_hour
        )

    exp20 = hour_export(20)
    exp_tom18 = hour_export(42)
    assert exp20 >= 2.0, f"tonight H20 must export, got {exp20} (tomorrow H18={exp_tom18})"


def test_leftover_exports_overnight_peak_h06():
    """01.09 23:00: remaining peak is H06 (0.99 > H23 0.90); leftover Dis H06.

    Tomorrow evening is a later window and must not take tonight's surplus.
    H23 may also Dis when SOC remains after H06 and overnight house load.
    """
    slots = 4
    start_h = 23
    hours = list(range(start_h, start_h + 24))
    offset = start_h * slots
    steps = len(hours) * slots

    today_rce = {23: 0.898}
    tom_rce = {
        0: 0.813, 1: 0.768, 2: 0.746, 3: 0.747, 4: 0.771, 5: 0.850,
        6: 0.994, 19: 1.576, 20: 1.643,
    }
    rce: list[float | None] = [None] * (offset + steps)
    pv = [0.0] * steps
    load = [0.15] * steps
    for abs_h in hours:
        clock = abs_h % 24
        is_tom = abs_h >= 24
        price = tom_rce.get(clock, 0.40) if is_tom else today_rce.get(clock, 0.40)
        for q in range(slots):
            rce[abs_h * slots + q] = price
        if is_tom and clock == 7:
            for q in range(slots):
                step = abs_h * slots + q - offset
                if 0 <= step < steps:
                    pv[step] = 0.5

    base = [HourControl(0.0, 0.0, False) for _ in range(steps)]
    min_kwh = 8.64
    h06 = 30
    load_per_hour = 0.15 * slots
    reserves: list[float] = []
    for abs_h in hours:
        hours_until_h06 = max(0, h06 - abs_h)
        floor = min_kwh + load_per_hour * hours_until_h06
        reserves.extend([floor] * slots)
    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=pv,
        load_series=load,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=19.4,
        battery_cap=48.0,
        min_kwh=min_kwh,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.95,
        eta_out=0.95,
        eta_pv_load=0.95,
        eta_pv_grid=0.95,
        eta_pv_battery=0.95,
        eps_step=0.01,
        reserves=reserves,
        export_floor=0.62,
        min_hourly_kwh=2.0,
    )

    def hour_export(abs_hour: int) -> float:
        return sum(
            controls[step].battery_export_kwh
            for step in range(steps)
            if (offset + step) // slots == abs_hour
        )

    exp06 = hour_export(30)
    exp23 = hour_export(23)
    exp_tom20 = hour_export(44)
    assert exp06 >= 2.0, (
        f"overnight peak H06 must export leftover, got H06={exp06} "
        f"H23={exp23} tomH20={exp_tom20}"
    )
    if exp23 >= 2.0:
        assert exp06 >= 2.0


def test_sale_windows_split_on_noon_gap():
    from src.plan_optimizer import sale_windows

    assert sale_windows([19, 20, 21, 41, 42]) == [[19, 20, 21], [41, 42]]
    assert sale_windows([19, 20, 23, 24, 41]) == [[19, 20, 23, 24], [41]]


def test_trim_failed_next_evening_keeps_tonight():
    from src.plan_optimizer import trim_remaining_after_failed_export_edge

    kept = trim_remaining_after_failed_export_edge(
        [19, 20, 21, 41], selected={42}, failed_hour=41,
    )
    assert 19 in kept and 20 in kept and 21 in kept
    assert 41 not in kept
