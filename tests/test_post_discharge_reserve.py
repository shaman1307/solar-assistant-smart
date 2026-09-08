"""Post-discharge survive reserve: load after last Dis until next-day PV cover + min."""

import pytest

from src.plan_optimizer import (
    HourControl,
    apply_post_discharge_reserve_floor,
    post_discharge_reserve_soc_kwh,
    pv_cover_ends_overnight_need,
)
from src.timer_plan import (
    ACTION_DISCHARGE_GRID,
    blocks_q15_to_slots,
    discharge_cap_pct_from_row,
    merge_blocks_q15,
)

OFF = 0.62
PEAK = 1.24


def _q15_buys(hour_prices: list[float]) -> list[float]:
    out: list[float] = []
    for p in hour_prices:
        out.extend([p] * 4)
    return out


def _weekday_day_buys() -> list[float]:
    return [
        OFF if not (6 <= h < 13 or 15 <= h < 22) else PEAK
        for h in range(24)
    ]


def test_evening_walk_requires_next_day_pv_cover():
    assert pv_cover_ends_overnight_need(
        local_hour=18,
        start_local_hour=18,
        crossed_midnight=False,
        seen_insufficient=True,
        cover_bound=13,
    ) is False
    assert pv_cover_ends_overnight_need(
        local_hour=7,
        start_local_hour=18,
        crossed_midnight=True,
        seen_insufficient=True,
        cover_bound=13,
    ) is True


def test_post_discharge_reserve_from_hour_after_last_dis():
    """After Dis hour 21: count 22–23 + tomorrow until PV cover + floor."""
    # Hours 21..23 today + 00..02 tomorrow in series; cover at tomorrow hour 01.
    pv = (
        [0.0] * 4  # 21
        + [0.0] * 8  # 22–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers
    )
    load = [0.2] * len(pv)
    day0 = _weekday_day_buys()
    day1 = _weekday_day_buys()
    buy = _q15_buys(day0[21:] + day1[:2])
    floor = 1.5
    # last Dis hour 21, offset at hour 21 start
    reserve = post_discharge_reserve_soc_kwh(
        21, pv, load, floor, 1.0, 1.0, 0.01,
        buy_series=buy, offpeak_buy=OFF,
        slots_per_hour=4, global_step_offset=21 * 4,
    )
    # From hour 22: 22,23,00 = 12 slots × 0.2, stop at tomorrow 01 cover
    assert reserve == pytest.approx(floor + 12 * 0.2)


def test_discharge_cap_ceils_reserve_soc_pct():
    """Fractional survive floor rounds up so SA never stops below the model."""
    assert discharge_cap_pct_from_row({"reserve_soc_pct": 34.2, "soc": 40.0}, 16) == 35
    assert discharge_cap_pct_from_row({"reserve_soc_pct": 31.01, "soc": 40.0}, 16) == 32
    assert discharge_cap_pct_from_row({"reserve_soc_pct": 34.0, "soc": 40.0}, 16) == 34


def test_apply_floor_uses_per_hour_post_dis_in_multi_hour_run():
    """H20 steps keep post_dis(20); H21 steps keep the thinner post_dis(21)."""
    pv = (
        [0.0] * 8  # 20–21
        + [0.0] * 8  # 22–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers
    )
    load = [0.25] * len(pv)
    day0 = _weekday_day_buys()
    day1 = _weekday_day_buys()
    buy = _q15_buys(day0[20:] + day1[:2])
    reserves = [1.5] * len(pv)
    offset = 20 * 4
    controls = [HourControl(0.0, 1.0)] * 8 + [HourControl(0.0, 0.0)] * (len(pv) - 8)
    out, end_floor = apply_post_discharge_reserve_floor(
        reserves,
        controls,
        pv_series=pv,
        load_series=load,
        reserve_floor_kwh=1.5,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        rce_step_offset=offset,
        slots_per_hour=4,
        buy_series=buy,
        offpeak_buy=OFF,
    )
    floor_after_20 = post_discharge_reserve_soc_kwh(
        20, pv, load, 1.5, 1.0, 1.0, 0.01,
        buy_series=buy, offpeak_buy=OFF,
        slots_per_hour=4, global_step_offset=offset,
    )
    floor_after_21 = post_discharge_reserve_soc_kwh(
        21, pv, load, 1.5, 1.0, 1.0, 0.01,
        buy_series=buy, offpeak_buy=OFF,
        slots_per_hour=4, global_step_offset=offset,
    )
    assert floor_after_20 > floor_after_21 + 0.2
    assert out[0] == pytest.approx(floor_after_20)  # H20 q0
    assert out[4] == pytest.approx(floor_after_21)  # H21 q0
    assert end_floor == pytest.approx(floor_after_21)


def test_apply_floor_includes_load_after_mid_hour_dis_end():
    """Dis 21:00-21:45 must budget the 21:45-22:00 house load vs full-hour Dis."""
    # Hours 21..23 today + 00..01 tomorrow; cover at tomorrow hour 01.
    pv = (
        [0.0] * 4  # 21
        + [0.0] * 8  # 22–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers
    )
    load = [0.25] * len(pv)
    day0 = _weekday_day_buys()
    day1 = _weekday_day_buys()
    buy = _q15_buys(day0[21:] + day1[:2])
    reserves = [1.5] * len(pv)
    offset = 21 * 4
    common = dict(
        pv_series=pv,
        load_series=load,
        reserve_floor_kwh=1.5,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        rce_step_offset=offset,
        slots_per_hour=4,
        buy_series=buy,
        offpeak_buy=OFF,
    )
    full_hour = [HourControl(0.0, 1.0)] * 4 + [HourControl(0.0, 0.0)] * (len(pv) - 4)
    mid_hour = (
        [HourControl(0.0, 1.0)] * 3
        + [HourControl(0.0, 0.0)]
        + [HourControl(0.0, 0.0)] * (len(pv) - 4)
    )
    _, floor_full = apply_post_discharge_reserve_floor(reserves, full_hour, **common)
    out_mid, floor_mid = apply_post_discharge_reserve_floor(reserves, mid_hour, **common)
    assert floor_full is not None and floor_mid is not None
    # Single-hour run: first-hour floor equals post_dis(21); mid-hour also adds q3
    # via the last-export-step tail, so mid >= full.
    assert floor_mid >= floor_full - 1e-9
    assert out_mid[0] == pytest.approx(floor_mid)
    assert out_mid[2] == pytest.approx(floor_mid)


def test_dis_slots_keep_reserve_capacity_pct():
    rows = [
        {
            "start": "25-07-2026 21:00",
            "hour": 21,
            "action": ACTION_DISCHARGE_GRID,
            "soc": 40.0,
            "grid_export": 1.5,
            "reserve_soc_pct": 34.0,
        },
        {
            "start": "25-07-2026 21:15",
            "hour": 21,
            "action": ACTION_DISCHARGE_GRID,
            "soc": 36.0,
            "grid_export": 1.5,
            "reserve_soc_pct": 34.0,
        },
        {
            "start": "25-07-2026 21:30",
            "hour": 21,
            "action": ACTION_DISCHARGE_GRID,
            "soc": 34.0,
            "grid_export": 1.5,
            "reserve_soc_pct": 34.0,
        },
    ]
    blocks = merge_blocks_q15(rows, ACTION_DISCHARGE_GRID)
    assert len(blocks) == 1
    assert blocks[0]["capacity_pct"] == 34
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
    }
    slots = blocks_q15_to_slots(blocks, "discharge", [], cfg)
    assert slots[0]["capacity_pct"] == 34
    assert slots[0]["capacity_pct"] != 16
