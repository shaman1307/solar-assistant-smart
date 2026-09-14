"""
Action labels and timer schedule compression (SolarAssistant timer slots).

Actions:
  - Idle - Grid Usage for Load
  - Idle - PV to Load. On-Grid
  - PV to Load. On-Grid
  - Grid Usage for Load
  - Charging from Grid
  - Charging from PV
  - Discharging to Load
  - Discharging to Grid and Load
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from .simulation_config import (
    get_simulation_params,
    plan_min_soc_pct,
    plan_timer_charge_power_kw,
    plan_timer_discharge_power_kw,
    plan_timer_min_block_minutes,
    plan_timer_min_hourly_transfer_kwh,
)
from .plan_spill import pv_load_energy_split

ACTION_IDLE_GRID = "Idle - Grid Usage for Load"
ACTION_IDLE_PV = "Idle - PV to Load. On-Grid"
ACTION_TIE_PV = "PV to Load. On-Grid"
ACTION_TIE_GRID = "Grid Usage for Load"
ACTION_CHARGE_GRID = "Charging from Grid"
# SRNE timed charge does not start unless stop % is clearly above live SOC.
CHARGE_CAP_MIN_HEADROOM_PCT = 5
ACTION_CHARGE_SOLAR = "Charging from PV"
ACTION_DISCHARGE_LOAD = "Discharging to Load"
ACTION_DISCHARGE_GRID = "Discharging to Grid and Load"
# Minimum hourly grid export (kWh) to label "Discharging to Grid and Load" vs "Discharging to Load".
GRID_EXPORT_ACTION_MIN_KWH = 0.5

# SOC at or above this (%) allows "PV export to grid" (full battery spill).
_PV_EXPORT_FULL_SOC_PCT = 99.9

ACTION_CYCLE = [
    ACTION_DISCHARGE_LOAD,
    ACTION_IDLE_GRID,
    ACTION_CHARGE_SOLAR,
    ACTION_CHARGE_GRID,
    ACTION_DISCHARGE_GRID,
]

_LEGACY_ACTION_MAP = {
    "From grid": ACTION_IDLE_GRID,
    "Charge from grid": ACTION_CHARGE_GRID,
    "Charge from solar": ACTION_CHARGE_SOLAR,
    "Discharge to grid": ACTION_DISCHARGE_GRID,
    "Discharge to load": ACTION_DISCHARGE_LOAD,
    "Discharging to Grid and Installation": ACTION_DISCHARGE_GRID,
    "PV export": ACTION_IDLE_PV,
    "PV export to grid": ACTION_IDLE_PV,
}


def normalize_action(action: str) -> str:
    return _LEGACY_ACTION_MAP.get(action, action)


def _pv_export_to_grid(
    *,
    pv: float | None,
    grid_export: float,
    soc_pct: float | None,
    epsilon: float = 0.001,
) -> bool:
    """PV spill to grid: production this hour and battery full (SOC ~100%)."""
    pv_val = float(pv) if pv is not None else 0.0
    if pv_val <= epsilon or grid_export <= epsilon:
        return False
    if soc_pct is None:
        return False
    return float(soc_pct) >= _PV_EXPORT_FULL_SOC_PCT


def classify_action(
    *,
    bat_charge: float = 0.0,
    bat_discharge: float = 0.0,
    grid_import: float = 0.0,
    grid_export: float = 0.0,
    production: float | None = None,
    pv: float | None = None,
    epsilon: float = 0.001,
) -> str:
    """Derive display action from gross battery charge/discharge and grid/PV context."""
    eps = max(0.0, float(epsilon))
    charge = max(0.0, float(bat_charge or 0))
    discharge = max(0.0, float(bat_discharge or 0))
    if charge <= eps:
        charge = 0.0
    if discharge <= eps:
        discharge = 0.0
    g_imp = max(0.0, float(grid_import or 0))
    g_exp = max(0.0, float(grid_export or 0))
    prod = float(production if production is not None else pv or 0)

    if charge > discharge:
        return ACTION_CHARGE_GRID if g_imp > prod else ACTION_CHARGE_SOLAR
    if discharge > charge:
        return (
            ACTION_DISCHARGE_GRID
            if g_exp > GRID_EXPORT_ACTION_MIN_KWH
            else ACTION_DISCHARGE_LOAD
        )
    if charge == 0 and discharge == 0:
        return ACTION_IDLE_PV if prod > 0 else ACTION_IDLE_GRID
    # Tie: equal gross charge and discharge in the same hour
    return ACTION_TIE_PV if prod > 0 else ACTION_TIE_GRID


def _slot_action_energy(slot: dict[str, Any], action: str) -> float:
    """kWh attributed to a 15-min slot action."""
    imp = float(slot.get("grid_import") or 0)
    exp = float(slot.get("grid_export") or 0)
    bd = float(slot.get("battery_delta") or 0)
    if action == ACTION_DISCHARGE_GRID:
        return max(min(exp, max(0.0, -bd)), 0.0)
    if action == ACTION_DISCHARGE_LOAD:
        return max(0.0, -bd)
    if action == ACTION_CHARGE_GRID:
        bd_pos = max(0.0, bd)
        return bd_pos if bd_pos > 0.0 else 0.0
    if action == ACTION_CHARGE_SOLAR:
        return max(0.0, bd)
    if action == ACTION_IDLE_GRID:
        return imp
    return 0.0


def _battery_discharge_split(
    slot: dict[str, Any],
    epsilon: float,
) -> tuple[float, float]:
    """Split q15 battery discharge into load vs grid export (kWh)."""
    bd = float(slot.get("battery_delta") or 0)
    if bd >= -epsilon:
        return 0.0, 0.0
    dis = abs(bd)
    batt_exp_raw = slot.get("battery_export_kwh")
    if batt_exp_raw is not None:
        batt_exp = max(0.0, min(float(batt_exp_raw), dis))
    else:
        exp = float(slot.get("grid_export") or 0)
        batt_exp = min(exp, dis) if exp > epsilon else 0.0
    to_load = max(0.0, dis - batt_exp)
    return to_load, batt_exp


def _hour_action_energies(
    slots: list[dict[str, Any]],
    epsilon: float,
) -> dict[str, float]:
    """Dominant action energies aligned with debug Smart flow columns (kWh)."""
    energy: dict[str, float] = defaultdict(float)
    grid_used = 0.0
    grid_export = 0.0
    bat_charge = 0.0
    bat_discharge = 0.0
    batt_export = 0.0

    for slot in slots:
        grid_used += float(slot.get("grid_import") or 0)
        grid_export += float(slot.get("grid_export") or 0)
        bd = float(slot.get("battery_delta") or 0)
        if bd > 0:
            bat_charge += bd
        elif bd < 0:
            bat_discharge += abs(bd)
        _, to_grid = _battery_discharge_split(slot, epsilon)
        batt_export += to_grid

    if batt_export > epsilon:
        energy[ACTION_DISCHARGE_GRID] = max(grid_export, batt_export)
        to_load = max(0.0, bat_discharge - batt_export)
        if to_load > epsilon:
            energy[ACTION_DISCHARGE_LOAD] = to_load
    else:
        if grid_export > epsilon:
            energy[ACTION_DISCHARGE_GRID] = grid_export
        to_load = max(0.0, bat_discharge - grid_export)
        if to_load > epsilon:
            energy[ACTION_DISCHARGE_LOAD] = to_load

    if bat_charge > epsilon:
        if grid_used > epsilon:
            energy[ACTION_CHARGE_GRID] = bat_charge + grid_used
        else:
            energy[ACTION_CHARGE_SOLAR] = bat_charge
    elif grid_used > epsilon:
        energy[ACTION_IDLE_GRID] = grid_used

    return energy


def _battery_export_energy(slots: list[dict[str, Any]], epsilon: float) -> float:
    return sum(_battery_discharge_split(slot, epsilon)[1] for slot in slots)


def _top_hour_action(energy: dict[str, float], epsilon: float) -> str:
    ranked = sorted(
        ((act, kwh) for act, kwh in energy.items() if kwh > epsilon),
        key=lambda item: item[1],
        reverse=True,
    )
    if not ranked:
        if energy.get(ACTION_CHARGE_SOLAR, 0) > 0:
            return ACTION_CHARGE_SOLAR
        return ACTION_IDLE_GRID
    return ranked[0][0]


def _hour_export_debug_action(
    hour: int,
    slots: list[dict[str, Any]],
    cfg: dict,
    *,
    epsilon: float = 0.001,
) -> str | None:
    """Hour export label from q15 slots (tests/debug).

    Prefer ``summarize_hour_actions_debug`` / ``classify_action`` at call sites.
    """
    del hour, cfg
    pv_total = sum(float(s.get("pv") or 0) for s in slots)
    grid_export = sum(float(s.get("grid_export") or 0) for s in slots)
    if pv_total > epsilon and grid_export > pv_total + epsilon:
        batt_exp = _battery_export_energy(slots, epsilon)
        bat_discharge = sum(
            max(0.0, -float(s.get("battery_delta") or 0)) for s in slots
        )
        soc_pct = float(slots[-1]["soc_pct"]) if slots and slots[-1].get("soc_pct") is not None else None
        if batt_exp > epsilon and batt_exp >= bat_discharge - epsilon:
            return ACTION_DISCHARGE_GRID
        if _pv_export_to_grid(
            pv=pv_total, grid_export=grid_export, soc_pct=soc_pct, epsilon=epsilon,
        ):
            return ACTION_IDLE_PV
        return ACTION_DISCHARGE_GRID
    batt_exp = _battery_export_energy(slots, epsilon)
    if batt_exp > epsilon:
        return ACTION_DISCHARGE_GRID
    if grid_export > epsilon and pv_total <= epsilon:
        return ACTION_DISCHARGE_GRID
    return None


def summarize_hour_actions(
    slots: list[dict[str, Any]],
    *,
    epsilon: float = 0.001,
) -> str:
    """Dominant hour action by summed q15 energy (single label)."""
    return _top_hour_action(_hour_action_energies(slots, epsilon), epsilon)


def summarize_hour_actions_debug(
    slots: list[dict[str, Any]],
    hour: int,
    cfg: dict,
    *,
    epsilon: float = 0.001,
) -> str:
    """Debug/PROD hourly action — same classify rules as Influx history rows."""
    del hour, cfg
    pv_total = sum(float(s.get("pv") or 0) for s in slots)
    grid_import = sum(float(s.get("grid_import") or 0) for s in slots)
    grid_export = sum(float(s.get("grid_export") or 0) for s in slots)
    bat_charge = sum(max(0.0, float(s.get("battery_delta") or 0)) for s in slots)
    bat_discharge = sum(max(0.0, -float(s.get("battery_delta") or 0)) for s in slots)
    return classify_action(
        bat_charge=bat_charge,
        bat_discharge=bat_discharge,
        grid_import=grid_import,
        grid_export=grid_export,
        production=pv_total,
        epsilon=epsilon,
    )


def _hour_q15_timer_energy(
    slots: list[dict[str, Any]],
    target_action: str,
    epsilon: float,
    *,
    hour_action: str | None = None,
) -> tuple[float, list[int]]:
    """Total kWh and active q15 indices (0..3) in the hour for one timer kind."""
    total = 0.0
    active: list[int] = []
    hour_act = normalize_action(hour_action or "")
    for qi, slot in enumerate(slots):
        if target_action == ACTION_DISCHARGE_GRID:
            _, to_grid = _battery_discharge_split(slot, epsilon)
            if to_grid <= epsilon:
                continue
            total += to_grid
        elif target_action == ACTION_CHARGE_GRID:
            act = normalize_action(slot.get("action") or "")
            imp = float(slot.get("grid_import") or 0)
            if act == ACTION_CHARGE_GRID:
                total += _slot_action_energy(slot, ACTION_CHARGE_GRID)
                active.append(qi)
            elif hour_act == ACTION_CHARGE_GRID:
                bd_pos = max(0.0, float(slot.get("battery_delta") or 0))
                spv = float(slot.get("pv") or 0)
                if bd_pos <= epsilon or imp <= epsilon or imp <= spv:
                    continue
                total += _slot_action_energy(slot, ACTION_CHARGE_GRID)
                active.append(qi)
        else:
            continue
        if target_action == ACTION_DISCHARGE_GRID:
            active.append(qi)
    return total, active


def _round_pct_half_up(value: float) -> int:
    """School / mathematical percent rounding: .5 and above rounds away from zero."""
    x = float(value)
    if x >= 0:
        return int(math.floor(x + 0.5))
    return int(math.ceil(x - 0.5))


def _charge_timer_cap_pct(
    slots: list[dict[str, Any]],
    cfg: dict,
    *,
    live_soc_pct: float | None = None,
) -> int:
    """SA charge stop %: end of this hour's charge window, at least live SOC+5.

    Not hour-end SOC after post-charge discharge, and not overnight reserve.
    Example: Chg 01:00-01:30 peaks at 24.5%, then load drains to 23.6% by 02:00 —
    cap must be 25% (round 24.5), else SA would stop at 24% and end ~23.1%.
    SRNE does not start timed charge when stop % is only 1–2 points above live SOC.
    """
    min_soc = int(plan_min_soc_pct(cfg))
    cap = min_soc
    eps = 0.001
    last_charge_soc: float | None = None
    start_soc: float | None = None
    if live_soc_pct is not None:
        try:
            start_soc = float(live_soc_pct)
        except (TypeError, ValueError):
            start_soc = None

    bat_cap = 0.0
    try:
        bat_cap = float((cfg.get("battery") or {}).get("capacity_kwh") or 0)
    except (TypeError, ValueError):
        bat_cap = 0.0

    for s in slots:
        charged = _slot_bat_charge_kwh(s)
        grid_chg = float(s.get("grid_import") or 0)
        is_chg = charged > eps or grid_chg > eps
        if not is_chg:
            act = str(s.get("action") or "")
            is_chg = "Charging from Grid" in act or act == ACTION_CHARGE_GRID
        if not is_chg:
            continue
        soc = s.get("soc_pct")
        if soc is None:
            continue
        try:
            soc_f = float(soc)
        except (TypeError, ValueError):
            continue
        last_charge_soc = soc_f
        if start_soc is None:
            start_soc = soc_f
            if bat_cap > 0:
                start_soc -= 100.0 * charged / bat_cap

    if last_charge_soc is not None:
        cap = min(100, max(min_soc, _round_pct_half_up(last_charge_soc)))
    else:
        last_soc: float | None = None
        for s in slots:
            soc = s.get("soc_pct")
            if soc is None:
                continue
            try:
                last_soc = float(soc)
            except (TypeError, ValueError):
                continue
        if last_soc is not None:
            cap = min(100, max(min_soc, _round_pct_half_up(last_soc)))
            if start_soc is None:
                start_soc = last_soc

    if start_soc is not None:
        cap = max(cap, min(100, int(start_soc) + CHARGE_CAP_MIN_HEADROOM_PCT))
    return min(100, max(min_soc, cap))


def _slot_bat_charge_kwh(slot: dict[str, Any]) -> float:
    """Battery charge kWh in a q15 slot or EA row."""
    if slot.get("battery_delta") is not None:
        return max(0.0, float(slot.get("battery_delta") or 0))
    if slot.get("bat_charge") is not None:
        return max(0.0, float(slot.get("bat_charge") or 0))
    bat = slot.get("battery")
    if bat is not None:
        return max(0.0, float(bat))
    return 0.0


def _slot_bat_discharge_kwh(slot: dict[str, Any]) -> float:
    """Battery discharge kWh in a q15 slot or EA row (smart Bat Discharge)."""
    if slot.get("battery_delta") is not None:
        return max(0.0, -float(slot.get("battery_delta") or 0))
    if slot.get("bat_discharge") is not None:
        return max(0.0, float(slot.get("bat_discharge") or 0))
    bat = slot.get("battery")
    if bat is not None:
        return max(0.0, -float(bat))
    return 0.0


def _hour_timer_segment(
    hour: int,
    slots: list[dict[str, Any]],
    target_action: str,
    cfg: dict,
    *,
    epsilon: float = 0.001,
    hour_action: str | None = None,
    not_before_min: int | None = None,
) -> str | None:
    """One SA timer line for this clock hour from q15 energy (>= min_block).

    Power is timer DC (battery) rating — inverter applies losses on AC side.
    *not_before_min* (minute-of-day): never start before this (no retroactive slots).
    """
    total_kwh, active_q = _hour_q15_timer_energy(
        slots, target_action, epsilon, hour_action=hour_action,
    )
    if not active_q or total_kwh <= epsilon:
        return None

    min_block = plan_timer_min_block_minutes(cfg)
    min_soc = int(plan_min_soc_pct(cfg))
    hour_start = hour * 60
    hour_end = hour_start + 60
    floor_min = hour_start if not_before_min is None else max(hour_start, int(not_before_min))

    # Drop quarters that already ended before the floor (past wall-clock).
    active_q = [
        qi for qi in active_q
        if hour_start + (qi + 1) * 15 > floor_min
    ]
    if not active_q:
        return None

    first_q = min(active_q)
    last_q = max(active_q)
    natural_from = hour_start + first_q * 15
    to_min = hour_start + (last_q + 1) * 15
    from_min = max(natural_from, floor_min)
    # Extend toward min_block only into the *future* within this hour — never into the past.
    # Pad a short leftover export window forward to min_block within the hour
    # (lower kW over a longer span).
    if to_min - from_min < min_block:
        to_min = min(hour_end, from_min + min_block)
    if to_min - from_min < min_block:
        return None

    duration_min = to_min - from_min
    if target_action == ACTION_CHARGE_GRID:
        power_kw = infer_charge_timer_power_kw(total_kwh, duration_min, cfg)
    else:
        power_kw = infer_discharge_timer_power_kw(
            total_kwh,
            duration_min,
            cfg,
            load_kwh=sum(
                _slot_load_kwh(slots[qi]) for qi in active_q if 0 <= qi < len(slots)
            ),
            pv_kwh=sum(
                _slot_pv_kwh(slots[qi]) for qi in active_q if 0 <= qi < len(slots)
            ),
        )
    if power_kw <= 0:
        return None

    prefix = "Chg" if target_action == ACTION_CHARGE_GRID else "Dis"
    if target_action == ACTION_CHARGE_GRID:
        cap = _charge_timer_cap_pct(slots, cfg)
    else:
        # Survive-until-morning floor from the last active Dis quarter.
        last = slots[last_q] if 0 <= last_q < len(slots) else {}
        slot_for_cap = dict(last)
        if slot_for_cap.get("reserve_soc_pct") is None and last.get("reserve_kwh") is not None:
            try:
                bat_cap = float(cfg["battery"]["capacity_kwh"])
                if bat_cap > 0:
                    slot_for_cap["reserve_soc_pct"] = (
                        100.0 * float(last["reserve_kwh"]) / bat_cap
                    )
            except (TypeError, ValueError, KeyError):
                pass
        cap = max(min_soc, discharge_cap_pct_from_row(slot_for_cap, min_soc))
    return f"{prefix} {_min_to_hhmm(from_min)}-{_min_to_hhmm(to_min)} {power_kw}kW cap{cap}%"


def _hour_slot_totals(slots: list[dict[str, Any]]) -> dict[str, float]:
    """Hourly sums from q15 slots for action / timer display."""
    return {
        "bat_charge": sum(_slot_bat_charge_kwh(s) for s in slots),
        "bat_discharge": sum(_slot_bat_discharge_kwh(s) for s in slots),
        "grid_import": sum(float(s.get("grid_import") or 0) for s in slots),
        "grid_export": sum(float(s.get("grid_export") or 0) for s in slots),
        "production": sum(float(s.get("pv") or 0) for s in slots),
    }


def _fallback_charge_grid_timer(
    hour: int,
    slots: list[dict[str, Any]],
    cfg: dict,
    *,
    epsilon: float = 0.001,
) -> str:
    """Full clock-hour charge slot when q15 clip cannot reach SA minimum duration."""
    totals = _hour_slot_totals(slots)
    if totals["bat_charge"] <= epsilon:
        return ""
    hour_start = hour * 60
    power_kw = infer_charge_timer_power_kw(totals["bat_charge"], 60, cfg)
    cap = _charge_timer_cap_pct(slots, cfg) if slots else 80
    return (
        f"Chg {_min_to_hhmm(hour_start)}-{_min_to_hhmm(hour_start + 60)} "
        f"{power_kw}kW cap{cap}%"
    )


def build_hour_timer_schedule(
    hour: int,
    slots: list[dict[str, Any]],
    cfg: dict,
    *,
    action: str | None = None,
    grid_export: float | None = None,
    bat_charge: float | None = None,
    epsilon: float = 0.001,
    not_before_min: int | None = None,
) -> str:
    """Per-hour SA timer — only when *action* (row label) warrants grid charge/discharge.

    *not_before_min*: do not emit segments that start in the past (minute-of-day).
    """
    totals = _hour_slot_totals(slots)
    act = normalize_action(
        action if action is not None else classify_action(**totals, epsilon=epsilon)
    )
    g_exp = float(grid_export if grid_export is not None else totals["grid_export"])
    if act == ACTION_CHARGE_GRID:
        bc = float(bat_charge if bat_charge is not None else totals["bat_charge"])
        if bc <= epsilon:
            return ""
        seg = _hour_timer_segment(
            hour, slots, ACTION_CHARGE_GRID, cfg,
            epsilon=epsilon, hour_action=act, not_before_min=not_before_min,
        )
        return seg or _fallback_charge_grid_timer(hour, slots, cfg, epsilon=epsilon)
    if act == ACTION_DISCHARGE_GRID and g_exp > 0:
        bd = totals["bat_discharge"]
        if bd <= epsilon:
            return ""
        seg = _hour_timer_segment(
            hour, slots, ACTION_DISCHARGE_GRID, cfg,
            epsilon=epsilon, hour_action=act, not_before_min=not_before_min,
        )
        return seg or ""
    return ""


def _hour_from_row(row: dict) -> int:
    return int(row["start"].split(" ")[1].split(":")[0])


def _inactive_slot(slot_n: int, kind: str, template: dict | None) -> dict[str, Any]:
    base = template or {}
    return {
        "slot": slot_n,
        "from": "00:00",
        "to": "00:00",
        "capacity_pct": base.get("capacity_pct", 15 if kind == "discharge" else 80),
        "voltage_v": base.get("voltage_v", 58.0 if kind == "charge" else 42.0),
        "power_kw": base.get("power_kw", 0.0),
        **({"grid": base.get("grid", True), "generator": base.get("generator", False)} if kind == "charge" else {}),
    }


def _merge_blocks(rows: list[dict], target_action: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        action = normalize_action(row.get("action", ""))
        if action != target_action:
            if current:
                blocks.append(current)
                current = None
            continue

        hour = _hour_from_row(row)
        if target_action == ACTION_CHARGE_GRID:
            power_kw = max(
                float(row.get("battery", 0) or 0) + float(row.get("grid_import", row.get("buy", 0)) or 0),
                1.0,
            )
        else:
            power_kw = max(float(row.get("grid_export", row.get("feed_in", 0)) or 0), 1.0)

        if current and hour == current["_last_hour"] + 1:
            current["to_hour"] = hour + 1
            current["_last_hour"] = hour
            current["power_kw"] = max(current["power_kw"], power_kw)
            current["capacity_pct"] = round(float(row.get("soc", current["capacity_pct"])), 0)
        else:
            if current:
                blocks.append(current)
            current = {
                "from_hour": hour,
                "to_hour": hour + 1,
                "_last_hour": hour,
                "power_kw": power_kw,
                "capacity_pct": round(float(row.get("soc", 80)), 0),
            }

    if current:
        blocks.append(current)
    return blocks


def _minute_of_day_from_start(row: dict) -> int:
    parts = row["start"].split(" ")[1].split(":")
    return int(parts[0]) * 60 + int(parts[1])


def discharge_cap_pct_from_row(row: dict[str, Any], fallback: float) -> int:
    """SA Dis stop %: overnight survive reserve at this slot, else planned SOC.

    Ceil fractional reserve so the inverter never stops below the modeled floor
    (e.g. 31.2% → cap32%).
    """
    raw = row.get("reserve_soc_pct")
    if raw is None and row.get("reserve_kwh") is not None:
        # Caller without battery_cap — keep prior block value.
        raw = fallback
    if raw is not None:
        try:
            return max(0, int(math.ceil(float(raw) - 1e-9)))
        except (TypeError, ValueError):
            pass
    try:
        return max(0, int(math.ceil(float(row.get("soc", fallback)) - 1e-9)))
    except (TypeError, ValueError):
        return max(0, int(math.ceil(float(fallback) - 1e-9)))


def merge_blocks_q15(rows: list[dict], target_action: str) -> list[dict[str, Any]]:
    """Merge consecutive 15-minute rows into timer blocks (HH:MM boundaries)."""
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        action = normalize_action(row.get("action", ""))
        if action != target_action:
            if current:
                blocks.append(current)
                current = None
            continue

        slot_min = _minute_of_day_from_start(row)
        if target_action == ACTION_CHARGE_GRID:
            import_kwh = float(row.get("grid_import", row.get("buy", 0)) or 0)
            power_kw = max(
                float(row.get("battery", 0) or 0) + import_kwh,
                1.0,
            )
        else:
            export_kwh = float(row.get("grid_export", row.get("feed_in", 0)) or 0)
            load_kwh = float(row.get("consumption") or row.get("load") or 0)
            pv_kwh = float(row.get("production") or row.get("pv") or 0)
            power_kw = max(export_kwh * 4.0, 1.0)

        if current and slot_min == current["_last_end_min"]:
            current["to_min"] = slot_min + 15
            current["_last_end_min"] = slot_min + 15
            current["power_kw"] = max(current["power_kw"], power_kw)
            if target_action == ACTION_DISCHARGE_GRID:
                current["export_kwh"] = float(current.get("export_kwh", 0.0)) + export_kwh
                current["load_kwh"] = float(current.get("load_kwh", 0.0)) + load_kwh
                current["pv_kwh"] = float(current.get("pv_kwh", 0.0)) + pv_kwh
                current["capacity_pct"] = discharge_cap_pct_from_row(row, current["capacity_pct"])
            else:
                current["capacity_pct"] = round(float(row.get("soc", current["capacity_pct"])), 0)
        else:
            if current:
                blocks.append(current)
            if target_action == ACTION_DISCHARGE_GRID:
                cap = discharge_cap_pct_from_row(row, 80)
            else:
                cap = round(float(row.get("soc", 80)), 0)
            current = {
                "from_min": slot_min,
                "to_min": slot_min + 15,
                "_last_end_min": slot_min + 15,
                "power_kw": power_kw,
                "capacity_pct": cap,
            }
            if target_action == ACTION_DISCHARGE_GRID:
                current["export_kwh"] = export_kwh
                current["load_kwh"] = load_kwh
                current["pv_kwh"] = pv_kwh

    if current:
        blocks.append(current)
    return blocks


def _slot_load_kwh(slot: dict[str, Any]) -> float:
    """House load kWh in a q15 slot (optimizer or EA row shape)."""
    for key in ("load", "consumption"):
        if slot.get(key) is not None:
            return float(slot.get(key) or 0)
    return 0.0


def _slot_pv_kwh(slot: dict[str, Any]) -> float:
    """PV production kWh in a q15 slot (optimizer or EA row shape)."""
    for key in ("pv", "production"):
        if slot.get(key) is not None:
            return float(slot.get(key) or 0)
    return 0.0


def infer_charge_timer_power_kw(
    charge_kwh: float,
    duration_min: int,
    cfg: dict,
) -> float:
    """SA timer DC kW from planned battery charge over duration.

    Floors at ``min_hourly_transfer_kwh / duration`` so a Chg block still meets
    the configured minimum battery transfer (e.g. 2 kWh / 0.5 h → 4 kW).
    Caps at ``plan_timer_charge_power_kw``. Steps to 0.5 kW like discharge.
    """
    max_kw = plan_timer_charge_power_kw(cfg)
    min_block = plan_timer_min_block_minutes(cfg)
    if charge_kwh <= 0.001 or duration_min < min_block:
        return max_kw
    duration_h = duration_min / 60.0
    needed_dc = charge_kwh / duration_h
    min_hourly = plan_timer_min_hourly_transfer_kwh(cfg)
    if min_hourly > 0.001:
        needed_dc = max(needed_dc, min_hourly / duration_h)
    if needed_dc <= 0.001:
        return max_kw
    stepped = min(max_kw, math.ceil(needed_dc * 2.0) / 2.0)
    return round(max(stepped, 0.5), 1)


def infer_discharge_timer_power_kw(
    export_kwh: float,
    duration_min: int,
    cfg: dict,
    *,
    load_kwh: float = 0.0,
    pv_kwh: float = 0.0,
) -> float:
    """SA timer DC kW: export + load deficit after PV, over duration."""
    max_kw = plan_timer_discharge_power_kw(cfg)
    min_block = plan_timer_min_block_minutes(cfg)
    if export_kwh <= 0.001 or duration_min < min_block:
        return max_kw
    params = get_simulation_params(cfg)
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    duration_h = duration_min / 60.0
    load_deficit, _ = pv_load_energy_split(
        max(0.0, pv_kwh), max(0.0, load_kwh), eta_pv_load=eta_pv_load,
    )
    ac_kwh = export_kwh + load_deficit
    needed_dc = ac_kwh / (duration_h * eta_out) if eta_out > 0 else ac_kwh / duration_h
    if needed_dc <= 0.001:
        return max_kw
    stepped = min(max_kw, math.ceil(needed_dc * 2.0) / 2.0)
    return round(max(stepped, 0.5), 1)


def _filter_min_duration_blocks(
    blocks: list[dict[str, Any]],
    min_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Drop SA timer blocks shorter than min_minutes (after q15 merge)."""
    if min_minutes is None:
        min_minutes = 30
    return [b for b in blocks if (b["to_min"] - b["from_min"]) >= min_minutes]


