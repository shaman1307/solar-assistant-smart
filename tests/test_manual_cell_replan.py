"""A manual Timer Schedule cell re-plans the later hours from its end SOC.

Hours after the last manual cell are optimized again, so the extra charge is
sold. Smart+ lowers the sale reserve down to min SOC at the 22:00 deadline
also when the plan starts inside a peak block.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.plan_q15 import run_day_smart_q15_plan
from src.simulation import build_energy_arbitrage_plan
from tests.test_smart_plus_supercharge import (
    NOV17_LOAD,
    NOV17_PV,
    NOV17_RCE,
    NOV18_LOAD,
    NOV18_PV,
    _end_soc_pct,
    _live_cfg,
    _quarters,
)

TODAY = "2026-11-17"
TOMORROW = "2026-11-18"
MANUAL_CHARGE = "Chg 14:00-15:00 5.8kW cap100%"
MIN_SOC_PCT = 16.0


def _rce_with_sale_block() -> list[float]:
    """Nov 17 RCE with H15–H19 at 2.5 PLN/kWh (all above the Smart+ sale floor)."""
    rce = list(NOV17_RCE)
    for hour in (15, 16, 17, 18, 19):
        rce[hour] = 2.5
    return rce


def _plan_at_10(*, smart_plus: bool, manual: bool) -> dict:
    """Plan built at 10:05 from 60% SOC, with or without the manual H14 charge."""
    cfg = _live_cfg(smart_plus=smart_plus)
    if manual:
        cfg["plan_overrides"] = {"timer_schedule": {TODAY: {"14": MANUAL_CHARGE}}}
    now = datetime(2026, 11, 17, 10, 5, tzinfo=ZoneInfo("Europe/Warsaw"))
    forecast = {
        "today": {
            "pv": NOV17_PV, "load": NOV17_LOAD,
            "pv_forecast": NOV17_PV, "load_forecast": NOV17_LOAD,
            "pv_total": sum(NOV17_PV), "load_total": sum(NOV17_LOAD),
        },
        "tomorrow": {
            "pv": NOV18_PV, "load": NOV18_LOAD,
            "pv_total": sum(NOV18_PV), "load_total": sum(NOV18_LOAD),
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 60.0,
        "today_hourly": {
            **{k: [0.0] * 24 for k in (
                "pv", "load", "bat_charge", "bat_discharge", "grid_buy", "grid_sell",
            )},
            "soc": [60.0] * 24,
        },
        "series_10min": None,
    }
    rce_q = {TODAY: _quarters(_rce_with_sale_block()), TOMORROW: [None] * 96}
    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=None),
        patch("src.simulation.quarter_rce_for_dates", return_value=rce_q),
    ):
        return build_energy_arbitrage_plan(forecast, metrics, {}, cfg)


def _today_rows(plan: dict) -> dict[int, dict]:
    return {
        int(r["hour"]): r
        for r in plan["rows"]
        if r.get("start") != "TOTAL" and str(r.get("plan_date")) == TODAY
    }


def test_manual_charge_cell_keeps_its_own_timer_and_raises_soc():
    rows = _today_rows(_plan_at_10(smart_plus=False, manual=True))
    base = _today_rows(_plan_at_10(smart_plus=False, manual=False))
    assert rows[14]["timer_schedule"] == MANUAL_CHARGE
    assert float(rows[14]["soc"]) > float(base[14]["soc"]) + 8.0


def test_manual_charge_cell_replans_the_hours_after_it():
    """Without Smart+: the extra charge is sold in H16 instead of kept at the reserve."""
    base = _today_rows(_plan_at_10(smart_plus=False, manual=False))
    manual = _today_rows(_plan_at_10(smart_plus=False, manual=True))
    assert float(base[16]["bat_discharge"]) < 4.0
    assert float(manual[16]["bat_discharge"]) > 7.5
    assert "Dis" in str(manual[16]["timer_schedule"])
    assert "Dis" in str(manual[15]["timer_schedule"])


def test_smart_plus_manual_charge_lengthens_the_evening_sale():
    """Smart+: the extra charge adds sale hours and SOC still reaches min SOC by 22:00."""
    base_plan = _plan_at_10(smart_plus=True, manual=False)
    manual_plan = _plan_at_10(smart_plus=True, manual=True)
    base = _today_rows(base_plan)
    manual = _today_rows(manual_plan)
    assert set(base_plan["plan_export_hours"]) < set(manual_plan["plan_export_hours"])
    assert 18 in manual_plan["plan_export_hours"]
    assert 18 not in base_plan["plan_export_hours"]
    assert float(manual[18]["bat_discharge"]) > float(base[18]["bat_discharge"]) + 3.0
    assert "Dis" in str(manual[18]["timer_schedule"])
    for rows in (base, manual):
        assert MIN_SOC_PCT - 0.5 <= float(rows[21]["soc"]) <= MIN_SOC_PCT + 0.5


def _evening_plan(*, smart_plus: bool) -> dict[int, list[dict]]:
    """Plan that starts at 15:00 with a 95% battery (the horizon starts in a peak block)."""
    plan = run_day_smart_q15_plan(
        date_str=TODAY,
        pv_hourly=NOV17_PV,
        load_hourly=NOV17_LOAD,
        tomorrow_pv=NOV18_PV,
        tomorrow_load=NOV18_LOAD,
        cfg=_live_cfg(smart_plus=smart_plus),
        rce_quarters=_quarters(_rce_with_sale_block()),
        initial_soc_kwh=0.95 * 47.6,
        from_hour=15,
        front_load_skip_leading_slots=0,
    )
    assert plan is not None
    return plan["q15_by_hour"]


def test_smart_plus_plan_inside_peak_block_sells_down_to_min_soc_by_22():
    by_hour = _evening_plan(smart_plus=True)
    sold = {
        hour: sum(float(s.get("grid_export") or 0.0) for s in by_hour.get(hour) or [])
        for hour in range(15, 21)
    }
    assert all(sold[hour] > 6.0 for hour in (15, 16, 17, 18)), sold
    assert sold[19] > 1.0, sold
    soc_21 = _end_soc_pct(by_hour.get(21) or [])
    assert soc_21 is not None
    assert MIN_SOC_PCT - 0.5 <= soc_21 <= MIN_SOC_PCT + 0.5


def test_plain_plan_inside_peak_block_holds_the_survive_reserve():
    by_hour = _evening_plan(smart_plus=False)
    soc_21 = _end_soc_pct(by_hour.get(21) or [])
    assert soc_21 is not None and soc_21 > 35.0

def _plan_at_0105_with_committed_hour(*, smart_plus: bool) -> dict:
    """The 15-minute re-plan: H01 is committed (Chg, ends at 50%), H02 on is planned."""
    cfg = _live_cfg(smart_plus=smart_plus)
    now = datetime(2026, 11, 17, 1, 5, tzinfo=ZoneInfo("Europe/Warsaw"))
    committed = {
        "plan_date": TODAY,
        "hour": 1,
        "start": "17-11-2026 02:00",
        "timer_schedule": "Chg 01:15-02:00 5.0kW cap50%",
        "action": "Charging from Grid",
        "soc": 50.0,
        "hour_labels_locked": True,
        "production": 0.0,
        "consumption": 0.6,
        "battery": 3.0,
        "bat_charge": 3.4,
        "bat_discharge": 0.4,
        "grid_import": 3.7,
        "grid_export": 0.0,
        "q15": [
            {"quarter": q, "soc": 44.0 + q * 2.0, "production": 0.0, "consumption": 0.15,
             "battery": 0.7, "grid_import": 0.9, "grid_export": 0.0, "from_actual": q < 1}
            for q in range(4)
        ],
    }
    stored = {"today_date": TODAY, "plan_from_hour": 1, "rows": [committed], "history_rows": []}
    forecast = {
        "today": {
            "pv": NOV17_PV, "load": NOV17_LOAD,
            "pv_forecast": NOV17_PV, "load_forecast": NOV17_LOAD,
            "pv_total": sum(NOV17_PV), "load_total": sum(NOV17_LOAD),
        },
        "tomorrow": {
            "pv": NOV18_PV, "load": NOV18_LOAD,
            "pv_total": sum(NOV18_PV), "load_total": sum(NOV18_LOAD),
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 45.0,
        "today_hourly": {
            **{k: [0.0] * 24 for k in (
                "pv", "load", "bat_charge", "bat_discharge", "grid_buy", "grid_sell",
            )},
            "soc": [45.0] * 24,
        },
        "series_10min": None,
    }
    rce = _rce_with_sale_block()
    for hour in range(0, 6):
        rce[hour] = 1.15
    rce_q = {TODAY: _quarters(rce), TOMORROW: [None] * 96}
    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=stored),
        patch("src.simulation.quarter_rce_for_dates", return_value=rce_q),
    ):
        return build_energy_arbitrage_plan(forecast, metrics, {}, cfg)


def _night_sold(by_hour: dict[int, list[dict]]) -> dict[int, float]:
    return {
        hour: sum(float(s.get("grid_export") or 0.0) for s in by_hour.get(hour) or [])
        for hour in range(2, 6)
    }


def _night_start_plan(*, smart_plus: bool, start_soc_pct: float) -> dict[int, list[dict]]:
    """Plan from H02 (the night grid-charge window) with the SOC the committed hour left."""
    rce = _rce_with_sale_block()
    for hour in range(0, 6):
        rce[hour] = 1.15
    plan = run_day_smart_q15_plan(
        date_str=TODAY,
        pv_hourly=NOV17_PV,
        load_hourly=NOV17_LOAD,
        tomorrow_pv=NOV18_PV,
        tomorrow_load=NOV18_LOAD,
        cfg=_live_cfg(smart_plus=smart_plus),
        rce_quarters=_quarters(rce),
        initial_soc_kwh=start_soc_pct / 100.0 * 47.6,
        from_hour=2,
        front_load_skip_leading_slots=0,
    )
    assert plan is not None
    return plan["q15_by_hour"]


def test_smart_plus_night_charge_window_never_sells_the_battery():
    """A surplus SOC at 02:00 is not sold and bought back: the night is charge only."""
    for soc_pct in (35.0, 52.0, 65.0):
        sold = _night_sold(_night_start_plan(smart_plus=True, start_soc_pct=soc_pct))
        assert all(kwh < 0.05 for kwh in sold.values()), (soc_pct, sold)


def test_plain_plan_still_sells_at_night_when_the_soc_allows():
    sold = _night_sold(_night_start_plan(smart_plus=False, start_soc_pct=52.0))
    assert sold[2] > 5.0, sold


def test_smart_plus_quarter_replan_keeps_the_night_charge_only():
    """The re-plan with a committed H01 ending at 50% has no Dis between the night Chg hours."""
    plan = _plan_at_0105_with_committed_hour(smart_plus=True)
    rows = _today_rows(plan)
    for hour in range(2, 6):
        assert "Dis" not in str(rows[hour]["timer_schedule"]), (hour, rows[hour]["timer_schedule"])
    assert not set(plan["plan_export_hours"]) & set(range(0, 6))
    assert {15, 16}.issubset(set(plan["plan_export_hours"]))

def _night_start_plan_with_evening_rce(
    *, smart_plus: bool, evening_rce: float, start_soc_pct: float,
) -> dict[int, list[dict]]:
    rce = list(NOV17_RCE)
    for hour in (15, 16, 17, 18, 19):
        rce[hour] = evening_rce
    for hour in range(0, 6):
        rce[hour] = 1.15
    plan = run_day_smart_q15_plan(
        date_str=TODAY,
        pv_hourly=NOV17_PV,
        load_hourly=NOV17_LOAD,
        tomorrow_pv=NOV18_PV,
        tomorrow_load=NOV18_LOAD,
        cfg=_live_cfg(smart_plus=smart_plus),
        rce_quarters=_quarters(rce),
        initial_soc_kwh=start_soc_pct / 100.0 * 47.6,
        from_hour=2,
        front_load_skip_leading_slots=0,
    )
    assert plan is not None
    return plan["q15_by_hour"]


def test_smart_plus_below_the_rce_floor_leaves_the_standard_night_sale():
    """No hour reaches super_rce_min (2.0): Smart+ is idle and Smart plans as without it."""
    for soc_pct in (52.0, 65.0):
        plus = _night_sold(_night_start_plan_with_evening_rce(
            smart_plus=True, evening_rce=1.5, start_soc_pct=soc_pct,
        ))
        plain = _night_sold(_night_start_plan_with_evening_rce(
            smart_plus=False, evening_rce=1.5, start_soc_pct=soc_pct,
        ))
        assert plain[2] > 5.0, plain
        assert plus == plain, (soc_pct, plus, plain)


def test_smart_plus_active_night_window_holds_the_charge_for_the_evening_sale():
    """Hours at 2.5 PLN make Smart+ active: the night window is not sold, the evening is."""
    by_hour = _night_start_plan_with_evening_rce(
        smart_plus=True, evening_rce=2.5, start_soc_pct=52.0,
    )
    assert all(kwh < 0.05 for kwh in _night_sold(by_hour).values())
    evening = sum(
        float(s.get("grid_export") or 0.0) for h in (15, 16, 17) for s in by_hour.get(h) or []
    )
    assert evening > 15.0, evening
