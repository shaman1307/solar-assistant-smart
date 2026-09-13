"""Reserve SOC looks through midnight to morning PV coverage."""

import pytest

from src.plan_optimizer import (
    grid_charge_ac_kw,
    grid_charge_target_soc_kwh_from_step,
    reserve_soc_kwh_from_step,
    morning_cover_bound_from_hour_buys,
)

OFF = 0.62
PEAK = 1.24


def _q15_buys(hour_prices: list[float]) -> list[float]:
    out: list[float] = []
    for p in hour_prices:
        out.extend([p] * 4)
    return out


def _weekday_day_buys() -> list[float]:
    """G12 weekday: peak 6–13 and 15–22."""
    return [
        OFF if not (6 <= h < 13 or 15 <= h < 22) else PEAK
        for h in range(24)
    ]


def _weekend_day_buys() -> list[float]:
    return [OFF] * 24


def test_morning_cover_bound_from_weekday_prices():
    assert morning_cover_bound_from_hour_buys(
        _weekday_day_buys(), offpeak_buy=OFF, epsilon=0.01,
    ) == 13


def test_morning_cover_bound_weekend_has_no_peak():
    assert morning_cover_bound_from_hour_buys(
        _weekend_day_buys(), offpeak_buy=OFF, epsilon=0.01,
    ) is None


def test_reserve_not_cut_by_single_sunny_q15():
    """One bright 15-min slot must not end the night reserve early."""
    pv = (
        [0.0] * 4  # hour 20 dark
        + [0.5, 0.0, 0.0, 0.0]  # hour 21 — one sunny q15, hour still dark overall
        + [0.0] * 8  # hours 22–23
        + [0.0] * 8  # tomorrow 00–01 still dark
        + [0.5, 0.5, 0.5, 0.5]  # tomorrow hour 02 covers → stop
    )
    load = [0.12] * len(pv)
    # Today evening peak + tomorrow early offpeak then… keep weekday shape for day0/day1.
    day0 = _weekday_day_buys()
    day1 = _weekday_day_buys()
    buy = _q15_buys(day0[20:] + day1[:3])
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=1.5,
        eta_out=0.9,
        eta_pv_load=0.95,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=20 * 4,
    )
    assert reserve > 2.0


def test_evening_sun_does_not_stop_before_midnight():
    """Same-day evening PV ≥ load must not end reserve; cross midnight to morning."""
    pv = (
        [0.4] * 4  # hour 18 sun
        + [0.0] * 20  # hours 19–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers → stop
    )
    load = [0.2] * len(pv)
    day0 = _weekday_day_buys()
    day1 = _weekday_day_buys()
    buy = _q15_buys(day0[18:] + day1[:2])
    floor = 1.5
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=18 * 4,
    )
    assert reserve == pytest.approx(floor + 24 * 0.2)


def test_afternoon_gap_does_not_stop_before_tonight():
    """Midday cloud then afternoon sun must not end reserve; wait for next morning."""
    pv = (
        [0.0] * 4  # hour 12 cloudy
        + [0.5] * 4  # hour 13 sun returns (afternoon — ignore)
        + [0.0] * 40  # hours 14–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers → stop
    )
    load = [0.2] * len(pv)
    day0 = _weekday_day_buys()
    day1 = _weekday_day_buys()
    buy = _q15_buys(day0[12:] + day1[:2])
    floor = 1.0
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=12 * 4,
    )
    assert reserve == pytest.approx(floor + 8.8)


def test_post_midnight_stops_on_same_morning_not_next_day():
    """From 00:00, reserve is only until today's morning PV — not tomorrow."""
    pv = (
        [0.0] * 24  # hours 0–5 dark
        + [0.5] * 4  # hour 6 covers → stop
        + [0.0] * 68  # rest of day + next night (must be ignored)
        + [0.5] * 4  # next-day morning (must be ignored)
    )
    load = [0.2] * len(pv)
    buy = _q15_buys(_weekday_day_buys() + _weekday_day_buys())
    floor = 1.5
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=0,
    )
    assert reserve == pytest.approx(floor + 5 * 0.8)
    assert reserve < floor + 8.0


def test_morning_hour_does_not_reserve_through_next_night():
    """From hour 06 with PV covering, reserve stays near floor (not Monday night)."""
    pv = [0.5] * 4  # hour 6 covers
    pv += [0.0] * 68  # rest of day + next night
    pv += [0.5] * 4  # next morning
    load = [0.2] * len(pv)
    buy = _q15_buys(_weekday_day_buys()[6:] + _weekday_day_buys())
    floor = 1.5
    reserve = reserve_soc_kwh_from_step(
        0, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=6 * 4,
    )
    assert reserve == pytest.approx(floor)


def test_g12_morning_peak_hour_12_pv_cover_stops_walk():
    """Hour 12 is still weekday morning peak; PV cover there ends overnight need."""
    pv = [0.0] * 48 + [0.5] * 4  # 0–11 dark, 12 covers
    pv += [0.0] * 48  # afternoon/night — must be ignored
    load = [0.2] * len(pv)
    buy = _q15_buys(_weekday_day_buys())
    floor = 1.5
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=0,
    )
    assert reserve == pytest.approx(floor + 11 * 0.8)
    assert reserve < floor + 15.0


