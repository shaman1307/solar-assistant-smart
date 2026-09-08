"""Optimizer hour-sum correction for min_hourly_transfer_kwh."""

import pytest

from src.plan_q15 import run_day_smart_q15_plan
from src.plan_optimizer import HourControl, enforce_min_hourly_battery_grid_limits


def _cfg(**timer_schedule) -> dict:
    return {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "horizon_hours": 24,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.5,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.3,
            },
        },
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 2.0,
            **timer_schedule,
        },
    }


def _rce_peak_afternoon() -> list[float]:
    q = [0.4] * 96
    for h in range(16, 19):
        for qi in range(4):
            q[h * 4 + qi] = 0.55 + (h - 16) * 0.15 + qi * 0.05
    return q


def test_correct_zeros_sub_threshold_hour_export():
    """Any export below the hourly floor is cleared — including 1–2q orphans."""
    controls = [
        HourControl(0.0, 0.0),
        HourControl(0.0, 0.45),
        HourControl(0.0, 0.45),
        HourControl(0.0, 0.0),
    ]
    out = enforce_min_hourly_battery_grid_limits(
        controls,
        rce_step_offset=16 * 4,
        step_scale=0.25,
        min_hourly_kwh=2.0,
        epsilon=0.05,
    )
    assert all(c.battery_export_kwh == 0.0 for c in out)


def test_correct_zeros_single_quarter_orphan_export():
    """Pi bug: 0.5 kWh feed-in in q0 without a Dis timer must be wiped."""
    controls = [
        HourControl(0.0, 0.5),
        HourControl(0.0, 0.0),
        HourControl(0.0, 0.0),
        HourControl(0.0, 0.0),
    ]
    out = enforce_min_hourly_battery_grid_limits(
        controls,
        rce_step_offset=23 * 4,
        step_scale=0.25,
        min_hourly_kwh=2.0,
        epsilon=0.05,
    )
    assert all(c.battery_export_kwh == 0.0 for c in out)


def test_correct_keeps_export_at_or_above_floor():
    controls = [
        HourControl(0.0, 0.5),
        HourControl(0.0, 0.5),
        HourControl(0.0, 0.5),
        HourControl(0.0, 0.5),
    ]
    out = enforce_min_hourly_battery_grid_limits(
        controls,
        rce_step_offset=0,
        step_scale=0.25,
        min_hourly_kwh=2.0,
        epsilon=0.05,
    )
    assert sum(c.battery_export_kwh for c in out) == 2.0


def test_correct_zeros_sub_threshold_hour_charge():
    """Sub-min charge is cleared — do not inflate to the hour floor."""
    controls = [
        HourControl(0.4, 0.0),
        HourControl(0.4, 0.0),
        HourControl(0.0, 0.0),
        HourControl(0.0, 0.0),
    ]
    out = enforce_min_hourly_battery_grid_limits(
        controls,
        rce_step_offset=0,
        step_scale=0.25,
        min_hourly_kwh=2.0,
        epsilon=0.05,
    )
    assert sum(c.grid_charge_kw for c in out) == 0.0


def test_plan_no_sub_threshold_battery_export():
    """End-to-end: with min_hourly=2, no hour has 0 < battery grid export < 2."""
    pv = [0.0] * 24
    load = [0.7] * 24
    pv[16] = 4.61
    pv[17] = 3.04
    load[16] = 0.69
    load[17] = 0.82

    plan = run_day_smart_q15_plan(
        date_str="2026-07-16",
        pv_hourly=pv,
        load_hourly=load,
        tomorrow_pv=[0.5] * 24,
        tomorrow_load=[1.0] * 24,
        rce_quarters=_rce_peak_afternoon(),
        initial_soc_kwh=42.0,
        from_hour=0,
        cfg=_cfg(min_hourly_transfer_kwh=2.0),
    )
    assert plan
    eps = 0.05
    for h in range(24):
        slots = (plan.get("q15_by_hour") or {}).get(h) or []
        batt_grid = sum(max(0.0, float(s.get("battery_export_kwh") or 0)) for s in slots)
        assert not (eps < batt_grid < 2.0), f"hour {h}: sub-threshold export {batt_grid}"


def test_no_grid_export_when_evening_soc_only_covers_night():
    """Evening sun must not collapse reserve — no Dis when SOC is night-critical."""
    pv = [0.0] * 24
    load = [0.9] * 24
    # Hour 17–18 still sunny enough to cover house; overnight reserve must remain.
    pv[17] = 1.2
    pv[18] = 1.0
    rce = [0.4] * 96
    for h in range(17, 22):
        for qi in range(4):
            rce[h * 4 + qi] = 0.75  # above export threshold

    # Dark morning so reserve spans the full night (not tomorrow hour-0 sun).
    tomorrow_pv = [0.0] * 8 + [2.0] * 16
    tomorrow_load = [0.9] * 24
    initial = 43.0 * 0.38  # ~16.3 kWh — only covers overnight need
    # Weekday evening = G12 peak → no grid charge to inflate SOC above reserve.
    plan = run_day_smart_q15_plan(
        date_str="2026-07-16",
        pv_hourly=pv,
        load_hourly=load,
        tomorrow_pv=tomorrow_pv,
        tomorrow_load=tomorrow_load,
        rce_quarters=rce,
        initial_soc_kwh=initial,
        from_hour=17,
        cfg=_cfg(min_hourly_transfer_kwh=0.0),
    )
    assert plan
    for h in (17, 18, 19):
        slots = (plan.get("q15_by_hour") or {}).get(h) or []
        batt_grid = sum(float(s.get("battery_export_kwh") or 0) for s in slots)
        assert batt_grid < 0.05, f"hour {h}: unexpected grid export {batt_grid}"
