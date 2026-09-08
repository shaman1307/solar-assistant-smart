"""One-step battery/grid physics shared by the optimizer, q15 replay, and tail cost."""

from __future__ import annotations

from dataclasses import dataclass

MIN_EPS_STEP_KWH = 0.001

__all__ = [
    "HourControl",
    "HourPhysics",
    "MIN_EPS_STEP_KWH",
    "apply_grid_charge_ac",
    "eps_step_kwh",
    "pv_load_energy_split",
    "simulate_hour",
    "slots_per_hour_from_scale",
]


def pv_load_energy_split(
    pv: float,
    load: float,
    *,
    eta_pv_load: float,
) -> tuple[float, float]:
    """Split AC-meter PV vs AC load into deficit and surplus for battery/export.

    Plan PV/load series are already AC (inverter / house meter). Do not apply
    ``eta_pv_load`` as a second conversion — that double-counts and inflates
    SOC on PV→battery. ``eta_pv_load <= 0`` still means ignore PV (full deficit).
    """
    if eta_pv_load <= 0:
        return max(0.0, load), max(0.0, pv)
    return max(0.0, load - pv), max(0.0, pv - load)


def eps_step_kwh(epsilon: float, step_scale: float) -> float:
    """Per-step epsilon floor shared by optimizer and q15 replay callers."""
    return max(float(epsilon) * float(step_scale), MIN_EPS_STEP_KWH)


def slots_per_hour_from_scale(step_scale: float) -> int:
    return max(1, int(round(1.0 / step_scale)) if step_scale > 0 else 1)


@dataclass
class HourControl:
    # Grid charge energy this step (kWh AC on the meter). DC into battery = AC × eta_grid.
    grid_charge_kw: float
    battery_export_kwh: float
    load_from_grid: bool = False


@dataclass
class HourPhysics:
    soc_end: float
    battery_delta: float
    grid_import: float
    grid_export: float


def apply_grid_charge_ac(
    *,
    soc: float,
    battery_delta: float,
    grid_import: float,
    ac_charge_kwh: float,
    battery_cap: float,
    eta_grid: float,
    epsilon: float,
) -> tuple[float, float, float]:
    """Draw ac_charge_kwh from grid; store AC × eta into battery."""
    head_room = max(0.0, battery_cap - soc)
    if ac_charge_kwh <= epsilon or head_room <= epsilon:
        return soc, battery_delta, grid_import
    max_ac = head_room / eta_grid if eta_grid > 0 else head_room
    ac = min(ac_charge_kwh, max_ac)
    stored = ac * eta_grid if eta_grid > 0 else ac
    return soc + stored, battery_delta + stored, grid_import + ac


def simulate_hour(
    soc_kwh: float,
    pv: float,
    load: float,
    control: HourControl,
    *,
    battery_cap: float,
    min_kwh: float,
    ac_cap_kw: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    epsilon: float,
    reserve_soc_kwh: float | None = None,
    discharge_dc_cap_kwh: float | None = None,
) -> HourPhysics:
    """One step: AC-meter PV vs AC load; PV→battery applies eta_pv_battery.

    ``ac_cap_kw`` is inverter AC headroom this step (export bus).
    ``discharge_dc_cap_kwh`` caps total battery DC withdraw (load + export);
    default None keeps legacy behaviour (no separate DC power ceiling).

    grid_charge_kw is AC kWh from the meter this step (Chg 6kW × 1h → 6 kWh import).
    DC stored = AC × eta_grid. Charge is applied before house load when load stays on
    the battery, so a Chg hour at min SOC nets charge − house on the battery.
    """
    reserve = min_kwh if reserve_soc_kwh is None else max(min_kwh, reserve_soc_kwh)
    soc = soc_kwh
    grid_import = 0.0
    grid_export = 0.0
    battery_delta = 0.0
    dc_used = 0.0

    def _dc_room() -> float:
        if discharge_dc_cap_kwh is None:
            return float("inf")
        return max(0.0, float(discharge_dc_cap_kwh) - dc_used)

    deficit, pv_surplus = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    # Load from grid only when explicitly requested. Grid charge does not force
    # house load onto the meter — load priority stays on the battery (SOC > min).
    load_on_grid = control.load_from_grid
    # Charge-before-load when house is on battery: same-hour Chg can supply load.
    charge_before_load = (
        control.grid_charge_kw > epsilon and not load_on_grid
    )

    export_headroom = max(0.0, ac_cap_kw - load)

    head_room = max(0.0, battery_cap - soc)
    if pv_surplus > epsilon:
        if head_room > epsilon and eta_pv_battery > 0:
            taken = min(pv_surplus, head_room / eta_pv_battery)
            stored = taken * eta_pv_battery
            soc += stored
            battery_delta += stored
            pv_surplus -= taken
        if pv_surplus > epsilon:
            pv_exp = min(
                pv_surplus * eta_pv_grid,
                max(0.0, export_headroom - grid_export),
            )
            grid_export += pv_exp

    if charge_before_load:
        soc, battery_delta, grid_import = apply_grid_charge_ac(
            soc=soc,
            battery_delta=battery_delta,
            grid_import=grid_import,
            ac_charge_kwh=control.grid_charge_kw,
            battery_cap=battery_cap,
            eta_grid=eta_grid,
            epsilon=epsilon,
        )

    available = max(0.0, soc - min_kwh)
    if deficit > epsilon:
        if load_on_grid:
            grid_import += deficit
        else:
            max_from_dc = _dc_room() * eta_out if eta_out > 0 else 0.0
            supplied = min(deficit, available * eta_out, max_from_dc)
            withdraw_load = supplied / eta_out if eta_out > 0 else 0.0
            soc -= withdraw_load
            battery_delta -= withdraw_load
            dc_used += withdraw_load
            available = max(0.0, soc - min_kwh)
            if deficit > supplied + epsilon:
                grid_import += deficit - supplied

    batt_export = min(max(0.0, control.battery_export_kwh), export_headroom)
    available_export = max(0.0, soc - reserve)
    if batt_export > epsilon and available_export > epsilon and eta_out > 0:
        export_withdraw = min(
            batt_export / eta_out,
            available_export,
            _dc_room(),
        )
        soc -= export_withdraw
        batt_export = export_withdraw * eta_out
        grid_export += batt_export
        battery_delta -= export_withdraw
        dc_used += export_withdraw

    if not charge_before_load:
        soc, battery_delta, grid_import = apply_grid_charge_ac(
            soc=soc,
            battery_delta=battery_delta,
            grid_import=grid_import,
            ac_charge_kwh=control.grid_charge_kw,
            battery_cap=battery_cap,
            eta_grid=eta_grid,
            epsilon=epsilon,
        )

    # Cap SOC at capacity; leave below-min SOC unchanged (no lift to min_kwh).
    soc = min(battery_cap, max(0.0, soc))
    return HourPhysics(
        soc_end=soc,
        battery_delta=battery_delta,
        grid_import=grid_import,
        grid_export=grid_export,
    )
