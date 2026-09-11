"""DP optimize_horizon: brute-force oracle (same bins) + output invariants."""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from src.g12_pricing import get_g12_zone
from src.grid_config import merge_grid_defaults
from src.plan_cost import hour_grid_cash_pln
from src.plan_dp import dp_step_clock
from src.plan_optimizer import (
    DP_COST_INF,
    HourControl,
    control_options,
    grid_charge_target_soc_kwh_from_step,
    reserve_soc_kwh_from_step,
    battery_export_step_allowed,
    g12_tariff_from_cfg,
    optimize_horizon,
    simulate_hour,
)
from src.plan_spill import build_tail_hour_arrays, tail_balance_cost_pln
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
    plan_min_soc_kwh,
    plan_reserve_min_soc_kwh,
    plan_timer_charge_grid_kw,
    plan_timer_discharge_power_kw,
)


def _cfg(**timer_kw) -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 20.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 20,
            "epsilon_kwh": 0.01,
            "losses_pct": {
                "grid_to_battery": 0.0,
                "battery_to_load_or_grid": 0.0,
                "pv_to_grid": 0.0,
                "pv_to_load": 0.0,
                "pv_to_battery": 0.0,
            },
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.5,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.3,
            },
        },
        "timer_schedule": {
            "min_block_minutes": 15,
            "min_hourly_transfer_kwh": 0.0,
            **timer_kw,
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def _empty_forecast() -> dict:
    z = [0.0] * 24
    day = {"pv": list(z), "load": list(z), "pv_total": 0.0, "load_total": 0.0}
    return {"today": day, "tomorrow": dict(day)}


def _path_cost_and_socs(
    controls: list[HourControl],
    *,
    pv_series: list[float],
    load_series: list[float],
    buy_prices: list[float],
    rce_series: list[float | None],
    initial_soc_kwh: float,
    cfg: dict,
    params: dict,
    end_dt: datetime,
    today_date,
    forecast: dict,
    step_scale: float,
    rce_step_offset: int,
) -> tuple[float, list[float]]:
    """Replay controls with the same cash + tail model as optimize_horizon."""
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    reserve_floor = plan_reserve_min_soc_kwh(cfg)
    discharge_dc = plan_timer_discharge_power_kw(cfg) * step_scale
    inverter_ac = float(cfg['inverter']['ac_capacity_kw']) * step_scale
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])
    epsilon = float(params["epsilon_kwh"])
    eps_step = max(epsilon * step_scale, 0.001)
    tariff = g12_tariff_from_cfg(cfg)
    offpeak = tariff.offpeak_full
    steps = len(controls)
    slots = max(1, int(round(1.0 / step_scale)))

    reserves = [
        reserve_soc_kwh_from_step(
            s, pv_series, load_series, reserve_floor, eta_out, eta_pv_load, eps_step,
            buy_series=buy_prices, offpeak_buy=offpeak,
            slots_per_hour=slots,
            global_step_offset=rce_step_offset,
        )
        for s in range(steps)
    ]

    def _pv_export_credit(rce, *, from_battery: bool):
        from src.plan_optimizer import export_credit_price
        return export_credit_price(rce, tariff, from_battery=from_battery, cfg=cfg)

    from src.plan_optimizer import tail_start_hour
    tail_start = tail_start_hour(
        steps=steps, rce_step_offset=rce_step_offset,
        step_scale=step_scale, end_dt=end_dt,
    )
    tail_pv, tail_load, tail_buy, tail_export_credit = build_tail_hour_arrays(
        end_dt, today_date, forecast, cfg, {}, _pv_export_credit,
        tail_start_hour=tail_start,
    )

    soc = initial_soc_kwh
    cost = 0.0
    socs = [soc]
    for step, ctrl in enumerate(controls):
        pv = pv_series[step]
        load = load_series[step]
        buy_p = buy_prices[step]
        rce_idx = rce_step_offset + step
        rce = rce_series[rce_idx] if rce_idx < len(rce_series) else None
        g12_zone = get_g12_zone(
            dp_step_clock(
                today_date, step,
                rce_step_offset=rce_step_offset, slots_per_hour=slots,
            ),
            cfg,
        )
        phys = simulate_hour(
            soc, pv, load, ctrl,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac,
                discharge_dc_cap_kwh=discharge_dc,
            eta_grid=eta_grid, eta_out=eta_out,
            eta_pv_load=eta_pv_load, eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery, epsilon=eps_step,
            reserve_soc_kwh=reserves[step],
        )
        cost += hour_grid_cash_pln(
            phys.grid_import, phys.grid_export, buy_p, rce, cfg,
            battery_export=min(ctrl.battery_export_kwh, phys.grid_export),
            g12_zone=g12_zone,
        )["cost"]
        soc = phys.soc_end
        socs.append(soc)

    cost += tail_balance_cost_pln(
        soc, tail_pv, tail_load, tail_buy, tail_export_credit,
        battery_cap=battery_cap, min_kwh=min_kwh,
        ac_cap_kw=float(cfg["inverter"]["ac_capacity_kw"]),
        eta_out=eta_out, eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
        epsilon=epsilon,
    )
    return float(cost), socs


