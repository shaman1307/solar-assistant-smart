"""Plan simulation parameters loaded from sa-config.yaml."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Rolling Energy arbitrage plan window (displayed / stored rows).
PLAN_HORIZON_HOURS = 24
_HOURS_PER_DAY = 24


def hours_until_end_of_tomorrow(from_hour: int) -> int:
    """Hours of forecast from *from_hour* today through 23:00 tomorrow (inclusive).

    Used as lookahead length for reserve / charge-target — longer than the
    rolling plan window when *from_hour* > 0.
    """
    h = max(0, min(23, int(from_hour)))
    return (_HOURS_PER_DAY - h) + _HOURS_PER_DAY

DEFAULT_SIMULATION: dict[str, Any] = {
    "min_soc_pct": 15,
    "epsilon_kwh": 0.05,
    "losses_pct": {
        "grid_to_battery": 7.5,
        "battery_to_load_or_grid": 7.5,
        "pv_to_battery": 7.5,
        "pv_to_grid": 7.5,
        "pv_to_load": 7.5,
    },
}

DEFAULT_TIMER_SCHEDULE: dict[str, Any] = {
    "min_block_minutes": 30,
    # Minimum hourly battery charge (import) / discharge (export) in kWh — smart Bat Charge/Discharge.
    "min_hourly_transfer_kwh": 2.0,
}

RESERVE_MIN_SOC_MARGIN = 1.0

MAX_BATTERY_CHARGE_POWER_KW = 6.0
MAX_BATTERY_DISCHARGE_POWER_KW = 8.0
DEFAULT_BATTERY_MAX_CHARGE_POWER_KW = 6.0
DEFAULT_BATTERY_MAX_DISCHARGE_POWER_KW = 8.0


def merge_battery_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure cfg['battery'] has timer power limit fields."""
    bat = cfg.setdefault("battery", {})
    bat.setdefault("max_charge_power_kw", DEFAULT_BATTERY_MAX_CHARGE_POWER_KW)
    bat.setdefault("max_discharge_power_kw", DEFAULT_BATTERY_MAX_DISCHARGE_POWER_KW)
    return cfg


def normalize_battery_power_limits(cfg: dict[str, Any]) -> dict[str, Any]:
    """Clamp battery timer power limits to allowed hardware ceilings."""
    merge_battery_defaults(cfg)
    bat = cfg["battery"]
    bat["max_charge_power_kw"] = round(
        min(MAX_BATTERY_CHARGE_POWER_KW, max(0.1, float(bat["max_charge_power_kw"]))),
        2,
    )
    bat["max_discharge_power_kw"] = round(
        min(MAX_BATTERY_DISCHARGE_POWER_KW, max(0.1, float(bat["max_discharge_power_kw"]))),
        2,
    )
    return cfg


def plan_timer_charge_power_kw(cfg: dict[str, Any]) -> float:
    """SA timer charge power (kW DC into battery)."""
    normalize_battery_power_limits(cfg)
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    return round(min(ac_kw, float(cfg["battery"]["max_charge_power_kw"])), 2)


def plan_timer_discharge_power_kw(cfg: dict[str, Any]) -> float:
    """SA timer discharge power (kW DC from battery)."""
    normalize_battery_power_limits(cfg)
    ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    return round(min(ac_kw, float(cfg["battery"]["max_discharge_power_kw"])), 2)


def plan_timer_discharge_ac_kw(cfg: dict[str, Any]) -> float:
    """AC output cap (kW) at timer DC discharge — DC × (1 − battery_out loss %)."""
    eta_out = get_simulation_params(cfg)["eta_battery_out"]
    return round(plan_timer_discharge_power_kw(cfg) * float(eta_out), 2)


def plan_timer_charge_grid_kw(cfg: dict[str, Any]) -> float:
    """Grid import (kW AC) to sustain timer DC charge — DC / (1 − grid_to_battery loss %)."""
    eta_grid = get_simulation_params(cfg)["eta_grid_battery"]
    dc_kw = plan_timer_charge_power_kw(cfg)
    if eta_grid <= 0:
        return dc_kw
    return round(dc_kw / float(eta_grid), 2)


