"""
Default hybrid inverter simulation — load priority, rules off.

Physical limits only:
  - battery capacity and minimum SOC floor
  - inverter AC throughput per hour

Energy is tracked in kWh 1:1 (same basis as Influx hourly accruals). Losses are not
applied again — SA meter history already reflects them.

No forecast, no optimizer ceilings, no grid-charge rules, no battery export commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import load_config
from .plan_physics import HourControl, simulate_hour
from .simulation_config import get_simulation_params, merge_simulation_defaults


@dataclass
class HourSimResult:
    soc_kwh: float
    soc_pct: float
    grid_import: float
    grid_export: float
    bat_charge: float
    bat_discharge: float


def _f(val: float | None) -> float | None:
    if val is None:
        return None
    return float(val)


def resolve_hour0_soc_kwh(
    hourly: dict[str, list],
    battery_cap: float,
    *,
    override_pct: float | None = None,
    override_kwh: float | None = None,
) -> tuple[float, float]:
    """SOC at start of hour 0 (override or backtrack from first hour-0 reading)."""
    if override_kwh is not None:
        start_kwh = max(0.0, min(battery_cap, float(override_kwh)))
        return start_kwh, (start_kwh / battery_cap) * 100.0 if battery_cap else 0.0
    if override_pct is not None:
        start_kwh = max(0.0, min(battery_cap, (float(override_pct) / 100.0) * battery_cap))
        return start_kwh, float(override_pct)

    soc_series = hourly.get("soc") or [None] * 24
    pct = soc_series[0]
    if pct is not None:
        end_kwh = (float(pct) / 100.0) * battery_cap
        bc = float(hourly.get("bat_charge", [None] * 24)[0] or 0.0)
        bd = float(hourly.get("bat_discharge", [None] * 24)[0] or 0.0)
        start_kwh = max(0.0, min(battery_cap, end_kwh - bc + bd))
        return start_kwh, (start_kwh / battery_cap) * 100.0

    # No hour-0 reading: use the latest available hourly SOC as a stand-in start.
    for h in range(len(soc_series) - 1, -1, -1):
        if soc_series[h] is not None:
            pct = float(soc_series[h])
            start_kwh = max(0.0, min(battery_cap, (pct / 100.0) * battery_cap))
            return start_kwh, pct

    raise ValueError("No SOC reading available to seed hour-0 battery energy")


def simulate_hour_load_priority(
    soc_kwh: float,
    pv_kwh: float,
    load_kwh: float,
    *,
    battery_cap: float,
    min_kwh: float,
    ac_cap_kwh: float,
    epsilon: float = 0.001,
) -> HourSimResult:
    """One hour: PV serves load first; deficit from battery then grid; surplus to battery then grid."""
    pv_kwh = max(0.0, pv_kwh)
    load_kwh = max(0.0, load_kwh)
    phys = simulate_hour(
        soc_kwh, pv_kwh, load_kwh, HourControl(0.0, 0.0),
        battery_cap=battery_cap,
        min_kwh=min_kwh,
        ac_cap_kw=ac_cap_kwh,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        epsilon=epsilon,
    )
    soc = max(min_kwh, min(battery_cap, phys.soc_end))
    bat_charge = max(0.0, phys.battery_delta)
    bat_discharge = max(0.0, -phys.battery_delta)
    return HourSimResult(
        soc_kwh=soc,
        soc_pct=(soc / battery_cap) * 100.0 if battery_cap > 0 else 0.0,
        grid_import=round(phys.grid_import, 3),
        grid_export=round(phys.grid_export, 3),
        bat_charge=round(bat_charge, 3),
        bat_discharge=round(bat_discharge, 3),
    )


def simulate_day_from_profile(
    hourly: dict[str, list],
    *,
    cfg: dict | None = None,
    initial_soc_pct: float | None = None,
    initial_soc_kwh: float | None = None,
) -> dict[str, Any]:
    """Replay hourly PV and load; return simulated vs actual rows and totals."""
    cfg = merge_simulation_defaults(cfg or load_config())
    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = (float(params["min_soc_pct"]) / 100.0) * battery_cap
    ac_cap_kwh = float(cfg["inverter"]["ac_capacity_kw"])
    epsilon = float(params["epsilon_kwh"])

    start_kwh, start_pct = resolve_hour0_soc_kwh(
        hourly,
        battery_cap,
        override_pct=initial_soc_pct,
        override_kwh=initial_soc_kwh,
    )
    soc = start_kwh

    rows: list[dict[str, Any]] = []
    sum_keys = ("grid_import", "grid_export", "bat_charge", "bat_discharge")
    sim_totals = {k: 0.0 for k in sum_keys}
    act_totals = {k: 0.0 for k in sum_keys}
    sim_soc_samples: list[float] = []
    act_soc_samples: list[float] = []

    for h in range(24):
        pv = _f((hourly.get("pv") or [None] * 24)[h])
        load = _f((hourly.get("load") or [None] * 24)[h])
        act_soc = _f((hourly.get("soc") or [None] * 24)[h])
        act_bc = _f((hourly.get("bat_charge") or [None] * 24)[h])
        act_bd = _f((hourly.get("bat_discharge") or [None] * 24)[h])
        act_gb = _f((hourly.get("grid_buy") or [None] * 24)[h])
        act_gs = _f((hourly.get("grid_sell") or [None] * 24)[h])

        act_grid_import = abs(act_gb) if act_gb is not None and act_gb < 0 else 0.0
        act_grid_export = act_gs if act_gs is not None and act_gs > 0 else 0.0

        row: dict[str, Any] = {
            "hour": h,
            "pv": pv,
            "load": load,
            "skipped": pv is None and load is None,
            "actual": {
                "soc": act_soc,
                "grid_used": round(act_grid_import, 3) if act_gb is not None else None,
                "grid_export": round(act_grid_export, 3) if act_gs is not None else None,
                "bat_charge": round(act_bc, 3) if act_bc is not None else None,
                "bat_discharge": round(act_bd, 3) if act_bd is not None else None,
            },
            "sim": None,
        }

        if pv is None or load is None:
            rows.append(row)
            continue

        result = simulate_hour_load_priority(
            soc, pv, load,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kwh=ac_cap_kwh,
            epsilon=epsilon,
        )
        soc = result.soc_kwh
        sim = {
            "soc": round(result.soc_pct, 1),
            "grid_used": result.grid_import,
            "grid_export": result.grid_export,
            "bat_charge": result.bat_charge,
            "bat_discharge": result.bat_discharge,
        }
        row["sim"] = sim
        rows.append(row)

        for k, v in zip(sum_keys, (
            result.grid_import, result.grid_export,
            result.bat_charge, result.bat_discharge,
        )):
            sim_totals[k] += v
        if act_gb is not None:
            act_totals["grid_import"] += act_grid_import
        if act_gs is not None:
            act_totals["grid_export"] += act_grid_export
        if act_bc is not None:
            act_totals["bat_charge"] += act_bc
        if act_bd is not None:
            act_totals["bat_discharge"] += act_bd
        sim_soc_samples.append(result.soc_pct)
        if act_soc is not None:
            act_soc_samples.append(act_soc)

    def _round_totals(d: dict[str, float]) -> dict[str, float]:
        return {k: round(v, 3) for k, v in d.items()}

    act_totals_out = {
        "grid_used": _round_totals(act_totals)["grid_import"],
        "grid_export": act_totals["grid_export"],
        "bat_charge": act_totals["bat_charge"],
        "bat_discharge": act_totals["bat_discharge"],
        "soc_end": act_soc_samples[-1] if act_soc_samples else None,
    }
    sim_totals_out = {
        "grid_used": sim_totals["grid_import"],
        "grid_export": sim_totals["grid_export"],
        "bat_charge": sim_totals["bat_charge"],
        "bat_discharge": sim_totals["bat_discharge"],
        "soc_end": round((soc / battery_cap) * 100.0, 1) if battery_cap else None,
    }

    return {
        "initial_soc_pct": round(start_pct, 1),
        "initial_soc_kwh": round(start_kwh, 3),
        "end_soc_kwh": round(soc, 3),
        "battery_cap_kwh": battery_cap,
        "min_soc_pct": float(params["min_soc_pct"]),
        "ac_cap_kwh": ac_cap_kwh,
        "rows": rows,
        "totals": {
            "actual": act_totals_out,
            "sim": sim_totals_out,
        },
    }
