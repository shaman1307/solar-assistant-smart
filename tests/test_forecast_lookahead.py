"""Forecast lookahead through end of tomorrow for reserve / charge-target."""

from __future__ import annotations

from datetime import datetime

from src.plan_q15 import STEP_SCALE, run_rolling_smart_q15_plan
from src.plan_optimizer import (
    grid_charge_target_soc_kwh_from_step,
    tomorrow_lookahead_start_hour,
    build_extended_buy_for_reserve,
    build_extended_pv_load_for_reserve,
)
from src.simulation_config import PLAN_HORIZON_HOURS, hours_until_end_of_tomorrow


def _cfg() -> dict:
    return {
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 18,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_battery": 25.0,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.24,
                "offpeak_price_pln_kwh": 0.62,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.4,
            },
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.62,
        },
        "timer_schedule": {"min_hourly_transfer_kwh": 2.0, "min_block_minutes": 30},
    }


def test_hours_until_end_of_tomorrow():
    assert hours_until_end_of_tomorrow(0) == 48
    assert hours_until_end_of_tomorrow(5) == 43
    assert hours_until_end_of_tomorrow(23) == 25
    assert PLAN_HORIZON_HOURS == 24


def test_tomorrow_lookahead_start_hour_full_day_when_plan_ends_today():
    today = datetime(2026, 8, 9).date()
    end = datetime(2026, 8, 9, 23, 45)
    assert tomorrow_lookahead_start_hour(
        end_dt=end, today_date=today, series_len=19 * 4, global_step_offset=5 * 4,
        step_scale=STEP_SCALE,
    ) == 0


def test_tomorrow_lookahead_start_hour_tail_after_rolling_window():
    today = datetime(2026, 8, 9).date()
    end = datetime(2026, 8, 10, 4, 45)
    assert tomorrow_lookahead_start_hour(
        end_dt=end, today_date=today, series_len=(19 + 5) * 4, global_step_offset=5 * 4,
        step_scale=STEP_SCALE,
    ) == 5


def test_extend_appends_only_missing_tomorrow_tail():
    """Plan ends tomorrow H04 → append H05–H23 only (not a second full day)."""
    today = datetime(2026, 8, 9).date()
    end = datetime(2026, 8, 10, 4, 45)
    steps = (19 + 5) * 4
    offset = 5 * 4
    pv = [0.1] * steps
    load = [0.2] * steps
    forecast = {
        "today": {"pv": [0.0] * 24, "load": [0.5] * 24},
        "tomorrow": {
            "pv": [float(h) for h in range(24)],
            "load": [1.0] * 24,
        },
    }
    pv_x, load_x = build_extended_pv_load_for_reserve(
        pv, load,
        step_scale=STEP_SCALE,
        end_dt=end,
        today_date=today,
        forecast=forecast,
        global_step_offset=offset,
    )
    assert len(pv_x) == steps + 19 * 4
    assert abs(pv_x[steps] - 5.0 * STEP_SCALE) < 1e-9
    assert abs(pv_x[-1] - 23.0 * STEP_SCALE) < 1e-9
    assert len(load_x) == len(pv_x)


def test_extend_full_tomorrow_when_plan_ends_today():
    today = datetime(2026, 8, 9).date()
    end = datetime(2026, 8, 9, 23, 45)
    steps = 19 * 4
    offset = 5 * 4
    pv = [0.1] * steps
    load = [0.2] * steps
    forecast = {
        "today": {"pv": [0.0] * 24, "load": [0.5] * 24},
        "tomorrow": {"pv": [3.0] * 24, "load": [1.0] * 24},
    }
    pv_x, _ = build_extended_pv_load_for_reserve(
        pv, load,
        step_scale=STEP_SCALE,
        end_dt=end,
        today_date=today,
        forecast=forecast,
        global_step_offset=offset,
    )
    assert len(pv_x) == steps + 24 * 4


def test_extend_buy_includes_monday_morning_peak():
    today = datetime(2026, 8, 9).date()  # Sunday
    end = datetime(2026, 8, 10, 4, 45)  # Monday early
    steps = (19 + 5) * 4
    offset = 5 * 4
    buy = [0.62] * steps
    forecast = {
        "today": {"pv": [0.0] * 24, "load": [0.5] * 24},
        "tomorrow": {"pv": [0.0] * 24, "load": [0.5] * 24},
    }
    cfg = _cfg()
    buy_x = build_extended_buy_for_reserve(
        buy,
        step_scale=STEP_SCALE,
        end_dt=end,
        today_date=today,
        forecast=forecast,
        cfg=cfg,
        global_step_offset=offset,
    )
    peak = float(cfg["grid"]["g12"]["peak_price_pln_kwh"])
    off = float(cfg["grid"]["g12"]["offpeak_price_pln_kwh"])
    assert abs(buy_x[steps] - off) < 0.01  # H05
    assert abs(buy_x[steps + 4] - peak) < 0.01  # H06


def test_charge_target_on_tomorrow_h01_sees_morning_peak():
    """With lookahead tail, a step inside tomorrow H01 has peak deficit ahead."""
    today = datetime(2026, 8, 9).date()
    end = datetime(2026, 8, 10, 4, 45)
    steps = (19 + 5) * 4
    offset = 5 * 4
    pv = [0.0] * steps
    load = [0.2] * steps
    buy = [0.62] * steps
    forecast = {
        "today": {"pv": [0.0] * 24, "load": [0.5] * 24},
        "tomorrow": {
            "pv": [0.0] * 10 + [4.0] * 14,
            "load": [0.8] * 24,
        },
    }
    cfg = _cfg()
    pv_x, load_x = build_extended_pv_load_for_reserve(
        pv, load, step_scale=STEP_SCALE, end_dt=end, today_date=today, forecast=forecast,
        global_step_offset=offset,
    )
    buy_x = build_extended_buy_for_reserve(
        buy, step_scale=STEP_SCALE, end_dt=end, today_date=today,
        forecast=forecast, cfg=cfg, global_step_offset=offset,
    )
    step_h01 = 19 * 4 + 4
    floor = 48.0 * 0.18
    target = grid_charge_target_soc_kwh_from_step(
        step_h01, pv_x, load_x, buy_x,
        floor, 0.925, 0.925, 0.05, 0.62,
        slots_per_hour=4,
        global_step_offset=offset,
    )
    assert target > floor + 0.5


def test_rolling_plan_returns_24h_split_across_midnight():
    cfg = _cfg()
    pv_today = [0.0] * 5 + [1.0] * 19
    load_today = [0.5] * 24
    pv_tom = [0.0] * 6 + [2.0] * 18
    load_tom = [0.6] * 24
    out = run_rolling_smart_q15_plan(
        date_str="2026-08-09",
        pv_hourly=pv_today,
        load_hourly=load_today,
        tomorrow_pv=pv_tom,
        tomorrow_load=load_tom,
        cfg=cfg,
        initial_soc_kwh=20.0,
        from_hour=5,
        horizon_hours=24,
    )
    assert out is not None
    today = out["today"]
    tomorrow = out["tomorrow"]
    assert today is not None and tomorrow is not None
    assert any(today["q15_by_hour"].get(h) for h in range(5, 24))
    assert not today["q15_by_hour"].get(0)
    assert any(tomorrow["q15_by_hour"].get(h) for h in range(0, 5))
    assert not tomorrow["q15_by_hour"].get(5)
    assert not tomorrow["q15_by_hour"].get(12)