def _min_to_hhmm(total_min: int) -> str:
    total_min %= 24 * 60
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def blocks_q15_to_slots(
    blocks: list[dict[str, Any]],
    kind: str,
    templates: list[dict[str, Any]],
    cfg: dict,
) -> list[dict[str, Any]]:
    min_soc = plan_min_soc_pct(cfg)
    charge_kw = plan_timer_charge_power_kw(cfg)
    discharge_kw = plan_timer_discharge_power_kw(cfg)
    slots: list[dict[str, Any]] = []

    for i in range(3):
        tpl = templates[i] if i < len(templates) else {}
        if i >= len(blocks):
            slots.append(_inactive_slot(i + 1, kind, tpl))
            continue

        blk = blocks[i]
        if kind == "charge":
            timer_kw = charge_kw
        else:
            dur = max(1, int(blk["to_min"] - blk["from_min"]))
            timer_kw = infer_discharge_timer_power_kw(
                float(blk.get("export_kwh") or 0.0),
                dur,
                cfg,
                load_kwh=float(blk.get("load_kwh") or 0.0),
                pv_kwh=float(blk.get("pv_kwh") or 0.0),
            )
        slot: dict[str, Any] = {
            "slot": i + 1,
            "from": _min_to_hhmm(blk["from_min"]),
            "to": _min_to_hhmm(blk["to_min"]),
            "capacity_pct": (
                int(blk["capacity_pct"])
                if kind == "charge"
                else max(int(min_soc), int(round(float(blk.get("capacity_pct") or min_soc))))
            ),
            "voltage_v": float(tpl.get("voltage_v", 58.0 if kind == "charge" else 42.0)),
            "power_kw": timer_kw,
        }
        if kind == "charge":
            slot["grid"] = True
            slot["generator"] = False
        slots.append(slot)

    return slots


