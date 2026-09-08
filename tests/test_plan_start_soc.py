"""Resolve plan-start SOC from live meter (hour 0) or prior Influx hour end."""

from __future__ import annotations

import pytest

from src.simulation import resolve_plan_start_soc_kwh


def test_plan_start_hour0_uses_day_start_not_live():
    battery_cap = 48.0
    live = 0.22 * battery_cap
    day_start = 0.25 * battery_cap
    got = resolve_plan_start_soc_kwh(
        0,
        {"soc": [None] * 24},
        battery_cap,
        16.0,
        day_start,
        live,
    )
    assert got == day_start


def test_plan_start_later_hour_uses_prior_influx_end():
    battery_cap = 48.0
    live = 0.22 * battery_cap
    hourly = {"soc": [None] * 24}
    hourly["soc"][2] = 30.0  # end of hour 2
    got = resolve_plan_start_soc_kwh(
        3,
        hourly,
        battery_cap,
        16.0,
        0.5 * battery_cap,
        live,
    )
    assert got == pytest.approx(0.30 * battery_cap)
