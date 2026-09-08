"""Physics-only and hardcoded evening-Dis baselines for monthly history."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from .g12_pricing import get_buy_price
from .inverter_sim import resolve_hour0_soc_kwh
from .plan_cost import hour_meter_cash_pln
from .plan_optimizer import HourControl, simulate_hour

BaselineMode = Literal["physics", "evening_dis"]

# Hover text for Monthly history Baseline column header.
BASELINE_HEADER_TITLE = (
    "Physics baseline (no timer control)\n"
    "Charge: PV only\n"
    "Discharge to grid: battery overflow only\n"
    "House load: battery while SOC > min\n"
    "SOC chains day-to-day within the month"
)

# Alias for BASELINE_HEADER_TITLE.
BASELINE_SA_HEADER_TITLE = BASELINE_HEADER_TITLE

# Hardcoded evening dump rule: Dis 8 kW from 19:00 to 22:00 down to SOC 30%.
EVENING_DIS_START_HOUR = 19  # inclusive
EVENING_DIS_END_HOUR = 22  # exclusive → hours 19, 20, 21
EVENING_DIS_POWER_KW = 8.0
EVENING_DIS_FLOOR_SOC_PCT = 30.0

DIS_BASELINE_HEADER_TITLE = (
    "Evening Dis baseline (hardcoded)\n"
    f"Dis {EVENING_DIS_POWER_KW:.0f} kW {EVENING_DIS_START_HOUR}:00–"
    f"{EVENING_DIS_END_HOUR}:00 down to SOC {EVENING_DIS_FLOOR_SOC_PCT:.0f}%\n"
    "Other hours: physics only (PV charge, overflow export)\n"
    "SOC chains day-to-day within the month"
)


def _hourly_slot(
    hourly: dict[str, list[float | None]] | None,
    hour: int,
    key: str,
) -> float | None:
    if not hourly:
        return None
    arr = hourly.get(key) or [None] * 24
    if 0 <= hour < len(arr):
        return arr[hour]
    return None


def _hour_rce_price(
    quarters: list[float | None],
    hour: int,
) -> float | None:
    c0 = hour * 4
    hour_rce_q15 = list(quarters[c0:c0 + 4])
    hour_rce_vals = [float(v) for v in hour_rce_q15 if v is not None]
    if not hour_rce_vals:
        return None
    return round(sum(hour_rce_vals) / len(hour_rce_vals), 4)


def _control_for_hour(
    hour: int,
    mode: BaselineMode,
    *,
    battery_cap: float,
    min_kwh: float,
) -> tuple[HourControl, float]:
    """Return (control, reserve_soc_kwh) for this hour."""
    if (
        mode == "evening_dis"
        and EVENING_DIS_START_HOUR <= hour < EVENING_DIS_END_HOUR
    ):
        floor = (EVENING_DIS_FLOOR_SOC_PCT / 100.0) * battery_cap
        return (
            HourControl(0.0, EVENING_DIS_POWER_KW, load_from_grid=False),
            max(min_kwh, floor),
        )
    return HourControl(0.0, 0.0, load_from_grid=False), min_kwh


def build_baseline_history_rows(
    plan_date: str,
    until_hour: int,
    hourly: dict[str, list[float | None]],
    quarters_by_date: dict[str, list[float | None]],
    cfg: dict,
    params: dict[str, float | int],
    *,
    initial_soc_kwh: float | None = None,
    mode: BaselineMode = "physics",
) -> tuple[list[dict[str, Any]], float]:
    """Replay IHDB PV/load for a baseline mode.

    *physics*: no timer Chg/Dis — load priority + PV overflow only.
    *evening_dis*: same, plus hardcoded Dis 8 kW 19–22 until SOC 30%.

    *initial_soc_kwh*: when set (month carry from the previous baseline day), use
    that seed instead of Influx hour-0 SOC. Returns (hour rows, end-of-day SOC kWh).
    """
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = (float(params["min_soc_pct"]) / 100.0) * battery_cap
    if until_hour <= 0 or not hourly:
        seed = min_kwh if initial_soc_kwh is None else float(initial_soc_kwh)
        return [], max(min_kwh, min(battery_cap, seed))

    ac_cap_kw = float(cfg["inverter"]["ac_capacity_kw"])
    epsilon = float(params["epsilon_kwh"])
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])

    base = datetime.strptime(plan_date, "%Y-%m-%d")
    quarters = quarters_by_date.get(plan_date) or []
    if initial_soc_kwh is not None:
        soc_kwh = max(min_kwh, min(battery_cap, float(initial_soc_kwh)))
    else:
        try:
            soc_kwh, _ = resolve_hour0_soc_kwh(hourly, battery_cap)
        except ValueError:
            soc_kwh = min_kwh
        soc_kwh = max(min_kwh, min(battery_cap, soc_kwh))
    rows: list[dict[str, Any]] = []

    for h in range(0, until_hour):
        pv_h = _hourly_slot(hourly, h, "pv")
        load_h = _hourly_slot(hourly, h, "load")
        if pv_h is None and load_h is None:
            continue

        pv = float(pv_h) if pv_h is not None else 0.0
        load = float(load_h) if load_h is not None else 0.0
        control, reserve = _control_for_hour(
            h, mode, battery_cap=battery_cap, min_kwh=min_kwh,
        )

        phys = simulate_hour(
            soc_kwh,
            pv,
            load,
            control,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            ac_cap_kw=ac_cap_kw,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            epsilon=epsilon,
            reserve_soc_kwh=reserve,
        )
        soc_kwh = phys.soc_end

        hour_dt = base.replace(hour=h)
        buy_price, g12_zone = get_buy_price(hour_dt, cfg)
        rce_price = _hour_rce_price(quarters, h)
        cash = hour_meter_cash_pln(
            phys.grid_import,
            phys.grid_export,
            buy_price,
            rce_price,
            cfg,
            g12_zone=g12_zone,
        )
        rows.append(
            {
                "hour": h,
                "g12_zone": g12_zone,
                "grid_import": phys.grid_import,
                "grid_export": phys.grid_export,
                "export_revenue": cash["export_revenue"],
                "import_energy_cost": cash["import_energy_cost"],
                "energy_cost": cash["energy_cost"],
                "service_cost": cash["service_cost"],
                "cost": cash["cost"],
                "soc_end_kwh": round(soc_kwh, 4),
            }
        )
    return rows, float(soc_kwh)


def summarize_baseline_rows(
    rows: list[dict[str, Any]],
    *,
    key_prefix: str = "baseline",
) -> dict[str, float]:
    export_revenue = sum(float(r.get("export_revenue") or 0.0) for r in rows)
    import_tariff = sum(float(r.get("import_energy_cost") or 0.0) for r in rows)
    service_cost = sum(float(r.get("service_cost") or 0.0) for r in rows)
    peak_import = sum(
        float(r.get("grid_import") or 0.0)
        for r in rows
        if r.get("g12_zone") == "peak"
    )
    offpeak_import = sum(
        float(r.get("grid_import") or 0.0)
        for r in rows
        if r.get("g12_zone") == "offpeak"
    )
    # Net = export RCE − import tariff (same signed net as MH energy columns).
    baseline_net = round(export_revenue - import_tariff, 4)
    return {
        f"{key_prefix}_export_revenue": round(export_revenue, 4),
        f"{key_prefix}_import_energy_cost": round(import_tariff, 4),
        f"{key_prefix}_energy_cost": round(import_tariff - export_revenue, 4),
        f"{key_prefix}_service_cost": round(service_cost, 4),
        f"{key_prefix}_cost": baseline_net,
        f"{key_prefix}_grid_import_peak": round(peak_import, 4),
        f"{key_prefix}_grid_import_offpeak": round(offpeak_import, 4),
        f"{key_prefix}_grid_import": round(peak_import + offpeak_import, 4),
    }


def attach_baseline_savings(
    summary: dict[str, Any],
    baseline_rows: list[dict[str, Any]],
    *,
    dis_baseline_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add physics (+ optional evening-Dis) baseline nets and Saved vs each."""
    from .plan_cost import month_energy_cost_total, month_import_cost_total, month_savings_pln

    baseline = summarize_baseline_rows(baseline_rows)
    summary.update(baseline)
    export_revenue = float(summary.get("export_revenue") or 0.0)
    import_energy = float(summary.get("import_energy_cost") or 0.0)
    service_cost = float(summary.get("service_cost") or 0.0)
    energy_cost_total = month_energy_cost_total(export_revenue, import_energy)
    import_cost_total = month_import_cost_total(service_cost)
    summary["energy_cost_total"] = energy_cost_total
    summary["import_cost_total"] = import_cost_total
    summary["savings_pln"] = month_savings_pln(
        export_revenue,
        import_energy,
        float(baseline["baseline_export_revenue"]),
        float(baseline["baseline_import_energy_cost"]),
    )
    if dis_baseline_rows is not None:
        dis = summarize_baseline_rows(dis_baseline_rows, key_prefix="dis_baseline")
        summary.update(dis)
        summary["dis_savings_pln"] = month_savings_pln(
            export_revenue,
            import_energy,
            float(dis["dis_baseline_export_revenue"]),
            float(dis["dis_baseline_import_energy_cost"]),
        )
    return summary
