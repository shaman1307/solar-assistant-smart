"""Grid charge sits in the last offpeak hours before the morning peak."""

from __future__ import annotations

from datetime import datetime

from src.grid_config import merge_grid_defaults
from src.plan_optimizer import optimize_horizon
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
    plan_min_soc_kwh,
)


OFF = 0.5
PEAK = 1.2


def _cfg() -> dict:
    cfg = {
        "battery": {
            "capacity_kwh": 32.0,
            "min_soc_pct": 10,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "horizon_hours": 24,
            "epsilon_kwh": 0.01,
            "losses_pct": {
                "grid_to_battery": 0.0,
                "battery_to_load_or_grid": 0.0,
                "pv_to_grid": 0.0,
                "pv_to_load": 0.0,
                "pv_to_battery": 0.0,
            },
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.5,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": PEAK,
                "offpeak_price_pln_kwh": OFF,
                "peak_energy_only_pln_kwh": 0.9,
                "offpeak_energy_only_pln_kwh": 0.4,
                "peak_hours": [[7, 13], [16, 22]],
            },
        },
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 0.5},
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def test_grid_charge_sits_in_last_offpeak_hour_before_peak():
    """Charge the last offpeak hour before 06:00; skip the current hour."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    # 00-05 offpeak load, 06-08 peak load needing battery cover; PV covers from 09.
    pv = [0.0] * 9 + [2.0] * 6 + [0.0] * 9
    load = [0.3] * 6 + [1.5] * 3 + [0.3] * 15
    buy = [OFF] * 6 + [PEAK] * 7 + [OFF] * 2 + [PEAK] * 6 + [OFF] * 3
    rce = [0.2] * 96
    # Start near min so morning peak clearly needs grid→battery top-up.
    initial = plan_min_soc_kwh(cfg) + 0.5
    end = datetime(2026, 7, 21, 23, 0)

    controls = optimize_horizon(
        steps=24,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=initial,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": pv, "load": load, "pv_total": sum(pv), "load_total": sum(load)},
            "tomorrow": {
                "pv": pv,
                "load": load,
                "pv_total": sum(pv),
                "load_total": sum(load),
            },
        },
        step_scale=1.0,
        rce_step_offset=0,
    )

    charged_hours = [h for h, c in enumerate(controls) if c.grid_charge_kw > 0.05]
    assert charged_hours, "expected some offpeak grid charge"
    assert controls[0].grid_charge_kw < 0.05, "current hour must not start Chg"
    early = [h for h in charged_hours if h < 6]
    assert early, f"expected pre-peak charge, got {charged_hours}"
    assert early == list(range(early[0], early[-1] + 1)), early
    assert early[-1] == 5, f"charge should end in the last offpeak hour (5), got {early}"
    assert early[0] >= 4, f"charge should sit close to peak, got {early}"


def test_no_overnight_charge_when_already_above_cover_target():
    """If SOC already covers floor + peak deficits, no grid→battery overnight."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    pv = [0.0] * 9 + [2.0] * 6 + [0.0] * 9
    load = [0.3] * 6 + [1.2] * 3 + [0.3] * 6 + [1.5] * 6 + [0.3] * 3
    buy = [OFF] * 6 + [PEAK] * 7 + [OFF] * 2 + [PEAK] * 6 + [OFF] * 3
    rce = [0.2] * 96
    initial = float(cfg["battery"]["capacity_kwh"]) * 0.45
    end = datetime(2026, 7, 21, 23, 0)

    controls = optimize_horizon(
        steps=24,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=initial,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": pv, "load": load, "pv_total": sum(pv), "load_total": sum(load)},
            "tomorrow": {
                "pv": pv,
                "load": load,
                "pv_total": sum(pv),
                "load_total": sum(load),
            },
        },
        step_scale=1.0,
        rce_step_offset=0,
    )

    overnight_chg = sum(c.grid_charge_kw for c in controls[:6])
    assert overnight_chg < 0.5, f"overnight grid charge should be ~0, got {overnight_chg}"


def test_peak_hours_not_fed_from_grid_when_peak_cover_held():
    """Below peak-cover target: charge in the last offpeak hours before peak."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    # Small peak deficit, large overnight offpeak load.
    pv = [0.0] * 9 + [3.0] * 5 + [0.0] * 10
    load = [0.8] * 6 + [0.4] * 3 + [0.3] * 15
    buy = [OFF] * 6 + [PEAK] * 7 + [OFF] * 11
    rce = [0.2] * 96
    initial = plan_min_soc_kwh(cfg) + 1.0
    end = datetime(2026, 7, 21, 23, 0)

    controls = optimize_horizon(
        steps=24,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=initial,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": pv, "load": load, "pv_total": sum(pv), "load_total": sum(load)},
            "tomorrow": {
                "pv": pv,
                "load": load,
                "pv_total": sum(pv),
                "load_total": sum(load),
            },
        },
        step_scale=1.0,
        rce_step_offset=0,
    )

    assert controls[0].grid_charge_kw < 0.05
    charged = [h for h in range(6) if controls[h].grid_charge_kw > 0.05]
    if charged:
        assert charged == list(range(charged[0], charged[-1] + 1)), charged
        assert charged[-1] == 5
        assert charged[0] >= 4


def test_above_peak_target_moves_dp_charge_before_peak():
    """Relocate DP pre-peak Chg into the last offpeak hour before peak."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    # Heavy overnight load drains hard; top-up must land just before 06:00.
    pv = [0.0] * 9 + [2.0] * 6 + [0.0] * 9
    load = [0.9] * 6 + [0.5] * 3 + [0.3] * 15
    buy = [OFF] * 6 + [PEAK] * 7 + [OFF] * 11
    rce = [0.2] * 96
    # Above typical peak-only target at start.
    initial = plan_min_soc_kwh(cfg) + 4.0
    end = datetime(2026, 7, 21, 23, 0)

    controls = optimize_horizon(
        steps=24,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=initial,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": pv, "load": load, "pv_total": sum(pv), "load_total": sum(load)},
            "tomorrow": {
                "pv": pv,
                "load": load,
                "pv_total": sum(pv),
                "load_total": sum(load),
            },
        },
        step_scale=1.0,
        rce_step_offset=0,
    )

    charged = [h for h in range(6) if controls[h].grid_charge_kw > 0.05]
    assert controls[0].grid_charge_kw < 0.05
    if charged:
        assert charged == list(range(charged[0], charged[-1] + 1)), charged
        assert charged[-1] == 5, f"charge should end before peak in hour 5, got {charged}"
        assert charged[0] >= 4, f"charge should sit close to peak, got {charged}"
        assert all(not controls[h].load_from_grid for h in charged)
