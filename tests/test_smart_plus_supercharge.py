"""Smart+ morning supercharge: same sell filter, charge eta, Nov 12 horizon."""

from __future__ import annotations

from datetime import datetime

from src.grid_config import merge_grid_defaults
from src.plan_charge import supercharge_counted_hours
from src.plan_optimizer import optimize_horizon
from src.plan_q15 import run_rolling_smart_q15_plan, timer_schedule_by_hour
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
    plan_min_soc_kwh,
)

# PSE RCE PLN/kWh brutto, 2026-11-12. Nov 13 is not published.
NOV12_RCE = [
    1.0087, 1.0682, 1.0657, 1.0328, 1.0016, 0.9884,
    1.6991, 2.9506, 2.8933, 3.2838, 3.1972, 2.8467,
    2.3541, 2.4477, 2.5058, 2.9453, 2.815, 2.403,
    2.064, 1.8865, 1.4293, 0.7835, 0.7042, 0.6528,
]
# Forecast PV/load for 2026-11-12 (kWh per hour).
NOV12_PV = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.024, 0.279, 0.462, 0.611, 0.899,
    1.133, 1.172, 0.578, 0.085, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
]
NOV12_LOAD = [
    0.842, 0.745, 0.594, 0.574, 0.525, 0.526, 0.587, 0.608, 0.545, 0.623,
    0.712, 0.996, 0.774, 0.644, 0.724, 0.915, 0.747, 0.717, 0.806, 0.81,
    0.904, 1.111, 1.038, 1.001,
]


def _quarters(hourly: list[float | None]) -> list[float | None]:
    out: list[float | None] = []
    for price in hourly:
        out.extend([price, price, price, price])
    return out


def _live_cfg(*, smart_plus: bool) -> dict:
    cfg = {
        "smart_mode_enabled": True,
        "smart_plus_enabled": smart_plus,
        "battery": {
            "capacity_kwh": 47.6,
            "max_charge_power_kw": 5.8,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "horizon_hours": 30,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
                "pv_to_battery": 7.5,
            },
        },
        "inverter": {
            "ac_capacity_kw": 8.0,
            "to_grid_battery_capacity": 16,
            "back_to_battery_capacity": 26,
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.68,
            "export_window_start_hour": 16,
            "super_rce_min_pln_kwh": 2.0,
            "g12": {
                "tariff_preset": "G12",
                "tariff_name": "Energa G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.7182,
                "offpeak_energy_only_pln_kwh": 0.4678,
            },
        },
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 1.0},
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def test_nov12_counts_morning_only_when_evening_is_not_richer():
    """H12 (2.35) loses to H16 (2.82). H07–H11 stay: the evening is cheaper.
    H13–H14 are daytime off-peak, not sale hours."""
    cfg = _live_cfg(smart_plus=True)
    rce = _quarters(NOV12_RCE) + [None] * 96
    counted = supercharge_counted_hours(
        end_hour=6,
        rce_series=rce,
        slots_per_hour=4,
        horizon_hours=list(range(30)),
        min_price=2.0,
        export_window_start_hour=16,
        cfg=cfg,
        today_date=datetime(2026, 11, 12),
    )
    assert counted == [7, 8, 9, 10, 11, 15, 16, 17, 18]


def test_supercharge_grid_ac_includes_charge_eta():
    """Extra battery DC is discharge kW × hours; grid AC is that DC / eta_grid."""
    cfg = _live_cfg(smart_plus=False)
    cfg["battery"]["capacity_kwh"] = 48.0
    cfg["battery"]["max_charge_power_kw"] = 9.2
    params = get_simulation_params(cfg)
    eta_grid = float(params["eta_grid_battery"])
    hours = 18
    slots = 4
    steps = hours * slots
    pv = [0.0] * steps
    load = [0.15] * steps
    off, peak = 0.6229, 1.2444
    buy = (
        [off] * (6 * slots)
        + [peak] * (7 * slots)
        + [off] * (2 * slots)
        + [peak] * (3 * slots)
    )
    rce_h = [0.4] * 24
    rce_h[8] = rce_h[9] = rce_h[10] = 3.0
    rce_h[16] = 1.0
    rce = _quarters(rce_h)
    initial = plan_min_soc_kwh(cfg) + 0.4
    end = datetime(2026, 11, 12, 17, 45)
    pv_h = [0.0] * 24
    load_h = [0.15] * 24
    forecast = {
        "today": {"pv": pv_h, "load": load_h, "pv_total": 0.0, "load_total": sum(load_h)},
        "tomorrow": {"pv": pv_h, "load": load_h, "pv_total": 0.0, "load_total": sum(load_h)},
    }

    def _run(plus: bool):
        run_cfg = {**cfg, "smart_plus_enabled": plus}
        return optimize_horizon(
            steps=steps,
            pv_series=pv,
            load_series=load,
            buy_prices=buy,
            rce_series=rce,
            initial_soc_kwh=initial,
            cfg=run_cfg,
            params=params,
            end_dt=end,
            today_date=end.date(),
            rce_map={},
            forecast=forecast,
            step_scale=0.25,
            rce_step_offset=0,
            front_load_skip_leading_slots=0,
        )

    base = _run(False)
    plus = _run(True)
    base_ac = sum(c.grid_charge_kw for c in base)
    plus_ac = sum(c.grid_charge_kw for c in plus)
    # Three super hours × 8 kW DC. Grid AC is DC / eta_grid, plus house load
    # on the extra charge steps (the charge pass buys that load too).
    extra_dc = 8.0 * 3
    extra_ac = plus_ac - base_ac
    assert extra_ac > extra_dc / eta_grid - 0.05
    house_dc = extra_ac * eta_grid - extra_dc
    assert 0.0 <= house_dc < 4.0


