"""SA timer power uses full AC capacity (inverter splits load vs grid)."""

from src.simulation_config import (
    normalize_battery_power_limits,
    plan_timer_charge_grid_kw,
    plan_timer_charge_power_kw,
    plan_timer_discharge_ac_kw,
    plan_timer_discharge_power_kw,
)
from src.timer_plan import (
    ACTION_CHARGE_GRID,
    ACTION_DISCHARGE_GRID,
    ACTION_DISCHARGE_LOAD,
    ACTION_IDLE_PV,
    build_hour_timer_schedule,
    derive_timer_schedule_q15,
)

_CFG = {
    "inverter": {"ac_capacity_kw": 8.0},
    "battery": {
        "capacity_kwh": 43.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 8.0,
    },
    "simulation": {"min_soc_pct": 17},
}


def _discharge_slot(grid_export_kwh: float = 1.5) -> dict:
    return {
        "action": ACTION_DISCHARGE_GRID,
        "battery_delta": -2.0,
        "grid_export": grid_export_kwh,
        "battery_export_kwh": grid_export_kwh,
        "grid_import": 0.0,
        "pv": 0.0,
    }


def test_leftover_quarter_extends_to_min_block_at_half_power():
    """One full-power quarter of leftover energy → Dis 30 min at ~half kW."""
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "timer_schedule": {"min_block_minutes": 30},
    }
    slots = [
        {
            "action": ACTION_DISCHARGE_GRID,
            "battery_delta": -2.0,
            "grid_export": 1.6,
            "battery_export_kwh": 1.6,
            "grid_import": 0.0,
            "pv": 0.0,
            "load": 0.25,
        },
        {
            "action": "Discharging to Load",
            "battery_delta": -0.27,
            "grid_export": 0.0,
            "battery_export_kwh": 0.0,
            "grid_import": 0.0,
            "pv": 0.0,
            "load": 0.25,
        },
        {
            "action": "Discharging to Load",
            "battery_delta": -0.27,
            "grid_export": 0.0,
            "battery_export_kwh": 0.0,
            "grid_import": 0.0,
            "pv": 0.0,
            "load": 0.25,
        },
        {
            "action": "Discharging to Load",
            "battery_delta": -0.27,
            "grid_export": 0.0,
            "battery_export_kwh": 0.0,
            "grid_import": 0.0,
            "pv": 0.0,
            "load": 0.25,
        },
    ]
    txt = build_hour_timer_schedule(
        23, slots, cfg, action=ACTION_DISCHARGE_GRID, grid_export=1.6,
    )
    assert "23:00-23:30" in txt
    assert "4.0kW" in txt or "4kW" in txt
    assert "8.0kW" not in txt


def test_hour_timer_discharge_uses_discharge_cap():
    slots = [_discharge_slot(1.5) for _ in range(4)]
    txt = build_hour_timer_schedule(
        22, slots, _CFG, action=ACTION_DISCHARGE_GRID, grid_export=6.0,
    )
    assert "6.5kW" in txt


def test_hour_timer_discharge_half_power_for_two_kwh_in_30min():
    """~2 kWh export only in 30 min → ~4.5kW timer (physics, no parallel load)."""
    slots = [
        {**_discharge_slot(0.0), "quarter": 0, "load": 0.0},
        {**_discharge_slot(1.0), "quarter": 1, "load": 0.0},
        {**_discharge_slot(1.0), "quarter": 2, "load": 0.0},
        {**_discharge_slot(0.0), "quarter": 3, "load": 0.0},
    ]
    txt = build_hour_timer_schedule(
        0, slots, _CFG, action=ACTION_DISCHARGE_GRID, grid_export=2.0,
    )
    assert "4.5kW" in txt
    assert "8.0kW" not in txt


