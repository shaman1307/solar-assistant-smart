"""Tail-hour balance cost after optimization horizon (physics + G12 tariff)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .g12_pricing import get_buy_price
from .plan_physics import HourControl, pv_load_energy_split, simulate_hour

__all__ = ["pv_load_energy_split", "build_tail_hour_arrays", "tail_balance_cost_pln"]


def _natural_hour(
    soc: float,
    pv: float,
    load: float,
    *,
    battery_cap: float,
    min_kwh: float,
    ac_cap_kw: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    epsilon: float,
) -> tuple[float, float, float]:
    """Battery+PV only. Returns (soc_end, grid_import, grid_export)."""
    phys = simulate_hour(
        soc, pv, load, HourControl(0.0, 0.0),
        battery_cap=battery_cap,
        min_kwh=min_kwh,
        ac_cap_kw=ac_cap_kw,
        eta_grid=1.0,
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid,
        eta_pv_battery=eta_pv_battery,
        epsilon=epsilon,
    )
    soc_end = max(min_kwh, min(battery_cap, phys.soc_end))
    return soc_end, phys.grid_import, phys.grid_export


def _forecast_day_key(dt: datetime, today_date) -> str:
    return "today" if dt.date() == today_date else "tomorrow"


def build_tail_hour_arrays(
    end_dt: datetime,
    today_date,
    forecast: dict[str, Any],
    cfg: dict,
    rce_map: dict[tuple[str, int], float | None],
    export_credit_fn,
    *,
    tail_start_hour: int | None = None,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """PV/load/buy/export-credit for calendar hours after the optimized horizon.

  *tail_start_hour* is the first calendar hour not covered by DP steps (e.g. 24 when
    the plan ends at 23:45). Defaults to *end_dt.hour* when *tail_start_hour* is
    omitted (hourly horizon).
    """
    day_key = _forecast_day_key(end_dt, today_date)
    pv_day = forecast[day_key]["pv"]
    load_day = forecast[day_key]["load"]
    tail_pv: list[float] = []
    tail_load: list[float] = []
    tail_buy: list[float] = []
    tail_export_credit: list[float] = []
    date_str = end_dt.strftime("%Y-%m-%d")
    start_h = end_dt.hour if tail_start_hour is None else int(tail_start_hour)
    for h in range(start_h, 24):
        tail_pv.append(float(pv_day[h]))
        tail_load.append(float(load_day[h]))
        dt = end_dt.replace(hour=h, minute=0, second=0, microsecond=0)
        buy, _ = get_buy_price(dt, cfg)
        tail_buy.append(buy)
        rce = rce_map.get((date_str, h))
        tail_export_credit.append(export_credit_fn(rce, from_battery=False))
    return tail_pv, tail_load, tail_buy, tail_export_credit


def tail_balance_cost_pln(
    soc_kwh: float,
    tail_pv: list[float],
    tail_load: list[float],
    tail_buy: list[float],
    tail_export_credit: list[float],
    *,
    battery_cap: float,
    min_kwh: float,
    ac_cap_kw: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    epsilon: float,
) -> float:
    """PLN: grid imports + PV spill opportunity cost (buy − export credit) after horizon."""
    soc = soc_kwh
    cost = 0.0
    for pv, load, buy, export_credit in zip(
        tail_pv, tail_load, tail_buy, tail_export_credit,
    ):
        soc, grid_import, grid_export = _natural_hour(
            soc, pv, load,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=ac_cap_kw,
            eta_out=eta_out, eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
            epsilon=epsilon,
        )
        cost += grid_import * buy
        if grid_export > epsilon:
            cost += grid_export * max(0.0, buy - export_credit)
    return cost