def _end_soc_pct(slots: list[dict]) -> float | None:
    """SOC at the end of the hour, from the last quarter."""
    if not slots:
        return None
    last = max(slots, key=lambda slot: int(slot.get("quarter", 0)))
    soc = last.get("soc_pct")
    return None if soc is None else float(soc)


def test_nov12_timer_actions_through_22():
    """2026-11-12 00:00–21:00. Hours from 22:00 on are outside this case."""
    cfg = _live_cfg(smart_plus=True)
    plan = run_rolling_smart_q15_plan(
        date_str="2026-11-12",
        pv_hourly=NOV12_PV,
        load_hourly=NOV12_LOAD,
        tomorrow_pv=NOV12_PV,
        tomorrow_load=NOV12_LOAD,
        cfg=cfg,
        rce_quarters=_quarters(NOV12_RCE),
        rce_quarters_tomorrow=[None] * 96,
        initial_soc_kwh=0.21 * 47.6,
        from_hour=0,
        horizon_hours=22,
        front_load_skip_leading_slots=0,
    )
    assert plan is not None
    today = plan["today"]
    assert today is not None
    eps = float(get_simulation_params(cfg)["epsilon_kwh"])
    timers = timer_schedule_by_hour(today["q15_by_hour"], cfg, eps)
    hours = list(range(22))
    lines = ["date       hour  RCE     SOC    Timer Schedule"]
    morning_chg = False
    for hour in hours:
        action = timers.get(hour, "")
        soc = _end_soc_pct(today["q15_by_hour"].get(hour) or [])
        soc_txt = f"{soc:5.1f}%" if soc is not None else "    -"
        label = action or "-"
        if hour < 6 and action.startswith("Chg"):
            morning_chg = True
        lines.append(
            f"2026-11-12  {hour:02d}:00  {NOV12_RCE[hour]:6.3f}  {soc_txt}  {label}"
        )
    text = "\n".join(lines)
    print(text)
    assert morning_chg
    assert len(hours) == 22
    assert "2026-11-12  00:00" in text
    assert "2026-11-12  21:00" in text
    assert "2026-11-13" not in text

    plain = run_rolling_smart_q15_plan(
        date_str="2026-11-12",
        pv_hourly=NOV12_PV,
        load_hourly=NOV12_LOAD,
        tomorrow_pv=NOV12_PV,
        tomorrow_load=NOV12_LOAD,
        cfg=_live_cfg(smart_plus=False),
        rce_quarters=_quarters(NOV12_RCE),
        rce_quarters_tomorrow=[None] * 96,
        initial_soc_kwh=0.21 * 47.6,
        from_hour=0,
        horizon_hours=22,
        front_load_skip_leading_slots=0,
    )

    def _morning_ac(day: dict) -> float:
        slots = day["q15_by_hour"]
        return sum(
            float(slot.get("grid_charge_kw") or 0.0)
            for hour in range(6)
            for slot in (slots.get(hour) or [])
        )

    assert _morning_ac(today) > _morning_ac(plain["today"]) + 15.0
    afternoon = timers.get(13, "") + timers.get(14, "")
    assert afternoon.startswith("Chg") or "Chg" in afternoon
    peak_import = sum(
        float(slot.get("grid_import") or 0.0)
        for hour in range(15, 22)
        for slot in (today["q15_by_hour"].get(hour) or [])
    )
    assert peak_import < 0.5
    # Morning super hours: SOC at 13:00 lands on the floor, not above it.
    soc_13 = _end_soc_pct(today["q15_by_hour"].get(12) or [])
    assert soc_13 is not None and 16.0 - 0.5 <= soc_13 <= 16.0 + 5.0