def test_hour_timer_discharge_includes_parallel_load():
    """Export + house load in same 30 min window → ~6 kW DC timer."""
    cfg = {
        **_CFG,
        "simulation": {
            "min_soc_pct": 16,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
    }
    slots = [
        {"action": ACTION_DISCHARGE_GRID, "battery_delta": -0.3, "grid_export": 0.0,
         "battery_export_kwh": 0.0, "grid_import": 0.0, "pv": 0.0, "load": 0.2485},
        {"action": ACTION_DISCHARGE_GRID, "battery_delta": -1.2, "grid_export": 0.8672,
         "battery_export_kwh": 0.8672, "grid_import": 0.0, "pv": 0.0, "load": 0.296},
        {"action": ACTION_DISCHARGE_GRID, "battery_delta": -1.5, "grid_export": 1.25,
         "battery_export_kwh": 1.25, "grid_import": 0.0, "pv": 0.0, "load": 0.1681},
        {"action": ACTION_DISCHARGE_LOAD, "battery_delta": -0.17, "grid_export": 0.0,
         "battery_export_kwh": 0.0, "grid_import": 0.0, "pv": 0.0, "load": 0.1542},
    ]
    txt = build_hour_timer_schedule(
        0, slots, cfg, action=ACTION_DISCHARGE_GRID, grid_export=2.117,
    )
    assert "6.0kW" in txt
    assert "Dis 00:15-00:45" in txt


def test_hour_timer_discharge_subtracts_pv_from_load():
    """PV covers part of load — battery timer lower than export+load alone."""
    cfg = {
        **_CFG,
        "simulation": {
            "min_soc_pct": 16,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
    }
    slots = [
        {"action": ACTION_DISCHARGE_LOAD, "battery_delta": -0.03, "grid_export": 0.0,
         "battery_export_kwh": 0.0, "grid_import": 0.0, "pv": 0.1797, "load": 0.2107},
        {"action": ACTION_DISCHARGE_LOAD, "battery_delta": -0.03, "grid_export": 0.0,
         "battery_export_kwh": 0.0, "grid_import": 0.0, "pv": 0.1797, "load": 0.2107},
        {"action": ACTION_DISCHARGE_GRID, "battery_delta": -0.5, "grid_export": 0.4625,
         "battery_export_kwh": 0.4625, "grid_import": 0.0, "pv": 0.1797, "load": 0.2107},
        {"action": ACTION_DISCHARGE_GRID, "battery_delta": -0.5, "grid_export": 0.4625,
         "battery_export_kwh": 0.4625, "grid_import": 0.0, "pv": 0.1797, "load": 0.2107},
    ]
    txt = build_hour_timer_schedule(
        19, slots, cfg, action=ACTION_DISCHARGE_GRID, grid_export=0.925,
    )
    assert "2.5kW" in txt
    assert "Dis 19:30-20:00" in txt


def test_derive_timer_schedule_q15_discharge_power():
    rows = []
    for q in range(4):
        rows.append({
            "start": f"2026-06-26 22:{q * 15:02d}",
            "action": ACTION_DISCHARGE_GRID,
            "battery": -1.5,
            "grid_import": 0,
            "grid_export": 1.5,
            "soc": 55,
        })
    sched = derive_timer_schedule_q15(rows, _CFG)
    dis = sched["discharge_slots"][0]
    assert dis["power_kw"] == 6.5


def test_derive_timer_schedule_q15_half_discharge_power():
    rows = []
    for q in range(4):
        rows.append({
            "start": f"2026-07-12 00:{q * 15:02d}",
            "action": ACTION_DISCHARGE_GRID if q in (1, 2) else ACTION_DISCHARGE_LOAD,
            "battery": -2.0 if q in (1, 2) else -0.2,
            "grid_import": 0,
            "grid_export": 1.0 if q in (1, 2) else 0.0,
            "soc": 25,
        })
    sched = derive_timer_schedule_q15(rows, _CFG)
    dis = sched["discharge_slots"][0]
    assert dis["power_kw"] == 4.5


def test_charge_timer_uses_needed_power_not_always_max():
    """Bat +2.5 kWh in 30 min → ~5 kW (min hourly 2 → floor 4 kW), not 6."""
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
    }
    slots = [
        {
            "action": ACTION_CHARGE_GRID,
            "battery_delta": 1.25,
            "grid_import": 1.35,
            "grid_export": 0.0,
            "pv": 0.0,
            "soc_pct": 21.0,
        },
        {
            "action": ACTION_CHARGE_GRID,
            "battery_delta": 1.25,
            "grid_import": 1.35,
            "grid_export": 0.0,
            "pv": 0.0,
            "soc_pct": 23.5,
        },
        {
            "action": ACTION_DISCHARGE_LOAD,
            "battery_delta": -0.1,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "pv": 0.0,
            "soc_pct": 23.3,
        },
        {
            "action": ACTION_DISCHARGE_LOAD,
            "battery_delta": -0.1,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "pv": 0.0,
            "soc_pct": 23.1,
        },
    ]
    txt = build_hour_timer_schedule(
        3, slots, cfg, action=ACTION_CHARGE_GRID,
    )
    assert "03:00-03:30" in txt
    assert "5.0kW" in txt
    assert "6.0kW" not in txt


def test_charge_timer_floors_at_min_hourly_transfer():
    """Tiny charge in 30 min still ≥ min_hourly/duration (2 kWh / 0.5 h → 4 kW)."""
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
    }
    slots = [
        {
            "action": ACTION_CHARGE_GRID,
            "battery_delta": 0.3,
            "grid_import": 0.35,
            "grid_export": 0.0,
            "pv": 0.0,
            "soc_pct": 18.5,
        },
        {
            "action": ACTION_CHARGE_GRID,
            "battery_delta": 0.3,
            "grid_import": 0.35,
            "grid_export": 0.0,
            "pv": 0.0,
            "soc_pct": 19.0,
        },
        {"action": ACTION_DISCHARGE_LOAD, "battery_delta": -0.1,
         "grid_import": 0.0, "grid_export": 0.0, "pv": 0.0, "soc_pct": 18.8},
        {"action": ACTION_DISCHARGE_LOAD, "battery_delta": -0.1,
         "grid_import": 0.0, "grid_export": 0.0, "pv": 0.0, "soc_pct": 18.6},
    ]
    txt = build_hour_timer_schedule(
        3, slots, cfg, action=ACTION_CHARGE_GRID,
    )
    assert "4.0kW" in txt


