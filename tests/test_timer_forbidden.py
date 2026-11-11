"""Forbidden clock hours drop the charge or export slot from the EA replay."""

from datetime import datetime

from src.plan_physics import HourControl
from src.plan_q15 import simulate_q15_controls
from src.timer_plan import (
    ACTION_CHARGE_GRID,
    ACTION_DISCHARGE_GRID,
    build_hour_timer_schedule,
    normalize_timer_forbidden,
    timer_action_forbidden,
)


def _replay(controls, *, start_step: int, cfg: dict, soc: float = 20.0):
    n = len(controls)
    return simulate_q15_controls(
        controls,
        [0.0] * n,
        start_step=start_step,
        steps=n,
        initial_soc_kwh=soc,
        pv_q=[0.0] * n,
        load_q=[0.0] * n,
        rce_q=[None] * (start_step + n),
        start_dt=datetime(2026, 11, 8),
        cfg=cfg,
        battery_cap=47.6,
        min_kwh=0.0,
        discharge_dc_kw=8.0,
        inverter_ac_kw=8.0,
        eps_q=0.001,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
    )


def test_forbidden_rows_are_hour_spans():
    rows = normalize_timer_forbidden([
        {"from_hour": 22, "to_hour": 6, "applies_to": "both"},
        {"from_hour": 5, "to_hour": 5, "applies_to": "charge"},
        {"from_hour": 9, "to_hour": 12, "applies_to": "nope"},
    ])
    assert rows == [{"from_hour": 22, "to_hour": 6, "applies_to": "both"}]
    cfg = {"timer_schedule": {"forbidden": rows}}
    assert timer_action_forbidden(cfg, 23, "charge")
    assert timer_action_forbidden(cfg, 0, "discharge")
    assert timer_action_forbidden(cfg, 5, "charge")
    assert not timer_action_forbidden(cfg, 6, "charge")


def test_simulation_drops_forbidden_charge_and_timer():
    cfg = {
        "battery": {"capacity_kwh": 47.6, "max_charge_power_kw": 5.8},
        "simulation": {"min_soc_pct": 16},
        "inverter": {"ac_capacity_kw": 8.0},
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 0.5,
            "forbidden": [{"from_hour": 5, "to_hour": 6, "applies_to": "charge"}],
        },
    }
    q15, _rows, soc = _replay([HourControl(1.0, 0.0)] * 4, start_step=5 * 4, cfg=cfg, soc=10.0)
    assert all(s["grid_import"] == 0.0 for s in q15[5])
    assert soc == 10.0
    kept, _rows, kept_soc = _replay([HourControl(1.0, 0.0)] * 4, start_step=4 * 4, cfg=cfg, soc=10.0)
    assert sum(s["grid_import"] for s in kept[4]) == 4.0
    assert kept_soc == 14.0
    assert build_hour_timer_schedule(
        5, q15[5], cfg, action=ACTION_CHARGE_GRID, bat_charge=4.0,
    ) == ""


def test_override_day_drops_forbidden_charge_on_horizon_controls():
    """A manual cell keeps its own slot. Other hours keep the horizon plan and still obey forbid."""
    import src.plan_timer_override as override_mod

    cfg = {
        "battery": {"capacity_kwh": 47.6, "max_charge_power_kw": 5.8},
        "simulation": {"min_soc_pct": 16},
        "inverter": {"ac_capacity_kw": 8.0},
        "grid": {
            "g12": {
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
            },
        },
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 0.5,
            "forbidden": [{"from_hour": 2, "to_hour": 4, "applies_to": "charge"}],
        },
    }
    n = 24 * 4
    horizon = {
        hour: [
            {
                "quarter": q,
                "grid_charge_kw": 1.0,
                "ctrl_battery_export_kwh": 0.0,
                "load_from_grid": False,
            }
            for q in range(4)
        ]
        for hour in (2, 3, 4)
    }
    plan = override_mod.replay_day_plan_with_timer_overrides(
        {"q15_by_hour": horizon, "q15_plan_rows": []},
        {1: "Chg 01:00-02:00 4.0kW cap30%"},
        date_str="2026-11-08",
        pv_q=[0.0] * n,
        load_q=[0.0] * n,
        buy_q=[0.6] * n,
        rce_q=[None] * n,
        cfg=cfg,
        from_hour=1,
        initial_soc_kwh=10.0,
        gap_mode="optimize",
    )
    by_hour = plan["q15_by_hour"]
    assert sum(s["grid_import"] for s in by_hour[1]) > 0.0
    assert all(s["grid_import"] == 0.0 for s in by_hour[2])
    assert all(s["grid_import"] == 0.0 for s in by_hour[3])
    assert sum(s["grid_import"] for s in by_hour[4]) > 0.0


def test_simulation_drops_forbidden_export_only():
    cfg = {
        "battery": {"capacity_kwh": 47.6, "max_discharge_power_kw": 8.0},
        "simulation": {"min_soc_pct": 16},
        "inverter": {"ac_capacity_kw": 8.0},
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 0.5,
            "forbidden": [{"from_hour": 18, "to_hour": 19, "applies_to": "discharge"}],
        },
    }
    q15, _rows, soc = _replay([HourControl(0.0, 1.0)] * 4, start_step=18 * 4, cfg=cfg, soc=20.0)
    assert all(s["grid_export"] == 0.0 for s in q15[18])
    assert soc == 20.0
    charge, _rows, charge_soc = _replay(
        [HourControl(1.0, 0.0)] * 4, start_step=18 * 4, cfg=cfg, soc=10.0,
    )
    assert sum(s["grid_import"] for s in charge[18]) == 4.0
    assert charge_soc == 14.0
    assert build_hour_timer_schedule(
        18, q15[18], cfg, action=ACTION_DISCHARGE_GRID, grid_export=4.0,
    ) == ""