# PSE RCE PLN/kWh brutto and forecast PV/load (kWh per hour), 2026-11-17 00:00.
NOV17_RCE = [
    1.0225, 1.032, 1.017, 1.0128, 0.9932, 0.8425, 1.0888, 1.4679,
    1.5363, 1.49, 1.3953, 1.3548, 1.4168, 1.8405, 2.4009, 2.4337,
    2.0575, 1.685, 1.5703, 1.4848, 1.3018, 1.0431, 0.9575, 0.8132,
]
NOV17_PV = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.23, 2.321, 3.395, 3.132,
    2.205, 1.76, 1.236, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
]
NOV17_LOAD = [
    0.929, 0.805, 0.653, 0.619, 0.569, 0.543, 0.67, 0.656, 0.59, 0.643,
    0.755, 0.962, 0.801, 0.641, 1.099, 0.714, 0.652, 0.599, 0.609, 0.884,
    1.196, 1.207, 1.12, 1.096,
]
NOV18_PV = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.164, 0.199, 0.246, 0.204,
    0.307, 0.459, 0.299, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
]
NOV18_LOAD = [
    0.76, 0.916, 0.688, 0.544, 0.552, 0.624, 0.622, 0.582, 0.678, 0.71,
    0.67, 0.962, 0.854, 0.571, 0.64, 0.739, 0.695, 0.64, 0.548, 0.756,
    1.056, 1.006, 0.829, 1.006,
]


def _nov17_plan(rce_hourly: list[float]) -> dict:
    plan = run_rolling_smart_q15_plan(
        date_str="2026-11-17",
        pv_hourly=NOV17_PV,
        load_hourly=NOV17_LOAD,
        tomorrow_pv=NOV18_PV,
        tomorrow_load=NOV18_LOAD,
        cfg=_live_cfg(smart_plus=True),
        rce_quarters=_quarters(rce_hourly),
        rce_quarters_tomorrow=[None] * 96,
        initial_soc_kwh=0.20 * 47.6,
        from_hour=0,
        horizon_hours=36,
        front_load_skip_leading_slots=0,
    )
    assert plan is not None
    return plan["today"]["q15_by_hour"]


def test_nov17_evening_super_hours_charge_only_to_the_survive_reserve():
    """H15–H16 are super hours; H14 is daytime off-peak. SOC at 22:00 lands on
    the survive reserve (house until 13:00 next day + min SOC, 38.9%)."""
    cfg = _live_cfg(smart_plus=True)
    rce = _quarters(NOV17_RCE)
    counted = supercharge_counted_hours(
        end_hour=6,
        rce_series=rce + [None] * 96,
        slots_per_hour=4,
        horizon_hours=list(range(36)),
        min_price=2.0,
        export_window_start_hour=16,
        cfg=cfg,
        today_date=datetime(2026, 11, 17),
    )
    assert counted == [15, 16]
    by_hour = _nov17_plan(NOV17_RCE)
    soc_06 = _end_soc_pct(by_hour.get(5) or [])
    soc_15 = _end_soc_pct(by_hour.get(14) or [])
    soc_22 = _end_soc_pct(by_hour.get(21) or [])
    print(f"SOC 06:00 {soc_06:.1f}%  15:00 {soc_15:.1f}%  22:00 {soc_22:.1f}%")
    assert soc_06 < 70.0
    assert soc_15 <= 85.0
    assert 38.0 <= soc_22 <= 41.0
    sold = sum(
        float(slot.get("grid_export") or 0.0)
        for hour in (15, 16)
        for slot in (by_hour.get(hour) or [])
    )
    assert sold > 12.5


def test_nov17_long_super_window_sells_below_survive_reserve():
    """H15–H18 at 2.5 PLN: 95% at 15:00 cannot cover four full hours plus the
    survive reserve. The sale runs below it, but not below min SOC plus the
    house until 22:00."""
    rce = list(NOV17_RCE)
    for hour in (15, 16, 17, 18):
        rce[hour] = 2.5
    by_hour = _nov17_plan(rce)
    soc_15 = _end_soc_pct(by_hour.get(14) or [])
    soc_19 = _end_soc_pct(by_hour.get(18) or [])
    soc_22 = _end_soc_pct(by_hour.get(21) or [])
    sold = {
        hour: sum(float(s.get("grid_export") or 0.0) for s in (by_hour.get(hour) or []))
        for hour in (15, 16, 17, 18)
    }
    print(f"SOC 15:00 {soc_15:.1f}%  19:00 {soc_19:.1f}%  22:00 {soc_22:.1f}%  sold {sold}")
    assert soc_15 <= 95.5
    assert all(kwh > 6.5 for kwh in sold.values())
    assert soc_22 < 38.0
    assert soc_22 >= 16.0 - 0.5
