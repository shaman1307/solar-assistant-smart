"""Evening export SOC ladder: skip cheap H21 for richer H22 until SOC allows both."""

from __future__ import annotations

import pytest

from src.plan_q15 import run_day_smart_q15_plan, timer_schedule_by_hour
from src.grid_config import merge_grid_defaults
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
)

# 2026-08-12 evening shape (Pi forecast + meter overlay H19–H23).
DATE = "2026-08-12"
CAP_KWH = 48.0

PV_TODAY = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.27, 1.611,
    3.18, 4.677, 6.092, 6.683, 5.773, 4.061, 3.887, 5.741,
    1.856, 2.312, 0.991, 0.037, 0.0, 0.0, 0.0, 0.0,
]
LOAD_TODAY = [
    1.057, 0.679, 0.492, 0.59, 0.577, 0.49, 0.479, 0.547,
    0.589, 0.529, 0.546, 0.898, 2.263, 6.801, 7.063, 2.669,
    1.497, 1.874, 1.087, 1.069, 0.456, 0.976, 1.074, 1.096,
]
PV_TOMORROW = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.022, 0.354, 0.977,
    1.915, 3.445, 5.129, 6.308, 6.914, 6.759, 5.457, 5.511,
    4.023, 2.716, 1.101, 0.121, 0.0, 0.0, 0.0, 0.0,
]
LOAD_TOMORROW = [
    0.959, 0.924, 0.571, 0.589, 0.588, 0.562, 0.499, 0.529,
    0.604, 0.627, 0.634, 1.02, 0.88, 0.721, 0.776, 0.573,
    0.776, 0.739, 0.77, 0.962, 1.232, 1.174, 1.169, 1.22,
]

# User RCE quarters H19–H22 (:00 :15 / :30 :45).
USER_RCE = {
    19: (1.273, 1.499, 1.774, 2.374),
    20: (2.088, 1.798, 1.668, 1.579),
    21: (0.608, 0.605, 0.718, 0.715),
    22: (0.897, 0.818, 0.795, 0.716),
}

# Timer Schedule: contiguous dusk window from the RCE peak (H20), leftover
# fills H21 then H22 as SOC allows.
EXPECTED = {
    75: {
        19: "Dis 19:00-20:00 8.0kW cap35%",
        20: "Dis 20:00-21:00 8.0kW cap34%",
        21: "Dis 21:00-21:45 6.5kW cap33%",
        22: "",
    },
    80: {
        19: "Dis 19:00-20:00 8.0kW cap35%",
        20: "Dis 20:00-21:00 8.0kW cap34%",
        21: "Dis 21:00-22:00 7.5kW cap32%",
        22: "",
    },
    85: {
        19: "Dis 19:00-20:00 8.0kW cap35%",
        20: "Dis 20:00-21:00 8.0kW cap34%",
        21: "Dis 21:00-22:00 7.0kW cap32%",
        22: "Dis 22:00-22:30 7.0kW cap31%",
    },
    90: {
        19: "Dis 19:00-20:00 8.0kW cap35%",
        20: "Dis 20:00-21:00 8.0kW cap34%",
        21: "Dis 21:00-22:00 7.5kW cap32%",
        22: "Dis 22:00-22:45 7.5kW cap30%",
    },
}


def _cfg() -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": CAP_KWH,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 17,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
                "pv_to_battery": 7.5,
            },
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.6229,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.75,
                "offpeak_energy_only_pln_kwh": 0.35,
                "peak_hours_weekday": [[6, 13], [15, 22]],
            },
        },
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 2.0,
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def _rce_quarters() -> list[float | None]:
    rce: list[float | None] = [None] * 96
    for h, qs in USER_RCE.items():
        for q, price in enumerate(qs):
            rce[h * 4 + q] = float(price)
    return rce


@pytest.mark.parametrize("soc_pct", [75, 80, 85, 90])
def test_evening_export_soc_ladder_fills_contiguous_from_peak(soc_pct: int):
    """Contiguous window from peak H20: leftover fills H21 then H22 as SOC allows."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    plan = run_day_smart_q15_plan(
        date_str=DATE,
        pv_hourly=list(PV_TODAY),
        load_hourly=list(LOAD_TODAY),
        tomorrow_pv=list(PV_TOMORROW),
        tomorrow_load=list(LOAD_TOMORROW),
        cfg=cfg,
        rce_quarters=_rce_quarters(),
        initial_soc_kwh=CAP_KWH * soc_pct / 100.0,
        from_hour=19,
        front_load_skip_leading_slots=0,
    )
    assert plan is not None
    timers = timer_schedule_by_hour(
        plan["q15_by_hour"], cfg, epsilon=float(params["epsilon_kwh"]),
    )
    expected = EXPECTED[soc_pct]
    for h in (19, 20, 21, 22):
        got = str(timers.get(h) or "").strip()
        assert got == expected[h], f"SOC {soc_pct}% H{h:02d}: {got!r} != {expected[h]!r}"