def _extend_blocks_to_min_duration(
    blocks: list[dict[str, Any]],
    rows: list[dict],
    target_action: str,
    min_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Extend short blocks across following q15 slots with the same action."""
    if min_minutes is None:
        min_minutes = 30
    actions_by_min: dict[int, str] = {}
    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        actions_by_min[_minute_of_day_from_start(row)] = normalize_action(row.get("action", ""))

    extended: list[dict[str, Any]] = []
    for blk in blocks:
        dur = blk["to_min"] - blk["from_min"]
        if dur >= min_minutes:
            extended.append(blk)
            continue
        end = blk["to_min"]
        cursor = blk["to_min"]
        while end - blk["from_min"] < min_minutes and cursor < 24 * 60:
            act = actions_by_min.get(cursor)
            if act is not None and act != target_action:
                break
            end = cursor + 15
            cursor = end
        if end - blk["from_min"] >= min_minutes:
            nb = dict(blk)
            nb["to_min"] = end
            nb["_last_end_min"] = end
            extended.append(nb)
    return extended


def _remerge_overlapping_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge blocks that touch after extension (from_min sorted)."""
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: b["from_min"])
    merged: list[dict[str, Any]] = [dict(ordered[0])]
    for blk in ordered[1:]:
        cur = merged[-1]
        if blk["from_min"] <= cur["to_min"]:
            cur["to_min"] = max(cur["to_min"], blk["to_min"])
            cur["_last_end_min"] = cur["to_min"]
            cur["power_kw"] = max(cur["power_kw"], blk["power_kw"])
            cur["export_kwh"] = float(cur.get("export_kwh", 0.0)) + float(blk.get("export_kwh", 0.0))
            cur["load_kwh"] = float(cur.get("load_kwh", 0.0)) + float(blk.get("load_kwh", 0.0))
            cur["pv_kwh"] = float(cur.get("pv_kwh", 0.0)) + float(blk.get("pv_kwh", 0.0))
            cur["capacity_pct"] = blk["capacity_pct"]
        else:
            merged.append(dict(blk))
    return merged


