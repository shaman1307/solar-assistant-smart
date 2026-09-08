"""Manual Timer Schedule overrides on Energy arbitrage plan."""

from src.plan_timer_override import (
    hour_control_from_timer_override,
    set_timer_schedule_override,
)
from src.plan_optimizer import HourControl


def test_hour_control_empty_timer():
    ctrl = hour_control_from_timer_override(14, 0, "")
    assert ctrl == HourControl(0.0, 0.0)


def test_hour_control_charge_segment():
    txt = "Chg 14:00-15:00 5kW cap80%"
    ctrl = hour_control_from_timer_override(14, 0, txt)
    assert ctrl.grid_charge_kw == 1.25  # 5 kW * 0.25 h per 15-min step
    assert ctrl.battery_export_kwh == 0.0


def test_hour_control_discharge_segment():
    txt = "Dis 19:30-20:00 6.51kW cap16%"
    ctrl = hour_control_from_timer_override(19, 2, txt)
    assert ctrl.grid_charge_kw == 0.0
    assert ctrl.battery_export_kwh > 0


def test_is_timer_schedule_hour_editable_future_only():
    from src.plan_timer_override import is_timer_schedule_hour_editable

    assert is_timer_schedule_hour_editable("2026-06-28", 12, today_date="2026-06-29", plan_from_hour=10) is False
    assert is_timer_schedule_hour_editable("2026-06-29", 10, today_date="2026-06-29", plan_from_hour=10) is False
    assert is_timer_schedule_hour_editable("2026-06-29", 11, today_date="2026-06-29", plan_from_hour=10) is True
    assert is_timer_schedule_hour_editable("2026-06-30", 0, today_date="2026-06-29", plan_from_hour=10) is True


def test_set_timer_override_clears_later_hours():
    cfg: dict = {}
    set_timer_schedule_override(cfg, "2026-06-29", 10, "Chg 10:00-11:00 5kW cap80%")
    set_timer_schedule_override(cfg, "2026-06-29", 16, "Dis 16:00-17:00 6kW cap20%")
    set_timer_schedule_override(cfg, "2026-06-29", 14, "Dis 14:00-15:00 6kW cap20%")
    day = cfg["plan_overrides"]["timer_schedule"]["2026-06-29"]
    assert "10" in day
    assert "16" not in day
    assert day["14"].startswith("Dis")


def test_replay_charge_timer_soc_matches_kwh():
    """5 kW for 30 min => 2.5 kWh stored, not 4x from missing step_scale."""
    from src.plan_timer_override import replay_day_plan_with_timer_overrides

    cfg = {
        "battery": {"capacity_kwh": 50.0, "max_discharge_power_kw": 8.0, "max_charge_power_kw": 5.0},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 15,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "g12": {
                "offpeak_price_pln_kwh": 0.62,
                "offpeak_energy_only_pln_kwh": 0.45,
                "peak_price_pln_kwh": 0.9,
                "peak_energy_only_pln_kwh": 0.7,
            },
            "feed_in_price_pln": 0.3,
        },
    }
    date_str = "2026-07-07"
    start_soc = 8.0  # 16% of 50 kWh
    plan = {
        "q15_by_hour": {
            4: [{"soc_end": start_soc, "soc_pct": 16.0, "quarter": 3}],
        },
        "q15_plan_rows": [],
    }
    pv_q = [0.0] * 96
    load_q = [0.0] * 96
    buy_q = [0.62] * 96
    rce_q: list[float | None] = [None] * 96
    timer = "Chg 05:30-06:00 5kW cap45%"
    out = replay_day_plan_with_timer_overrides(
        plan,
        {5: timer},
        date_str=date_str,
        pv_q=pv_q,
        load_q=load_q,
        buy_q=buy_q,
        rce_q=rce_q,
        cfg=cfg,
        from_hour=5,
        initial_soc_kwh=start_soc,
    )
    slots = out["q15_by_hour"][5]
    end_soc = float(slots[-1]["soc_end"])
    delta_kwh = end_soc - start_soc
    delta_pct = (end_soc / 50.0) * 100.0 - 16.0
    assert 2.0 < delta_kwh < 3.0, f"expected ~2.5 kWh, got {delta_kwh}"
    assert delta_pct < 8.0, f"expected ~5% SOC, got +{delta_pct:.1f}%"


def test_grid_charge_with_load_priority():
    """30 min @ 5 kW charge; 5.5 kWh house from battery → import ≈ charge/η only."""
    from src.plan_q15 import build_smart_plan_hour_row
    from src.plan_timer_override import replay_day_plan_with_timer_overrides

    cfg = {
        "battery": {"capacity_kwh": 43.0, "max_discharge_power_kw": 8.0, "max_charge_power_kw": 5.0},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 15,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_battery": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "g12": {
                "offpeak_price_pln_kwh": 0.62,
                "offpeak_energy_only_pln_kwh": 0.45,
                "peak_price_pln_kwh": 0.9,
                "peak_energy_only_pln_kwh": 0.7,
            },
            "feed_in_price_pln": 0.3,
        },
    }
    date_str = "2026-07-07"
    # Start high enough that 5.5 kWh load can come from battery during the hour.
    start_soc = 20.0
    load_q = [0.0] * 96
    for q in range(4):
        load_q[5 * 4 + q] = 5.5 / 4.0
    pv_q = [0.0] * 96
    pv_q[5 * 4] = 0.03
    timer = "Chg 05:30-06:00 5kW cap45%"
    out = replay_day_plan_with_timer_overrides(
        {"q15_by_hour": {4: [{"soc_end": start_soc, "quarter": 3}]}, "q15_plan_rows": []},
        {5: timer},
        date_str=date_str,
        pv_q=pv_q,
        load_q=load_q,
        buy_q=[0.62] * 96,
        rce_q=[None] * 96,
        cfg=cfg,
        from_hour=5,
        initial_soc_kwh=start_soc,
    )
    from datetime import datetime

    row = build_smart_plan_hour_row(
        datetime.strptime(date_str, "%Y-%m-%d").replace(hour=5),
        out["q15_by_hour"][5],
        cfg=cfg,
        epsilon=0.05,
        display_pv=0.03,
        display_load=5.5,
        manual_timer_schedule=timer,
    )
    # 30 min * 5 kW = 2.5 kWh AC on the meter (house not on meter).
    assert 2.3 < row["grid_import"] < 2.7, row["grid_import"]
    assert float(row["grid_import"]) < 4.0
    slots = out["q15_by_hour"][5]
    soc_end = float(slots[-1]["soc_end"])
    assert soc_end < start_soc
    assert soc_end > 15.0