def test_charge_timer_capped_at_battery_max():
    cfg = {
        **_CFG,
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {**_CFG["battery"], "max_charge_power_kw": 5.0},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
    }
    # 5 kWh over 60 min → needs 5 kW (hits battery max).
    slots = [{
        "action": ACTION_CHARGE_GRID,
        "battery_delta": 1.25,
        "grid_import": 1.4,
        "grid_export": 0.0,
        "pv": 0.0,
        "soc_pct": 20.0 + i,
    } for i in range(4)]
    txt = build_hour_timer_schedule(
        2, slots, cfg, action=ACTION_CHARGE_GRID,
    )
    assert "5.0kW" in txt
    assert "8.0kW" not in txt


def test_normalize_clamps_above_hardware_max():
    cfg = {
        "inverter": {"ac_capacity_kw": 10.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 9.0,
            "max_discharge_power_kw": 12.0,
        },
        "simulation": {"min_soc_pct": 17, "losses_pct": {
            "grid_to_battery": 7.5,
            "battery_to_load_or_grid": 7.5,
            "pv_to_grid": 7.5,
        }},
    }
    normalize_battery_power_limits(cfg)
    assert cfg["battery"]["max_charge_power_kw"] == 9.0
    assert cfg["battery"]["max_discharge_power_kw"] == 8.0
    assert plan_timer_charge_power_kw(cfg) == 9.0
    assert plan_timer_discharge_power_kw(cfg) == 8.0
    cfg["battery"]["max_charge_power_kw"] = 20.0
    normalize_battery_power_limits(cfg)
    assert cfg["battery"]["max_charge_power_kw"] == 9.2
    assert plan_timer_charge_power_kw(cfg) == 9.2


def test_model_applies_config_losses_to_timer_caps():
    cfg = {
        **_CFG,
        "simulation": {
            "min_soc_pct": 17,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
            },
        },
    }
    assert plan_timer_discharge_power_kw(cfg) == 8.0
    # AC equivalent of DC timer power (reference only; optimizer uses DC 8.0).
    assert plan_timer_discharge_ac_kw(cfg) == 7.4
    assert plan_timer_charge_power_kw(cfg) == 5.0
    assert plan_timer_charge_grid_kw(cfg) == 5.41