def derive_timer_schedule_q15(
    rows: list[dict],
    cfg: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    min_block = plan_timer_min_block_minutes(cfg)
    charge_merged = merge_blocks_q15(rows, ACTION_CHARGE_GRID)
    discharge_merged = merge_blocks_q15(rows, ACTION_DISCHARGE_GRID)
    charge_blocks = _filter_min_duration_blocks(
        _remerge_overlapping_blocks(
            _extend_blocks_to_min_duration(
                charge_merged, rows, ACTION_CHARGE_GRID, min_minutes=min_block,
            ),
        ),
        min_minutes=min_block,
    )
    discharge_blocks = _filter_min_duration_blocks(
        _remerge_overlapping_blocks(
            _extend_blocks_to_min_duration(
                discharge_merged, rows, ACTION_DISCHARGE_GRID, min_minutes=min_block,
            ),
        ),
        min_minutes=min_block,
    )

    return {
        "timed_charge_enabled": bool(charge_blocks),
        "timed_discharge_enabled": bool(discharge_blocks),
        "charge_slots": blocks_q15_to_slots(charge_blocks, "charge", existing.get("charge_slots", []), cfg),
        "discharge_slots": blocks_q15_to_slots(
            discharge_blocks, "discharge", existing.get("discharge_slots", []), cfg
        ),
    }


def _parse_hhmm_to_min(time_str: str) -> int | None:
    try:
        parts = str(time_str).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def _slot_is_active(slot: dict[str, Any]) -> bool:
    if slot.get("from") == "00:00" and slot.get("to") == "00:00" and float(slot.get("power_kw") or 0) <= 0:
        return False
    from_min = _parse_hhmm_to_min(slot.get("from", "00:00"))
    to_min = _parse_hhmm_to_min(slot.get("to", "00:00"))
    return from_min is not None and to_min is not None and to_min > from_min


def _hour_slot_clip(
    slot: dict[str, Any],
    hour: int,
) -> tuple[str, str, int] | None:
    """Return (from, to, duration_min) of slot clipped to [hour:00, hour+1:00), or None."""
    from_min = _parse_hhmm_to_min(slot.get("from", "00:00"))
    to_min = _parse_hhmm_to_min(slot.get("to", "00:00"))
    if from_min is None or to_min is None or to_min <= from_min:
        return None
    hour_start = hour * 60
    hour_end = (hour + 1) * 60
    clip_from = max(from_min, hour_start)
    clip_to = min(to_min, hour_end)
    if clip_from >= clip_to:
        return None
    return _min_to_hhmm(clip_from), _min_to_hhmm(clip_to), clip_to - clip_from


def hour_has_timer_schedule(rows: list[dict], hour: int) -> bool:
    """True when Energy arbitrage row for *hour* has a non-empty Timer Schedule cell."""
    row = next(
        (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
        None,
    )
    if not row:
        return False
    return bool(str(row.get("timer_schedule") or "").strip())


_TIMER_SCHEDULE_SEG_RE = re.compile(
    r"^(Chg|Dis)\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s+([\d.]+)kW\s+cap(\d+)%",
    re.IGNORECASE,
)


def _normalize_hhmm(time_str: str) -> str:
    parts = str(time_str).strip().split(":")
    if len(parts) != 2:
        return time_str
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def parse_timer_schedule_segments(text: str) -> list[dict[str, Any]]:
    """Parse Energy arbitrage Timer Schedule cell — e.g. Dis 19:30-20:00 6.51kW cap16%."""
    segments: list[dict[str, Any]] = []
    for part in (p.strip() for p in text.split("|")):
        if not part:
            continue
        match = _TIMER_SCHEDULE_SEG_RE.match(part)
        if not match:
            continue
        kind_raw, from_t, to_t, power_s, cap_s = match.groups()
        kind = kind_raw.lower()
        segments.append({
            "kind": kind,
            "from": _normalize_hhmm(from_t),
            "to": _normalize_hhmm(to_t),
            "power_kw": float(power_s),
            "capacity_pct": int(cap_s),
        })
    return segments


def quarter_start_minute(now: datetime) -> int:
    """Start of the current 15-min slot (minute of day)."""
    return now.hour * 60 + (now.minute // 15) * 15


def clip_timer_schedule_not_before(
    timer_txt: str,
    earliest_from_min: int,
    *,
    cfg: dict | None = None,
) -> str:
    """Trim timer segments so none start before *earliest_from_min* (no retroactive slots).

    Fully past segments (end <= earliest) are dropped. A still-running segment is
    truncated at *earliest_from_min*; remaining duration may be shorter than
    min_block so an in-progress discharge stays visible mid-window.
    Unparseable text is left unchanged.
    """
    del cfg  # reserved for callers; min_block does not apply to clip remainders
    raw = str(timer_txt or "").strip()
    if not raw:
        return ""
    segments = parse_timer_schedule_segments(raw)
    if not segments:
        return raw
    parts: list[str] = []
    for seg in segments:
        from_min = _hhmm_to_minute_of_day(seg["from"])
        to_min = _hhmm_to_minute_of_day(seg["to"])
        if from_min is None or to_min is None:
            continue
        if to_min <= earliest_from_min:
            continue
        clip_from = max(from_min, earliest_from_min)
        if clip_from >= to_min:
            continue
        prefix = "Chg" if seg["kind"] == "chg" else "Dis"
        parts.append(
            f"{prefix} {_min_to_hhmm(clip_from)}-{seg['to']} "
            f"{seg['power_kw']:g}kW cap{seg['capacity_pct']}%"
        )
    return " | ".join(parts)


def _hhmm_to_minute_of_day(hhmm: str) -> int | None:
    parts = str(hhmm).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def timer_discharge_end_times_hhmm(timer_txt: str) -> list[str]:
    """All discharge segment end times (HH:MM) from a Timer Schedule cell."""
    ends: list[str] = []
    for seg in parse_timer_schedule_segments(timer_txt):
        if seg.get("kind") != "dis":
            continue
        to_min = _hhmm_to_minute_of_day(seg["to"])
        if to_min is None:
            continue
        ends.append(f"{to_min // 60:02d}:{to_min % 60:02d}")
    return ends


def timer_discharge_early_end_hhmm(timer_txt: str, hour: int) -> str | None:
    """HH:MM when discharge ends before (hour+1):00, else None.

    Example: hour 22 + ``Dis 22:00-22:45 …`` → ``22:45``; ``Dis 22:00-23:00`` → None.
    """
    segments = parse_timer_schedule_segments(timer_txt)
    discharge = [s for s in segments if s.get("kind") == "dis"]
    if not discharge:
        return None
    hour_start = hour * 60
    hour_end = (hour + 1) * 60
    latest_end: int | None = None
    for seg in discharge:
        to_min = _hhmm_to_minute_of_day(seg["to"])
        if to_min is None:
            continue
        if hour_start <= to_min < hour_end:
            if latest_end is None or to_min > latest_end:
                latest_end = to_min
    if latest_end is None:
        return None
    return f"{latest_end // 60:02d}:{latest_end % 60:02d}"


def timer_discharge_active_at(timer_txt: str, now: datetime) -> bool:
    """True when *now* is inside a discharge segment (start <= now < end)."""
    if not timer_txt or not str(timer_txt).strip():
        return False
    now_min = now.hour * 60 + now.minute
    for seg in parse_timer_schedule_segments(timer_txt):
        if seg.get("kind") != "dis":
            continue
        start_min = _hhmm_to_minute_of_day(seg["from"])
        end_min = _hhmm_to_minute_of_day(seg["to"])
        if start_min is None or end_min is None:
            continue
        if start_min <= now_min < end_min:
            return True
    return False


def timer_charge_active_at(timer_txt: str, now: datetime) -> bool:
    """True when *now* is inside a grid-charge segment (start <= now < end)."""
    if not timer_txt or not str(timer_txt).strip():
        return False
    now_min = now.hour * 60 + now.minute
    for seg in parse_timer_schedule_segments(timer_txt):
        if seg.get("kind") != "chg":
            continue
        start_min = _hhmm_to_minute_of_day(seg["from"])
        end_min = _hhmm_to_minute_of_day(seg["to"])
        if start_min is None or end_min is None:
            continue
        if start_min <= now_min < end_min:
            return True
    return False


def timer_covers_quarter(timer_txt: str, hour: int, quarter: int) -> bool:
    """True when any Chg/Dis segment overlaps a 15-min slot in *hour*."""
    if not str(timer_txt or "").strip():
        return False
    slot_start = int(hour) * 60 + int(quarter) * 15
    slot_end = slot_start + 15
    for seg in parse_timer_schedule_segments(timer_txt):
        if seg.get("kind") not in ("dis", "chg"):
            continue
        from_min = _hhmm_to_minute_of_day(seg["from"])
        to_min = _hhmm_to_minute_of_day(seg["to"])
        if from_min is None or to_min is None:
            continue
        if slot_start < to_min and slot_end > from_min:
            return True
    return False


def sa_discharge_slot_active_at(rules: dict[str, Any] | None, now: datetime) -> bool:
    """True when *now* is inside any active SA discharge slot window."""
    if not rules:
        return False
    now_min = now.hour * 60 + now.minute
    for slot in rules.get("discharge_slots") or []:
        from_t = str(slot.get("from") or "00:00")
        to_t = str(slot.get("to") or "00:00")
        if from_t == "00:00" and to_t == "00:00":
            continue
        power_kw = float(slot.get("power_kw") or 0.0)
        if power_kw <= 0 and slot.get("power_w") is not None:
            power_kw = float(slot.get("power_w") or 0) / 1000.0
        if power_kw <= 0:
            continue
        from_min = _hhmm_to_minute_of_day(from_t)
        to_min = _hhmm_to_minute_of_day(to_t)
        if from_min is None or to_min is None:
            continue
        if from_min <= now_min < to_min:
            return True
    return False


def sa_discharge_timer_for_hour(
    rules: dict[str, Any] | None,
    hour: int,
    *,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Timer Schedule cell text from live SA discharge slot overlapping *hour*.

    Uses slot window even when timed_discharge_enabled is false — SA often clears
    the checkbox before the slot end while export is still winding down.
    """
    if not rules:
        return ""
    hour_start = int(hour) * 60
    hour_end = hour_start + 60
    min_soc = int(plan_min_soc_pct(cfg)) if cfg else None
    for slot in rules.get("discharge_slots") or []:
        from_t = str(slot.get("from") or "00:00")
        to_t = str(slot.get("to") or "00:00")
        if from_t == "00:00" and to_t == "00:00":
            continue
        from_min = _hhmm_to_minute_of_day(from_t)
        to_min = _hhmm_to_minute_of_day(to_t)
        if from_min is None or to_min is None:
            continue
        if from_min >= hour_end or to_min <= hour_start:
            continue
        power_kw = float(slot.get("power_kw") or 0.0)
        if power_kw <= 0 and slot.get("power_w") is not None:
            power_kw = float(slot.get("power_w") or 0) / 1000.0
        cap_raw = slot.get("capacity_pct")
        if cap_raw is not None:
            cap = int(round(float(cap_raw)))
        elif min_soc is not None:
            cap = min_soc
        else:
            continue
        return f"Dis {from_t}-{to_t} {power_kw:g}kW cap{cap}%"
    return ""


_TIMER_END_LOOKBACK_MIN = 15


def _timer_kind_end_due(
    timer_txt: str,
    now: datetime,
    *,
    kind: str,
    plan_hour: int | None = None,
) -> tuple[bool, str | None]:
    """True when a *kind* segment end in *plan_hour*'s row is <= *now* and fresh."""
    if not timer_txt or not str(timer_txt).strip():
        return False, None
    now_min = now.hour * 60 + now.minute
    row_hour = now.hour if plan_hour is None else plan_hour
    row_start = row_hour * 60
    row_end = (row_hour + 1) * 60
    earliest_relevant = now_min - _TIMER_END_LOOKBACK_MIN
    latest_due: int | None = None
    for seg in parse_timer_schedule_segments(timer_txt):
        if seg.get("kind") != kind:
            continue
        end_min = _hhmm_to_minute_of_day(seg["to"])
        if end_min is None or end_min > now_min:
            continue
        if end_min < earliest_relevant:
            continue
        if not (row_start < end_min <= row_end):
            continue
        if latest_due is None or end_min > latest_due:
            latest_due = end_min
    if latest_due is None:
        return False, None
    return True, f"{latest_due // 60:02d}:{latest_due % 60:02d}"


def timer_discharge_end_due(
    timer_txt: str,
    now: datetime,
    *,
    plan_hour: int | None = None,
) -> tuple[bool, str | None]:
    """True when a discharge end in *plan_hour*'s row is <= *now* and still fresh.

    Covers in-hour ends (e.g. 22:45) and full-hour ends (e.g. 22:00 on row 21).
    """
    return _timer_kind_end_due(
        timer_txt, now, kind="dis", plan_hour=plan_hour,
    )


def timer_charge_end_due(
    timer_txt: str,
    now: datetime,
    *,
    plan_hour: int | None = None,
) -> tuple[bool, str | None]:
    """True when a grid-charge end in *plan_hour*'s row is <= *now* and still fresh.

    Needed so mid-quarter Limit-home clears Timed charge after Chg …-H:30/H:45,
    not only after discharge ends.
    """
    return _timer_kind_end_due(
        timer_txt, now, kind="chg", plan_hour=plan_hour,
    )


def plan_row_grid_export_kwh(row: dict[str, Any] | None) -> float:
    if not row:
        return 0.0
    v = row.get("grid_export")
    if v is None:
        return 0.0
    return max(0.0, float(v))


def build_sa_schedule_from_hour_row(
    rows: list[dict],
    hour: int,
    cfg: dict,
    existing: dict[str, Any] | None = None,
    live_soc_pct: float | None = None,
) -> dict[str, Any] | None:
    """SA write payload from one Energy arbitrage hour row (Timer Schedule column)."""
    row = next(
        (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
        None,
    )
    if not row:
        return None
    timer_txt = str(row.get("timer_schedule") or "").strip()
    if not timer_txt:
        return None

    segments = parse_timer_schedule_segments(timer_txt)
    if not segments:
        return None

    existing = existing or {}
    charge_cap = plan_timer_charge_power_kw(cfg)
    discharge_cap = plan_timer_discharge_power_kw(cfg)
    charge_slots = _ensure_three_slots(existing.get("charge_slots", []), "charge")
    discharge_slots = _ensure_three_slots(existing.get("discharge_slots", []), "discharge")
    timed_charge = False
    timed_discharge = False

    for seg in segments:
        if seg["kind"] == "chg":
            timed_charge = True
            tpl = charge_slots[0]
            cap = int(seg["capacity_pct"])
            if live_soc_pct is not None:
                try:
                    cap = max(
                        cap,
                        min(100, int(float(live_soc_pct)) + CHARGE_CAP_MIN_HEADROOM_PCT),
                    )
                except (TypeError, ValueError):
                    pass
            charge_slots[0] = {
                "slot": 1,
                "from": seg["from"],
                "to": seg["to"],
                "capacity_pct": cap,
                "voltage_v": float(tpl.get("voltage_v", 58.0)),
                "power_kw": round(min(float(seg["power_kw"]), charge_cap), 2),
                "grid": True,
                "generator": False,
            }
        elif seg["kind"] == "dis":
            timed_discharge = True
            tpl = discharge_slots[0]
            discharge_slots[0] = {
                "slot": 1,
                "from": seg["from"],
                "to": seg["to"],
                "capacity_pct": seg["capacity_pct"],
                "voltage_v": float(tpl.get("voltage_v", 42.0)),
                "power_kw": round(min(float(seg["power_kw"]), discharge_cap), 2),
            }

    if not timed_charge and not timed_discharge:
        return None

    return {
        "target_hour": hour,
        "planned_action": row.get("action"),
        "timer_schedule": timer_txt,
        "timed_charge_enabled": timed_charge,
        "timed_discharge_enabled": timed_discharge,
        "charge_slots": charge_slots,
        "discharge_slots": discharge_slots,
    }


def _timer_slot_matches(sa_slot: dict[str, Any], exp_slot: dict[str, Any]) -> bool:
    """True when SA slot 1 matches the plan-derived slot (times, power, cap)."""
    if not _slot_is_active(exp_slot):
        return not _slot_is_active(sa_slot)
    if not _slot_is_active(sa_slot):
        return False
    for key in ("from", "to"):
        if str(sa_slot.get(key) or "") != str(exp_slot.get(key) or ""):
            return False
    if abs(float(sa_slot.get("power_kw") or 0) - float(exp_slot.get("power_kw") or 0)) > 0.05:
        return False
    sa_cap = int(round(float(sa_slot.get("capacity_pct") or 0)))
    exp_cap = int(round(float(exp_slot.get("capacity_pct") or 0)))
    return sa_cap == exp_cap


def sa_schedule_matches_plan_row(
    rows: list[dict],
    hour: int,
    cfg: dict,
    rules: dict[str, Any],
) -> bool:
    """True when live SA timed charge/discharge flags and slot 1 match the plan hour row."""
    expected = build_sa_schedule_from_hour_row(rows, hour, cfg, existing=rules)
    if expected is None:
        return not rules.get("timed_charge_enabled") and not rules.get("timed_discharge_enabled")

    if bool(rules.get("timed_charge_enabled")) != bool(expected.get("timed_charge_enabled")):
        return False
    if bool(rules.get("timed_discharge_enabled")) != bool(expected.get("timed_discharge_enabled")):
        return False

    sa_charge = (rules.get("charge_slots") or [{}])[0]
    exp_charge = (expected.get("charge_slots") or [{}])[0]
    if expected.get("timed_charge_enabled"):
        if not _timer_slot_matches(sa_charge, exp_charge):
            return False
    elif _slot_is_active(sa_charge):
        return False

    sa_dis = (rules.get("discharge_slots") or [{}])[0]
    exp_dis = (expected.get("discharge_slots") or [{}])[0]
    if expected.get("timed_discharge_enabled"):
        if not _timer_slot_matches(sa_dis, exp_dis):
            return False
    elif _slot_is_active(sa_dis):
        return False

    return True


def format_hour_timer_schedule(
    hour: int,
    schedule: dict[str, Any],
    *,
    cfg: dict | None = None,
) -> str:
    """SA timer text for one clock hour — clipped range; omit if overlap below min period."""
    min_block = plan_timer_min_block_minutes(cfg or {})
    parts: list[str] = []
    for kind, prefix in (("charge_slots", "Chg"), ("discharge_slots", "Dis")):
        for slot in schedule.get(kind) or []:
            if not _slot_is_active(slot):
                continue
            clip = _hour_slot_clip(slot, hour)
            if clip is None:
                continue
            clip_from, clip_to, dur = clip
            if dur < min_block:
                continue
            cap = slot.get("capacity_pct", "")
            pw = slot.get("power_kw", 0)
            parts.append(f"{prefix} {clip_from}-{clip_to} {pw}kW cap{cap}%")
    return " | ".join(parts) if parts else ""


def _blocks_to_slots(
    blocks: list[dict[str, Any]],
    kind: str,
    templates: list[dict[str, Any]],
    cfg: dict,
) -> list[dict[str, Any]]:
    min_soc = plan_min_soc_pct(cfg)
    charge_kw = plan_timer_charge_power_kw(cfg)
    discharge_kw = plan_timer_discharge_power_kw(cfg)
    slots: list[dict[str, Any]] = []

    for i in range(3):
        tpl = templates[i] if i < len(templates) else {}
        if i >= len(blocks):
            slots.append(_inactive_slot(i + 1, kind, tpl))
            continue

        blk = blocks[i]
        from_h = blk["from_hour"] % 24
        to_h = blk["to_hour"] % 24
        timer_kw = charge_kw if kind == "charge" else discharge_kw
        slot: dict[str, Any] = {
            "slot": i + 1,
            "from": f"{from_h:02d}:00",
            "to": f"{to_h:02d}:00",
            "capacity_pct": (
                int(blk["capacity_pct"])
                if kind == "charge"
                else max(int(min_soc), int(round(float(blk.get("capacity_pct") or min_soc))))
            ),
            "voltage_v": float(tpl.get("voltage_v", 58.0 if kind == "charge" else 42.0)),
            "power_kw": timer_kw,
        }
        if kind == "charge":
            slot["grid"] = True
            slot["generator"] = False
        slots.append(slot)

    return slots


def derive_timer_schedule(
    rows: list[dict],
    cfg: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    charge_blocks = _merge_blocks(rows, ACTION_CHARGE_GRID)
    discharge_blocks = _merge_blocks(rows, ACTION_DISCHARGE_GRID)

    return {
        "timed_charge_enabled": bool(charge_blocks),
        "timed_discharge_enabled": bool(discharge_blocks),
        "charge_slots": _blocks_to_slots(charge_blocks, "charge", existing.get("charge_slots", []), cfg),
        "discharge_slots": _blocks_to_slots(discharge_blocks, "discharge", existing.get("discharge_slots", []), cfg),
    }


def _ensure_three_slots(slots: list[dict], kind: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for i in range(3):
        if i < len(slots):
            result.append(dict(slots[i]))
        else:
            result.append(_inactive_slot(i + 1, kind, None))
    return result


def _row_power_kw(row: dict, action: str, cfg: dict) -> float:
    """Configured timer power cap — inverter splits between load and grid."""
    if action == ACTION_CHARGE_GRID:
        return plan_timer_charge_power_kw(cfg)
    if action == ACTION_DISCHARGE_GRID:
        return plan_timer_discharge_power_kw(cfg)
    return 0.0


def build_hourly_schedule(
    rows: list[dict],
    target_hour: int,
    cfg: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    charge_slots = _ensure_three_slots(existing.get("charge_slots", []), "charge")
    discharge_slots = _ensure_three_slots(existing.get("discharge_slots", []), "discharge")

    row = next(
        (r for r in rows if r.get("hour") == target_hour and r.get("start") != "TOTAL"),
        None,
    )
    action = normalize_action(row.get("action", "")) if row else ACTION_IDLE_GRID
    timer_txt = (row.get("timer_schedule") or "").strip() if row else ""
    if timer_txt.startswith("Dis"):
        action = ACTION_DISCHARGE_GRID
    elif timer_txt.startswith("Chg"):
        action = ACTION_CHARGE_GRID
    from_h = target_hour % 24
    to_h = (target_hour + 1) % 24
    min_soc = int(plan_min_soc_pct(cfg))

    timed_charge = False
    timed_discharge = False

    if action == ACTION_CHARGE_GRID and row:
        timed_charge = True
        tpl = charge_slots[0]
        charge_slots[0] = {
            "slot": 1,
            "from": f"{from_h:02d}:00",
            "to": f"{to_h:02d}:00",
            "capacity_pct": int(round(float(row.get("soc", 80)))),
            "voltage_v": float(tpl.get("voltage_v", 58.0)),
            "power_kw": _row_power_kw(row, action, cfg),
            "grid": True,
            "generator": False,
        }
        discharge_slots[0] = _inactive_slot(1, "discharge", discharge_slots[0])

    elif action == ACTION_DISCHARGE_GRID and row:
        timed_discharge = True
        tpl = discharge_slots[0]
        dis_cap = discharge_cap_pct_from_row(row, min_soc)
        discharge_slots[0] = {
            "slot": 1,
            "from": f"{from_h:02d}:00",
            "to": f"{to_h:02d}:00",
            "capacity_pct": max(min_soc, dis_cap),
            "voltage_v": float(tpl.get("voltage_v", 42.0)),
            "power_kw": _row_power_kw(row, action, cfg),
        }
        charge_slots[0] = _inactive_slot(1, "charge", charge_slots[0])

    else:
        charge_slots[0] = _inactive_slot(1, "charge", charge_slots[0])
        discharge_slots[0] = _inactive_slot(1, "discharge", discharge_slots[0])

    return {
        "target_hour": target_hour,
        "planned_action": action,
        "timed_charge_enabled": timed_charge,
        "timed_discharge_enabled": timed_discharge,
        "charge_slots": charge_slots,
        "discharge_slots": discharge_slots,
    }


def _schedule_timed_enabled(schedule: dict[str, Any]) -> bool:
    return bool(schedule.get("timed_charge_enabled") or schedule.get("timed_discharge_enabled"))


def _slot_covers_minute(slot: dict[str, Any], minute_of_day: int) -> bool:
    if not _slot_is_active(slot):
        return False
    start = _parse_hhmm_to_min(slot.get("from", "00:00"))
    end = _parse_hhmm_to_min(slot.get("to", "00:00"))
    return start is not None and end is not None and start <= minute_of_day < end


def pick_sa_timer_schedule(
    rows: list[dict],
    *,
    now_hour: int,
    cfg: dict,
    existing: dict[str, Any] | None = None,
    proposed: dict[str, Any] | None = None,
    now_minute: int | None = None,
) -> dict[str, Any] | None:
    """SA write payload: q15 block covering now, else timed current hour, else next hour."""
    existing = existing or {}
    minute = now_minute if now_minute is not None else now_hour * 60

    if proposed and _schedule_timed_enabled(proposed):
        discharge = (proposed.get("discharge_slots") or [{}])[0]
        charge = (proposed.get("charge_slots") or [{}])[0]
        if proposed.get("timed_discharge_enabled") and _slot_covers_minute(discharge, minute):
            return proposed
        if proposed.get("timed_charge_enabled") and _slot_covers_minute(charge, minute):
            return proposed

    current = build_hourly_schedule(rows, now_hour, cfg, existing)
    if _schedule_timed_enabled(current):
        return current

    next_h = (now_hour + 1) % 24
    upcoming = build_hourly_schedule(rows, next_h, cfg, existing)
    if _schedule_timed_enabled(upcoming):
        return upcoming
    return None
