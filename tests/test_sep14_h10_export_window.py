"""2026-09-14 replay: inverter actuals H00–H09, plan from H10.

At 09:45 the rolling optimizer started at H10. Leftover export must stay closed
because H08 PV already covers house load. Snapshot from Pi 2026-09-14.
"""

from __future__ import annotations

from datetime import date

from src.grid_config import export_window_start_hour, merge_grid_defaults
from src.plan_optimizer import evening_export_window_hours, morning_cover_bound_from_hour_buys
from src.plan_q15 import run_rolling_smart_q15_plan, timer_schedule_by_hour
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
)

DATE = "2026-09-14"
CAP_KWH = 47.6
# End-SOC after inverter H09 (history row).
SOC_AFTER_H09_PCT = 21.0

# Inverter / Influx hourly actuals H00–H09 (kWh).
PV_ACTUAL_H00_H09 = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.139, 1.013, 2.02]
LOAD_ACTUAL_H00_H09 = [1.014, 1.072, 0.533, 0.454, 0.465, 0.529, 0.53, 0.512, 0.516, 0.682]

# Plan (forecast) from H10 (kWh).
PV_PLAN = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.018, 0.145, 0.981, 2.021,
    0.595, 1.798, 1.281, 3.106, 0.421, 0.896, 0.538, 0.669,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
]
LOAD_PLAN = [
    1.127, 0.771, 0.536, 0.672, 0.554, 0.57, 0.546, 0.563, 0.502, 0.547,
    0.578, 0.876, 0.763, 0.56, 0.572, 0.553, 0.529, 0.847,
    0.875, 0.778, 0.87, 1.179, 1.197, 1.381,
]
PV_TOMORROW = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.032, 0.532, 1.674, 2.58, 3.094, 3.78,
    4.677, 3.944, 3.497, 3.108, 2.083, 1.768, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
]
LOAD_TOMORROW = [
    1.156, 1.048, 0.595, 0.615, 0.609, 0.612, 0.584, 0.604, 0.563, 0.675,
    0.613, 0.964, 0.853, 0.67, 0.707, 0.732, 1.459, 0.758,
    0.739, 0.784, 1.089, 1.315, 1.212, 1.151,
]

# Pi /api/rce series_15min 2026-09-14. Tomorrow unpublished at snapshot time.
RCE_TODAY = [
    1.0713, 1.0525, 1.0157, 0.9972,
    1.0078, 0.985, 0.9866, 0.9747,
    0.9734, 0.9662, 0.9589, 0.9636,
    0.9495, 0.9526, 0.9557, 0.9597,
    0.9541, 0.9574, 0.9646, 1.0056,
    0.9859, 0.9953, 1.0376, 1.1226,
    1.1311, 1.2699, 1.3764, 1.9481,
    1.7436, 1.7847, 1.6333, 1.8761,
    1.9285, 1.5589, 1.4217, 1.3626,
    1.4711, 1.3859, 1.2522, 1.06,
    1.1745, 1.1366, 1.0633, 1.0047,
    0.9889, 0.9995, 0.9684, 0.9611,
    0.9931, 0.9217, 0.8633, 0.863,
    0.8518, 0.8467, 0.8374, 0.8359,
    0.794, 0.8238, 0.8221, 0.8423,
    0.8321, 0.8375, 0.8874, 0.9168,
    0.8455, 0.9292, 1.0073, 1.0998,
    1.0236, 1.1376, 1.3192, 1.5028,
    1.3661, 1.5605, 1.9452, 2.8258,
    2.6067, 3.2283, 3.3233, 3.7379,
    3.5244, 3.1592, 2.6302, 1.9006,
    2.1156, 1.877, 1.4178, 1.313,
    1.4147, 1.3397, 1.2729, 1.1652,
    1.2337, 1.182, 1.1232, 1.0617,
]


def _today_hourly() -> tuple[list[float], list[float]]:
    pv = list(PV_PLAN)
    load = list(LOAD_PLAN)
    pv[:10] = [float(v) for v in PV_ACTUAL_H00_H09]
    load[:10] = [float(v) for v in LOAD_ACTUAL_H00_H09]
    return pv, load


def _cfg() -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": CAP_KWH,
            "max_charge_power_kw": 9.2,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "epsilon_kwh": 0.05,
            "horizon_hours": 24,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
                "pv_to_battery": 7.5,
            },
        },
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
        "grid": {
            "grid_export_threshold_pln_kwh": 0.68,
            "export_window_start_hour": 16,
            "g12": {
                "tariff_name": "Energa G12",
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.7182,
                "offpeak_energy_only_pln_kwh": 0.4678,
            },
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def _export_window(cfg: dict, pv: list[float], load: list[float]) -> set[int]:
    params = get_simulation_params(cfg)
    today = date(2026, 9, 14)
    g12_cover = morning_cover_bound_from_hour_buys(
        offpeak_buy=float(cfg["grid"]["g12"]["offpeak_price_pln_kwh"]),
        epsilon=float(params["epsilon_kwh"]),
        cfg=cfg,
        today_date=today,
    )
    return evening_export_window_hours(
        list(range(48)),
        pv_series=[],
        load_series=[],
        rce_step_offset=0,
        slots=4,
        steps=0,
        eta_pv_load=float(params["eta_pv_load"]),
        epsilon=float(params["epsilon_kwh"]),
        export_window_start_hour=export_window_start_hour(cfg),
        forecast={
            "today": {"pv": list(pv), "load": list(load)},
            "tomorrow": {"pv": list(PV_TOMORROW), "load": list(LOAD_TOMORROW)},
        },
        cover_bound=g12_cover,
        cfg=cfg,
        today_date=today,
    )


def test_sep14_h10_not_in_export_window_with_inverter_morning():
    """H08 actual PV covers load; leftover must not reopen at H10."""
    cfg = _cfg()
    pv, load = _today_hourly()
    window = _export_window(cfg, pv, load)
    assert 8 not in window
    assert 9 not in window
    assert 10 not in window
    assert 11 not in window
    assert 15 not in window
    assert 16 in window


def test_sep14_replay_from_h10_does_not_export_before_16():
    """Rolling plan at 10:00 (DP from H10) must not write a H10 Dis timer."""
    cfg = _cfg()
    pv, load = _today_hourly()
    soc = SOC_AFTER_H09_PCT / 100.0 * CAP_KWH
    rolling = run_rolling_smart_q15_plan(
        date_str=DATE,
        pv_hourly=pv,
        load_hourly=load,
        tomorrow_pv=list(PV_TOMORROW),
        tomorrow_load=list(LOAD_TOMORROW),
        cfg=cfg,
        rce_quarters=list(RCE_TODAY),
        rce_quarters_tomorrow=[None] * 96,
        initial_soc_kwh=soc,
        from_hour=10,
        horizon_hours=24,
        front_load_skip_leading_slots=0,
    )
    assert rolling is not None
    today = rolling["today"]
    assert today is not None
    timers = timer_schedule_by_hour(today["q15_by_hour"], cfg, today["epsilon"])
    for hour in range(10, 16):
        txt = timers.get(hour) or ""
        assert not txt.startswith("Dis"), (
            f"H{hour:02d} must not get a leftover Dis timer, got {txt!r}"
        )
        export_kwh = sum(
            float(s.get("battery_export_kwh") or 0)
            for s in (today["q15_by_hour"].get(hour) or [])
        )
        assert export_kwh <= today["epsilon"], (
            f"H{hour:02d} battery→grid {export_kwh:.3f} kWh with window start 16"
        )