def simulation_form_defaults() -> dict[str, float | int]:
    """Flat Configuration-form defaults (HTML inputs and JS populateForm)."""
    losses = DEFAULT_SIMULATION["losses_pct"]
    return {
        "simulation.min_soc_pct": int(DEFAULT_SIMULATION["min_soc_pct"]),
        "simulation.losses_pct.pv_to_battery": float(losses["pv_to_battery"]),
        "simulation.losses_pct.grid_to_battery": float(losses["grid_to_battery"]),
        "simulation.losses_pct.battery_to_load_or_grid": float(losses["battery_to_load_or_grid"]),
        "simulation.losses_pct.pv_to_grid": float(losses["pv_to_grid"]),
        "simulation.losses_pct.pv_to_load": float(losses["pv_to_load"]),
        "timer_schedule.min_block_minutes": int(DEFAULT_TIMER_SCHEDULE["min_block_minutes"]),
        "timer_schedule.min_hourly_transfer_kwh": float(
            DEFAULT_TIMER_SCHEDULE["min_hourly_transfer_kwh"]
        ),
        "battery.max_charge_power_kw": float(DEFAULT_BATTERY_MAX_CHARGE_POWER_KW),
        "battery.max_discharge_power_kw": float(DEFAULT_BATTERY_MAX_DISCHARGE_POWER_KW),
    }


def merge_timer_schedule_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure cfg['timer_schedule'] exists with default values (in-memory merge only)."""
    ts = cfg.setdefault("timer_schedule", {})
    for key, value in DEFAULT_TIMER_SCHEDULE.items():
        ts.setdefault(key, value)
    return cfg


def merge_simulation_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure cfg['simulation'] exists with default values (in-memory merge only)."""
    sim = cfg.setdefault("simulation", {})
    for key, value in DEFAULT_SIMULATION.items():
        if key == "losses_pct":
            losses = sim.setdefault("losses_pct", {})
            for loss_key, loss_val in value.items():
                losses.setdefault(loss_key, loss_val)
        else:
            sim.setdefault(key, value)
    return cfg


def get_timer_schedule_params(cfg: dict[str, Any]) -> dict[str, float | int]:
    """Return normalized Timer Schedule thresholds from config."""
    ts = merge_timer_schedule_defaults(deepcopy(cfg)).get("timer_schedule", {})
    min_block = int(ts.get("min_block_minutes", DEFAULT_TIMER_SCHEDULE["min_block_minutes"]))
    min_block = max(15, min(60, min_block))
    if min_block % 15 != 0:
        min_block = max(15, (min_block // 15) * 15)
    min_hourly = float(ts.get("min_hourly_transfer_kwh", DEFAULT_TIMER_SCHEDULE["min_hourly_transfer_kwh"]))
    return {
        "min_block_minutes": min_block,
        "min_hourly_transfer_kwh": max(0.0, min_hourly),
    }


def plan_timer_min_block_minutes(cfg: dict[str, Any]) -> int:
    return int(get_timer_schedule_params(cfg)["min_block_minutes"])


def plan_timer_min_hourly_transfer_kwh(cfg: dict[str, Any]) -> float:
    return float(get_timer_schedule_params(cfg)["min_hourly_transfer_kwh"])


def get_simulation_params(cfg: dict[str, Any]) -> dict[str, float | int]:
    """Return normalized simulation settings (efficiencies, limits, thresholds)."""
    sim = merge_simulation_defaults(deepcopy(cfg)).get("simulation", {})
    losses = sim.get("losses_pct") or {}

    def loss_pct(key: str) -> float:
        return float(losses.get(key, DEFAULT_SIMULATION["losses_pct"][key]))

    def eta(key: str) -> float:
        return 1.0 - loss_pct(key) / 100.0

    return {
        "min_soc_pct": float(sim["min_soc_pct"]),
        "horizon_hours": PLAN_HORIZON_HOURS,
        "epsilon_kwh": float(sim["epsilon_kwh"]),
        "eta_grid_battery": eta("grid_to_battery"),
        "eta_battery_out": eta("battery_to_load_or_grid"),
        "eta_pv_battery": eta("pv_to_battery"),
        "eta_pv_grid": eta("pv_to_grid"),
        "eta_pv_load": eta("pv_to_load"),
    }


def plan_min_soc_pct(cfg: dict[str, Any]) -> float:
    return float(get_simulation_params(cfg)["min_soc_pct"])


def plan_min_soc_kwh(cfg: dict[str, Any]) -> float:
    cap = float(cfg["battery"]["capacity_kwh"])
    return (plan_min_soc_pct(cfg) / 100.0) * cap


def plan_reserve_min_soc_pct(cfg: dict[str, Any]) -> float:
    """Night-reserve floor equals min_soc_pct exactly (no extra margin)."""
    return min(100.0, plan_min_soc_pct(cfg) * RESERVE_MIN_SOC_MARGIN)


def plan_reserve_min_soc_kwh(cfg: dict[str, Any]) -> float:
    """kWh floor for survive-until-PV reserve; charging/discharge hard limit stays min_soc."""
    cap = float(cfg["battery"]["capacity_kwh"])
    return (plan_reserve_min_soc_pct(cfg) / 100.0) * cap