def _brute_best_cost(
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    buy_prices: list[float],
    rce_series: list[float | None],
    initial_soc_kwh: float,
    cfg: dict,
    params: dict,
    end_dt: datetime,
    today_date,
    forecast: dict,
    step_scale: float,
    rce_step_offset: int,
) -> float:
    """Enumerate same action set as DP; snap SOC to the same bins."""
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    reserve_floor = plan_reserve_min_soc_kwh(cfg)
    discharge_dc = plan_timer_discharge_power_kw(cfg) * step_scale
    inverter_ac = float(cfg['inverter']['ac_capacity_kw']) * step_scale
    charge_ac = plan_timer_charge_grid_kw(cfg) * step_scale
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])
    epsilon = float(params["epsilon_kwh"])
    eps_step = max(epsilon * step_scale, 0.001)
    tariff = g12_tariff_from_cfg(cfg)
    offpeak = tariff.offpeak_full
    export_floor = float(cfg["grid"]["grid_export_threshold_pln_kwh"])
    slots = max(1, int(round(1.0 / step_scale)))

    reserves = [
        reserve_soc_kwh_from_step(
            s, pv_series, load_series, reserve_floor, eta_out, eta_pv_load, eps_step,
            buy_series=buy_prices, offpeak_buy=offpeak,
            slots_per_hour=slots, global_step_offset=rce_step_offset,
        )
        for s in range(steps)
    ]
    charge_targets = [
        grid_charge_target_soc_kwh_from_step(
            s, pv_series, load_series, buy_prices, reserve_floor,
            eta_out, eta_pv_load, eps_step, offpeak,
            slots_per_hour=slots, global_step_offset=rce_step_offset,
        )
        for s in range(steps)
    ]

    from src.plan_optimizer import export_credit_price, tail_start_hour

    def _pv_export_credit(rce, *, from_battery: bool):
        return export_credit_price(rce, tariff, from_battery=from_battery, cfg=cfg)

    tail_start = tail_start_hour(
        steps=steps, rce_step_offset=rce_step_offset,
        step_scale=step_scale, end_dt=end_dt,
    )
    tail_pv, tail_load, tail_buy, tail_export_credit = build_tail_hour_arrays(
        end_dt, today_date, forecast, cfg, {}, _pv_export_credit,
        tail_start_hour=tail_start,
    )

    best = DP_COST_INF

    def dfs(step: int, soc: float, cost_so_far: float) -> None:
        nonlocal best
        if cost_so_far >= best:
            return
        if step == steps:
            tail = tail_balance_cost_pln(
                soc, tail_pv, tail_load, tail_buy, tail_export_credit,
                battery_cap=battery_cap, min_kwh=min_kwh,
                ac_cap_kw=float(cfg['inverter']['ac_capacity_kw']),
                eta_out=eta_out, eta_pv_load=eta_pv_load,
                eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
                epsilon=epsilon,
            )
            best = min(best, cost_so_far + tail)
            return

        pv = pv_series[step]
        load = load_series[step]
        buy_p = buy_prices[step]
        rce_idx = rce_step_offset + step
        rce = rce_series[rce_idx] if rce_idx < len(rce_series) else None
        allow = False  # Match optimize_horizon: export is ranked post-pass, not in DP.
        g12_zone = get_g12_zone(
            dp_step_clock(
                today_date, step,
                rce_step_offset=rce_step_offset, slots_per_hour=slots,
            ),
            cfg,
        )
        for ctrl in control_options(
            soc, pv, load,
            battery_cap=battery_cap, min_kwh=min_kwh,
            discharge_dc_cap_kwh=discharge_dc,
            inverter_ac_cap_kw=inverter_ac,
            charge_ac_cap_kw=charge_ac,
            eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
            epsilon=eps_step, buy_p=buy_p, offpeak_buy=offpeak,
            reserve_soc_kwh=reserves[step],
            charge_target_soc_kwh=charge_targets[step],
            allow_battery_export=allow,
        ):
            phys = simulate_hour(
                soc, pv, load, ctrl,
                battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac,
                discharge_dc_cap_kwh=discharge_dc,
                eta_grid=eta_grid, eta_out=eta_out,
                eta_pv_load=eta_pv_load, eta_pv_grid=eta_pv_grid,
                eta_pv_battery=eta_pv_battery, epsilon=eps_step,
                reserve_soc_kwh=reserves[step],
            )
            step_cost = hour_grid_cash_pln(
                phys.grid_import, phys.grid_export, buy_p, rce, cfg,
                battery_export=min(ctrl.battery_export_kwh, phys.grid_export),
                g12_zone=g12_zone,
            )["cost"]
            # Match DP: keep continuous phys.soc_end (same as soc_at in optimize_horizon).
            dfs(step + 1, phys.soc_end, cost_so_far + step_cost)

    dfs(0, min(battery_cap, max(min_kwh, initial_soc_kwh)), 0.0)
    assert best < DP_COST_INF
    return best


