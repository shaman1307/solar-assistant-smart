"""Completed q0 must include energy from the :10 10-min bucket."""

from __future__ import annotations

from src.plan_hourly_actuals import (
    TEN_MIN_KWH_PER_KW,
    _actual_q15_battery_grid,
    actual_q15_slice_kwh,
)


def test_q0_includes_charge_that_starts_at_minute_10():
    """Charge spike at :10 must land in q0, not vanish until q1."""
    hour = 3
    series: list[float | None] = [None] * 144
    base = hour * 6
    series[base + 0] = 0.0  # 03:00
    series[base + 1] = 5.0  # 03:10 — charge
    series[base + 2] = 5.0  # 03:20
    grid_buy: list[float | None] = [None] * 144
    grid_buy[base + 0] = 0.0
    grid_buy[base + 1] = -5.0
    grid_buy[base + 2] = -5.0
    s10 = {
        "bat_charge": list(series),
        "bat_discharge": [0.0 if v is not None else None for v in series],
        "grid_buy": grid_buy,
        "grid_sell": [0.0] * 144,
        "pv": [0.0] * 144,
        "load": [0.5] * 144,
    }
    # q0 = slot0 + 0.5·slot1 = 0 + 0.5·5·(10/60) = 5/12 kWh
    expect = 5.0 * TEN_MIN_KWH_PER_KW * 0.5
    q0_bat = actual_q15_slice_kwh(s10["bat_charge"], hour, 0)
    assert q0_bat == expect
    bat, gi, _ = _actual_q15_battery_grid(s10, hour, 0)
    assert bat == round(expect, 4)
    assert gi == round(expect, 4)
