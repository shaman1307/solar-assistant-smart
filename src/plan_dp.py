"""Horizon DP: charge/idle only. Battery export is assigned after DP."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .g12_pricing import get_g12_zone
from .grid_config import export_window_start_hour, grid_export_threshold_pln_kwh
from .plan_charge import grid_charge_ac_kw
from .plan_export import (
    export_credit_price,
    g12_tariff_from_cfg,
    max_battery_export_kwh,
)
from .plan_physics import (
    HourControl,
    eps_step_kwh,
    simulate_hour,
    slots_per_hour_from_scale,
)
from .plan_reserve import (
    build_extended_buy_for_reserve,
    build_extended_pv_load_for_reserve,
    grid_charge_target_soc_kwh_from_step,
    reserve_soc_kwh_from_step,
)
from .plan_spill import build_tail_hour_arrays, tail_balance_cost_pln
from .simulation_config import (
    plan_min_soc_kwh,
    plan_reserve_min_soc_kwh,
    plan_timer_charge_grid_kw,
    plan_timer_discharge_power_kw,
    plan_timer_min_block_minutes,
    plan_timer_min_hourly_transfer_kwh,
)

DP_SOC_BIN_KWH = 0.5
DP_COST_INF = 1e15
EXPORT_POWER_FRACS = (1.0, 0.5, 0.25)
EXPORT_MIN_FRAC = 0.25
CONTROL_DEDUP_DECIMALS = 3


@dataclass
class HorizonDpResult:
    """DP path plus scalars the charge/export passes need."""

    controls: list[HourControl]
    reserves: list[float]
    charge_targets: list[float]
    offpeak_buy: float
    battery_cap: float
    min_kwh: float
    charge_ac_step: float
    discharge_dc_step: float
    inverter_ac_step: float
    eta_grid: float
    eta_out: float
    eta_pv_load: float
    eta_pv_grid: float
    eta_pv_battery: float
    eps_step: float
    min_hourly_transfer: float
    export_floor: float
    window_start: int
    min_block_minutes: int
    skip_post: bool = False


def dp_step_clock(
    today_date,
    step: int,
    *,
    rce_step_offset: int,
    slots_per_hour: int,
) -> datetime:
    """Warsaw clock at the start of a DP step hour bucket."""
    if isinstance(today_date, datetime):
        base = today_date.date()
    elif isinstance(today_date, date):
        base = today_date
    else:
        base = datetime.strptime(str(today_date)[:10], "%Y-%m-%d").date()
    sph = max(1, int(slots_per_hour))
    hour_index = (int(rce_step_offset) + int(step)) // sph
    day_offset, hour = divmod(int(hour_index), 24)
    return datetime.combine(base, datetime.min.time()) + timedelta(
        days=day_offset, hours=hour,
    )


def _soc_bin(soc_kwh: float, min_kwh: float, bin_kwh: float) -> int:
    return max(0, int(round((soc_kwh - min_kwh) / bin_kwh)))


def _soc_from_bin(idx: int, min_kwh: float, bin_kwh: float) -> float:
    return min_kwh + idx * bin_kwh

def control_options(
    soc_kwh: float,
    pv: float,
    load: float,
    *,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_cap_kwh: float,
    inverter_ac_cap_kw: float,
    charge_ac_cap_kw: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    epsilon: float,
    buy_p: float,
    offpeak_buy: float,
    reserve_soc_kwh: float,
    charge_target_soc_kwh: float,
    allow_battery_export: bool,
) -> list[HourControl]:
    """Build DP actions for one step.

    - reserve_soc_kwh: export floor (self-use through overnight / morning).
    - charge_target_soc_kwh: floor + house deficits until PV covers (peak days).
    When charge is allowed, options collapse to charge-only (continuous offpeak fill).
    """
    head_room = battery_cap - soc_kwh
    charge_rate = grid_charge_ac_kw(
        soc_kwh,
        buy_p=buy_p,
        offpeak_buy=offpeak_buy,
        charge_target_soc_kwh=charge_target_soc_kwh,
        head_room_kwh=head_room,
        charge_ac_cap_kw=charge_ac_cap_kw,
        eta_grid=eta_grid,
        epsilon=epsilon,
    )
    if charge_rate > epsilon:
        return [HourControl(charge_rate, 0.0)]

    opts = [HourControl(0.0, 0.0)]

    max_batt_export = max_battery_export_kwh(
        soc_kwh, pv, load,
        min_kwh=min_kwh, ac_cap_kw=inverter_ac_cap_kw,
        eta_out=eta_out, eta_pv_load=eta_pv_load,
        reserve_soc_kwh=reserve_soc_kwh,
        epsilon=epsilon,
        discharge_dc_cap_kwh=discharge_dc_cap_kwh,
    )
    # Tier caps are AC export from battery at the DC power ceiling.
    export_ac_cap = (
        discharge_dc_cap_kwh * eta_out if eta_out > 0 else discharge_dc_cap_kwh
    )
    min_viable = max(epsilon, export_ac_cap * EXPORT_MIN_FRAC)
    if max_batt_export >= min_viable and allow_battery_export:
        for frac in EXPORT_POWER_FRACS:
            tier_cap = export_ac_cap * frac
            tier_export = min(max_batt_export, tier_cap)
            if tier_export >= min_viable:
                opts.append(HourControl(0.0, tier_export))

    seen: set[tuple[float, float]] = set()
    out: list[HourControl] = []
    for o in opts:
        key = (
            round(o.grid_charge_kw, CONTROL_DEDUP_DECIMALS),
            round(o.battery_export_kwh, CONTROL_DEDUP_DECIMALS),
        )
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out

def tail_start_hour(
    *,
    steps: int,
    rce_step_offset: int,
    step_scale: float,
    end_dt: datetime,
) -> int:
    """First calendar hour after the last optimized step (no double-count with DP)."""
    if steps <= 0:
        return end_dt.hour
    slots_per_hour = slots_per_hour_from_scale(step_scale)
    last_global = rce_step_offset + steps - 1
    return (last_global // slots_per_hour) + 1

def run_horizon_dp(
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    buy_prices: list[float],
    rce_series: list[float | None],
    initial_soc_kwh: float,
    cfg: dict,
    params: dict[str, float | int],
    end_dt: datetime,
    today_date,
    rce_map: dict[tuple[str, int], float | None],
    forecast: dict[str, Any] | None = None,
    step_scale: float = 1.0,
    rce_step_offset: int = 0,
    front_load_skip_leading_slots: int | None = None,
    skip_export_hours: set[int] | None = None,
) -> HorizonDpResult:
    from .plan_cost import hour_grid_cash_pln

    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    reserve_floor_kwh = plan_reserve_min_soc_kwh(cfg)
    # Timer Dis is DC kW; inverter bus is separate AC headroom for export.
    discharge_dc_kw = plan_timer_discharge_power_kw(cfg)
    inverter_ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    # Timer/optimizer charge cap as AC on the meter (DC into bat = AC × eta).
    charge_ac_kw = plan_timer_charge_grid_kw(cfg)
    min_hourly_transfer = plan_timer_min_hourly_transfer_kwh(cfg)
    epsilon = float(params["epsilon_kwh"])
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])
    tariff = g12_tariff_from_cfg(cfg)
    discharge_dc_step = discharge_dc_kw * step_scale
    inverter_ac_step = inverter_ac_kw * step_scale
    charge_ac_step = charge_ac_kw * step_scale
    eps_step = eps_step_kwh(epsilon, step_scale)

    bin_kwh = max(DP_SOC_BIN_KWH, battery_cap / max(1, int((battery_cap - min_kwh) / DP_SOC_BIN_KWH)))
    max_bin = int(math.ceil((battery_cap - min_kwh) / bin_kwh))
    inf = DP_COST_INF

    dp: list[dict[int, float]] = [{} for _ in range(steps + 1)]
    soc_at: list[dict[int, float]] = [{} for _ in range(steps + 1)]
    back: dict[tuple[int, int], tuple[int, HourControl]] = {}

    s0 = _soc_bin(initial_soc_kwh, min_kwh, bin_kwh)
    dp[0][s0] = 0.0
    soc_at[0][s0] = min(battery_cap, max(min_kwh, initial_soc_kwh))
    forecast_data = forecast or {
        "today": {"pv": [], "load": [], "pv_total": 0.0, "load_total": 0.0},
        "tomorrow": {"pv": [], "load": [], "pv_total": 0.0, "load_total": 0.0},
    }

    def _pv_export_credit(rce: float | None, *, from_battery: bool) -> float:
        return export_credit_price(rce, tariff, from_battery=from_battery, cfg=cfg)

    tail_start = tail_start_hour(
        steps=steps, rce_step_offset=rce_step_offset,
        step_scale=step_scale, end_dt=end_dt,
    )
    tail_pv, tail_load, tail_buy, tail_export_credit = build_tail_hour_arrays(
        end_dt, today_date, forecast_data, cfg, rce_map, _pv_export_credit,
        tail_start_hour=tail_start,
    )

    # Reserve / charge-target walks use forecast through end of tomorrow when the
    # rolling plan window is shorter (e.g. ends at tomorrow H04). Optimized DP
    # steps stay within the plan window only.
    pv_for_reserve, load_for_reserve = build_extended_pv_load_for_reserve(
        pv_series, load_series,
        step_scale=step_scale, end_dt=end_dt, today_date=today_date, forecast=forecast_data,
        global_step_offset=rce_step_offset,
    )
    buy_for_reserve = build_extended_buy_for_reserve(
        buy_prices,
        step_scale=step_scale, end_dt=end_dt, today_date=today_date,
        forecast=forecast_data, cfg=cfg,
        global_step_offset=rce_step_offset,
    )
    slots_per_hour = slots_per_hour_from_scale(step_scale)

    reserves = [
        reserve_soc_kwh_from_step(
            s, pv_for_reserve, load_for_reserve, reserve_floor_kwh,
            eta_out, eta_pv_load, eps_step,
            buy_series=buy_for_reserve,
            offpeak_buy=tariff.offpeak_full,
            slots_per_hour=slots_per_hour,
            global_step_offset=rce_step_offset,
            cfg=cfg,
            today_date=today_date,
        )
        for s in range(steps)
    ]
    offpeak_buy = tariff.offpeak_full
    charge_targets = [
        grid_charge_target_soc_kwh_from_step(
            s, pv_for_reserve, load_for_reserve, buy_for_reserve,
            reserve_floor_kwh, eta_out, eta_pv_load, eps_step, offpeak_buy,
            slots_per_hour=slots_per_hour,
            global_step_offset=rce_step_offset,
            cfg=cfg,
            today_date=today_date,
        )
        for s in range(steps)
    ]

    export_floor = grid_export_threshold_pln_kwh(cfg)
    window_start = export_window_start_hour(cfg)

    for step in range(steps):
        pv = pv_series[step]
        load = load_series[step]
        buy_p = buy_prices[step]
        rce_idx = rce_step_offset + step
        rce = rce_series[rce_idx] if rce_idx < len(rce_series) else None
        # Charge/idle only in DP; battery export is assigned by hourly RCE rank after.
        allow_battery_export = False
        clock = dp_step_clock(
            today_date, step,
            rce_step_offset=rce_step_offset, slots_per_hour=slots_per_hour,
        )
        g12_zone = get_g12_zone(clock, cfg)

        for soc_bin, cost_in in list(dp[step].items()):
            soc = soc_at[step].get(
                soc_bin,
                min(battery_cap, _soc_from_bin(soc_bin, min_kwh, bin_kwh)),
            )
            reserve = reserves[step]
            charge_target = charge_targets[step]
            for ctrl in control_options(
                soc, pv, load,
                battery_cap=battery_cap, min_kwh=min_kwh,
                discharge_dc_cap_kwh=discharge_dc_step,
                inverter_ac_cap_kw=inverter_ac_step,
                charge_ac_cap_kw=charge_ac_step,
                eta_grid=eta_grid,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                epsilon=eps_step, buy_p=buy_p, offpeak_buy=offpeak_buy,
                reserve_soc_kwh=reserve,
                charge_target_soc_kwh=charge_target,
                allow_battery_export=allow_battery_export,
            ):
                phys = simulate_hour(
                    soc, pv, load, ctrl,
                    battery_cap=battery_cap, min_kwh=min_kwh,
                    ac_cap_kw=inverter_ac_step,
                    discharge_dc_cap_kwh=discharge_dc_step,
                    eta_grid=eta_grid, eta_out=eta_out,
                    eta_pv_load=eta_pv_load,
                    eta_pv_grid=eta_pv_grid,
                    eta_pv_battery=eta_pv_battery,
                    epsilon=eps_step,
                    reserve_soc_kwh=reserve,
                )
                step_cost = hour_grid_cash_pln(
                    phys.grid_import, phys.grid_export, buy_p, rce, cfg,
                    battery_export=min(ctrl.battery_export_kwh, phys.grid_export),
                    g12_zone=g12_zone,
                )["cost"]
                nb = min(max_bin, _soc_bin(phys.soc_end, min_kwh, bin_kwh))
                total = cost_in + step_cost
                if total < dp[step + 1].get(nb, inf):
                    dp[step + 1][nb] = total
                    back[(step + 1, nb)] = (soc_bin, ctrl)
                    soc_at[step + 1][nb] = phys.soc_end

    if not dp[steps]:
        return HorizonDpResult(
            controls=[HourControl(0.0, 0.0) for _ in range(steps)],
            reserves=reserves,
            charge_targets=charge_targets,
            offpeak_buy=offpeak_buy,
            battery_cap=battery_cap,
            min_kwh=min_kwh,
            charge_ac_step=charge_ac_step,
            discharge_dc_step=discharge_dc_step,
            inverter_ac_step=inverter_ac_step,
            eta_grid=eta_grid,
            eta_out=eta_out,
            eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery,
            eps_step=eps_step,
            min_hourly_transfer=min_hourly_transfer,
            export_floor=export_floor,
            window_start=window_start,
            min_block_minutes=plan_timer_min_block_minutes(cfg),
            skip_post=True,
        )

    def _total_cost(path_cost: float, soc_bin: int) -> float:
        soc_end = soc_at[steps].get(
            soc_bin,
            min(battery_cap, _soc_from_bin(soc_bin, min_kwh, bin_kwh)),
        )
        tail = tail_balance_cost_pln(
            soc_end, tail_pv, tail_load, tail_buy, tail_export_credit,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac_kw,
            eta_out=eta_out, eta_pv_load=eta_pv_load,
            eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
            epsilon=epsilon,
        )
        return path_cost + tail

    best_bin = min(dp[steps], key=lambda b: _total_cost(dp[steps][b], b))

    controls: list[HourControl] = []
    b = best_bin
    for step in range(steps, 0, -1):
        soc_bin, ctrl = back.get((step, b), (0, HourControl(0.0, 0.0)))
        controls.append(ctrl)
        b = soc_bin
    controls.reverse()

    return HorizonDpResult(
        controls=controls,
        reserves=reserves,
        charge_targets=charge_targets,
        offpeak_buy=offpeak_buy,
        battery_cap=battery_cap,
        min_kwh=min_kwh,
        charge_ac_step=charge_ac_step,
        discharge_dc_step=discharge_dc_step,
        inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid,
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid,
        eta_pv_battery=eta_pv_battery,
        eps_step=eps_step,
        min_hourly_transfer=min_hourly_transfer,
        export_floor=export_floor,
        window_start=window_start,
        min_block_minutes=plan_timer_min_block_minutes(cfg),
        skip_post=False,
    )

