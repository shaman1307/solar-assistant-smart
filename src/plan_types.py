"""Shared types for the energy-arbitrage plan (controls, physics, hour rows)."""

from __future__ import annotations

from typing import Any, TypedDict

from .plan_physics import HourControl, HourPhysics

__all__ = ["HourControl", "HourPhysics", "PlanHourRow", "Q15Slot"]


class Q15Slot(TypedDict, total=False):
    """One 15-minute plan slot (optimizer replay and UI table)."""

    hour: int
    quarter: int
    action: str
    pv: float
    load: float
    production: float
    consumption: float
    grid_import: float
    grid_export: float
    battery_delta: float
    battery: float
    soc_pct: float
    soc: float
    soc_end: float
    reserve_kwh: float
    reserve_soc_pct: float
    rce: float | None
    from_actual: bool


class PlanHourRow(TypedDict, total=False):
    """One clock-hour Energy Arbitrage row (JSON / SQLite / UI)."""

    hour: int
    plan_date: str
    start: str
    action: str
    timer_schedule: str
    timer_schedule_manual: bool
    q15: list[dict[str, Any]]
    production: float
    consumption: float
    battery: float
    bat_charge: float
    bat_discharge: float
    grid_import: float
    grid_export: float
    soc: float
    import_cost: float
    export_revenue: float
    energy_cost: float
    service_cost: float
    cost: float
    rce_price: float | None
    rce_q15: list[float | None]
    export_credit: float | None
    g12_zone: str
    buy_price: float
    export_planned: bool
    hour_labels_locked: bool
    soc_blended: bool
    history_hour: bool