def test_weekend_reserve_stops_on_morning_pv_not_fake_peak_window():
    """All-offpeak weekend: no 6–13 peak; still stop when morning PV covers."""
    pv = (
        [0.0] * 24  # hours 0–5 dark
        + [0.5] * 4  # hour 6 covers → stop
        + [0.0] * 68
    )
    load = [0.2] * len(pv)
    buy = _q15_buys(_weekend_day_buys() + _weekend_day_buys())
    floor = 1.5
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=0,
    )
    assert reserve == pytest.approx(floor + 5 * 0.8)


def test_weekend_afternoon_gap_does_not_use_weekday_peak_end():
    """Weekend midday cloud + afternoon sun must not end reserve before tonight."""
    pv = (
        [0.0] * 4  # hour 12 cloudy
        + [0.5] * 4  # hour 13 sun
        + [0.0] * 40  # 14–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers
    )
    load = [0.2] * len(pv)
    buy = _q15_buys(_weekend_day_buys()[12:] + _weekend_day_buys()[:2])
    floor = 1.0
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=12 * 4,
    )
    assert reserve == pytest.approx(floor + 8.8)


def test_weekend_grid_charge_target_ignores_overnight_offpeak_load():
    """All-offpeak buy prices → charge target is only the min floor."""
    pv = [0.0] * 24 + [0.5] * 4
    load = [0.2] * len(pv)
    buy = [OFF] * len(pv)
    floor = 1.5
    target = grid_charge_target_soc_kwh_from_step(
        3, pv, load, buy, floor, 1.0, 1.0, 0.01, offpeak_buy=OFF,
        global_step_offset=0,
    )
    assert target == pytest.approx(floor)
    assert grid_charge_ac_kw(
        10.0, buy_p=OFF, offpeak_buy=OFF, charge_target_soc_kwh=target,
        head_room_kwh=30.0, charge_ac_cap_kw=1.5, eta_grid=0.925, epsilon=0.01,
    ) == 0.0


def test_weekday_grid_charge_target_is_peak_deficits_only():
    """Peak-day charge target is floor + morning peak load, not overnight offpeak."""
    pv = [0.0] * 32 + [0.5] * 4
    load = [0.2] * len(pv)
    buy = [OFF] * 24 + [PEAK] * 8 + [PEAK] * 4
    buy = buy[: len(pv)]
    floor = 1.5
    target = grid_charge_target_soc_kwh_from_step(
        3, pv, load, buy, floor, 1.0, 1.0, 0.01, offpeak_buy=OFF,
        global_step_offset=0,
    )
    # 8 peak q15 × 0.2 kWh, overnight offpeak excluded.
    assert target == pytest.approx(floor + 1.6)
    assert grid_charge_ac_kw(
        floor + 0.5, buy_p=OFF, offpeak_buy=OFF, charge_target_soc_kwh=target,
        head_room_kwh=30.0, charge_ac_cap_kw=1.5, eta_grid=0.925, epsilon=0.01,
    ) > 0.0
    assert grid_charge_ac_kw(
        target + 0.1, buy_p=OFF, offpeak_buy=OFF, charge_target_soc_kwh=target,
        head_room_kwh=30.0, charge_ac_cap_kw=1.5, eta_grid=0.925, epsilon=0.01,
    ) == 0.0


def test_dark_weekend_day_never_covers_walk_sums_horizon():
    """If no hour has PV ≥ load, walk does not break early; charge target stays floor."""
    # 12 dark hours, tiny PV never covers 0.5 load/hour.
    hours = 12
    pv = [0.05] * (hours * 4)
    load = [0.5] * (hours * 4)
    buy = [OFF] * len(pv)
    floor = 1.5
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=0,
    )
    # From after hour 0: hours 1..11 always deficit → 11 × 0.5 (q15 sum per hour = 2.0)
    # each hour load 2.0, pv 0.2 → deficit 1.8 per hour × 11
    assert reserve == pytest.approx(floor + 11 * 1.8)
    target = grid_charge_target_soc_kwh_from_step(
        3, pv, load, buy, floor, 1.0, 1.0, 0.01, offpeak_buy=OFF,
        global_step_offset=0,
    )
    assert target == pytest.approx(floor)


def test_morning_cover_bound_evening_only_block_uses_start():
    """Truncated series showing only evening peak → bound is block start, not end."""
    # Hours 0–14 missing/offpeak, 15–21 peak (as if series started in evening).
    hour_buys = [OFF] * 15 + [PEAK] * 7 + [OFF] * 2
    assert morning_cover_bound_from_hour_buys(
        hour_buys, offpeak_buy=OFF, epsilon=0.01,
    ) == 15


def test_evening_only_bound_does_not_stop_on_evening_pv_cover():
    """With evening-only peak visible, same-day evening PV cover must not end walk."""
    # Start hour 18: sun covers, then night, tomorrow morning covers.
    pv = (
        [0.5] * 4  # hour 18 covers
        + [0.0] * 20  # 19–23
        + [0.0] * 4  # tomorrow 00
        + [0.5] * 4  # tomorrow 01 covers
    )
    load = [0.2] * len(pv)
    day0 = [OFF] * 15 + [PEAK] * 7 + [OFF] * 2
    day1 = _weekend_day_buys()
    buy = _q15_buys(day0[18:] + day1[:2])
    floor = 1.5
    reserve = reserve_soc_kwh_from_step(
        3, pv, load,
        reserve_floor_kwh=floor,
        eta_out=1.0,
        eta_pv_load=1.0,
        epsilon=0.01,
        buy_series=buy,
        offpeak_buy=OFF,
        global_step_offset=18 * 4,
    )
    # Must walk through night: 19–23 + tomorrow 00 = 24 slots × 0.2
    assert reserve == pytest.approx(floor + 24 * 0.2)