def _run_dp(
    *,
    steps: int,
    pv: list[float],
    load: list[float],
    buy: list[float],
    rce: list[float | None],
    soc0: float,
    cfg: dict,
    end_dt: datetime,
    step_scale: float = 1.0,
    rce_step_offset: int = 0,
) -> list[HourControl]:
    params = get_simulation_params(cfg)
    forecast = _empty_forecast()
    return optimize_horizon(
        steps=steps,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=soc0,
        cfg=cfg,
        params=params,
        end_dt=end_dt,
        today_date=end_dt.date(),
        rce_map={},
        forecast=forecast,
        step_scale=step_scale,
        rce_step_offset=rce_step_offset,
    )


def test_dp_matches_brute_force_on_short_horizon():
    """Charge-only DP path cost equals full enumeration (export is a post-pass)."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    end = datetime(2026, 7, 16, 10, 0)
    steps = 4
    pv = [0.0] * steps
    load = [0.4] * steps
    buy = [0.5] * steps
    # Below export floor so ranked export does not change the charge path.
    rce = [0.4, 0.4, 0.4, 0.4, 0.4]
    offset = 1
    forecast = _empty_forecast()

    controls = _run_dp(
        steps=steps, pv=pv, load=load, buy=buy, rce=rce, soc0=12.0,
        cfg=cfg, end_dt=end, step_scale=1.0, rce_step_offset=offset,
    )
    assert all(c.battery_export_kwh == 0.0 for c in controls)
    dp_cost, _ = _path_cost_and_socs(
        controls, pv_series=pv, load_series=load, buy_prices=buy, rce_series=rce,
        initial_soc_kwh=12.0, cfg=cfg, params=params, end_dt=end,
        today_date=end.date(), forecast=forecast, step_scale=1.0,
        rce_step_offset=offset,
    )
    brute = _brute_best_cost(
        steps=steps, pv_series=pv, load_series=load, buy_prices=buy, rce_series=rce,
        initial_soc_kwh=12.0, cfg=cfg, params=params, end_dt=end,
        today_date=end.date(), forecast=forecast, step_scale=1.0,
        rce_step_offset=offset,
    )
    assert dp_cost == pytest.approx(brute, abs=1e-4)


def test_dp_oracle_high_rce_prefers_export_when_soc_ample():
    cfg = _cfg()
    end = datetime(2026, 7, 16, 14, 0)
    steps = 3
    controls = _run_dp(
        steps=steps,
        pv=[0.0] * steps,
        load=[0.2] * steps,
        buy=[0.5] * steps,
        rce=[0.9] * (steps + 1),
        soc0=18.0,
        cfg=cfg,
        end_dt=end,
        step_scale=1.0,
        rce_step_offset=1,
    )
    assert any(c.battery_export_kwh > 0.05 for c in controls)


def test_dp_oracle_low_rce_forbids_battery_export():
    cfg = _cfg()
    end = datetime(2026, 7, 16, 14, 0)
    steps = 4
    controls = _run_dp(
        steps=steps,
        pv=[0.0] * steps,
        load=[0.3] * steps,
        buy=[0.5] * steps,
        rce=[0.3] * steps,
        soc0=18.0,
        cfg=cfg,
        end_dt=end,
        step_scale=1.0,
        rce_step_offset=0,
    )
    assert all(c.battery_export_kwh == 0.0 for c in controls)


def test_dp_oracle_reserve_blocks_export_despite_high_rce():
    """Overnight need raises reserve above SOC headroom → no battery export."""
    cfg = _cfg()
    end = datetime(2026, 7, 16, 20, 0)
    steps = 2
    # Series longer than steps so reserve walk sees the dark night ahead.
    pv = [0.0] * 12
    load = [1.0] * 12
    buy = [0.5] * 12
    rce = [0.9] * 13
    controls = optimize_horizon(
        steps=steps,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=6.0,  # min≈4; reserve through night >> headroom
        cfg=cfg,
        params=get_simulation_params(cfg),
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast=_empty_forecast(),
        step_scale=1.0,
        rce_step_offset=1,
    )
    assert all(c.battery_export_kwh == 0.0 for c in controls)


def test_dp_no_export_when_overnight_path_hits_min_soc():
    """High evening RCE must not dump battery if night load drives SOC to min."""
    cfg = _cfg()
    # Cap 20 kWh, min 20% = 4 kWh. Start ~7 kWh; 8h × 0.5 load ≈ 4 kWh need → at min.
    end = datetime(2026, 7, 18, 20, 0)  # Saturday evening
    steps = 3
    n = 12
    pv = [0.0] * n
    load = [0.5] * n
    buy = [0.5] * n  # weekend offpeak
    rce = [0.9] * (n + 1)
    controls = optimize_horizon(
        steps=steps,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=7.0,
        cfg=cfg,
        params=get_simulation_params(cfg),
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast=_empty_forecast(),
        step_scale=1.0,
        rce_step_offset=1,
    )
    assert all(c.battery_export_kwh == 0.0 for c in controls)


def test_dp_invariants_soc_bounds_and_export_rule():
    cfg = _cfg()
    params = get_simulation_params(cfg)
    end = datetime(2026, 7, 16, 10, 0)
    steps = 4
    pv = [0.0, 0.5, 0.0, 0.0]
    load = [0.5, 0.4, 0.5, 0.5]
    buy = [0.5, 0.5, 1.0, 0.5]
    rce = [0.9, 0.9, 0.9, 0.9, 0.9]
    offset = 1
    soc0 = 10.0
    controls = _run_dp(
        steps=steps, pv=pv, load=load, buy=buy, rce=rce, soc0=soc0,
        cfg=cfg, end_dt=end, step_scale=1.0, rce_step_offset=offset,
    )
    cost, socs = _path_cost_and_socs(
        controls, pv_series=pv, load_series=load, buy_prices=buy, rce_series=rce,
        initial_soc_kwh=soc0, cfg=cfg, params=params, end_dt=end,
        today_date=end.date(), forecast=_empty_forecast(), step_scale=1.0,
        rce_step_offset=offset,
    )
    min_kwh = plan_min_soc_kwh(cfg)
    cap = float(cfg["battery"]["capacity_kwh"])
    assert math.isfinite(cost)
    for s in socs:
        assert min_kwh - 1e-6 <= s <= cap + 1e-6
    export_floor = float(cfg["grid"]["grid_export_threshold_pln_kwh"])
    from src.plan_optimizer import hourly_avg_rce, slots_per_hour_from_scale
    slots = slots_per_hour_from_scale(1.0)
    for i, c in enumerate(controls):
        if c.battery_export_kwh > 1e-6:
            hour = (offset + i) // slots
            avg = hourly_avg_rce(rce, hour, slots_per_hour=slots)
            assert avg is not None and avg + 1e-9 >= export_floor


def test_dp_invariants_no_grid_charge_on_peak_buy():
    cfg = _cfg()
    end = datetime(2026, 7, 16, 10, 0)
    steps = 3
    # Peak buy everywhere, SOC below a would-be target — still no charge.
    controls = _run_dp(
        steps=steps,
        pv=[0.0] * steps,
        load=[0.5] * steps,
        buy=[1.0] * steps,
        rce=[0.4] * steps,
        soc0=5.0,
        cfg=cfg,
        end_dt=end,
        step_scale=1.0,
        rce_step_offset=0,
    )
    assert all(c.grid_charge_kw == 0.0 for c in controls)


def test_q15_export_requires_hourly_avg_above_floor():
    """Hour avg RCE below threshold → no ranked export (even if mid q15 are high)."""
    cfg = _cfg()
    cfg["grid"]["grid_export_threshold_pln_kwh"] = 0.62
    end = datetime(2026, 7, 16, 18, 0)
    rce = [None] * (18 * 4) + [0.50, 0.70, 0.70, 0.50]  # avg 0.60
    steps = 4
    offset = 18 * 4
    controls = _run_dp(
        steps=steps,
        pv=[0.0] * steps,
        load=[0.2] * steps,
        buy=[0.5] * steps,
        rce=rce,
        soc0=18.0,
        cfg=cfg,
        end_dt=end,
        step_scale=0.25,
        rce_step_offset=offset,
    )
    assert all(c.battery_export_kwh == 0.0 for c in controls)
