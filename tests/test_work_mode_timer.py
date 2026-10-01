"""Timer schedule discharge end parsing for work mode."""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.timer_plan import (
    timer_charge_end_due,
    timer_discharge_active_at,
    timer_discharge_early_end_hhmm,
    timer_discharge_end_due,
    timer_discharge_end_times_hhmm,
)
from src.work_mode_scheduler import limit_home_due_for_timer, on_grid_job_applied


def test_full_hour_discharge_not_early():
    assert timer_discharge_early_end_hhmm("Dis 19:00-20:00 7.12kW cap16%", 19) is None


def test_early_end_at_45():
    assert timer_discharge_early_end_hhmm("Dis 22:00-22:45 6.54kW cap16%", 22) == "22:45"


def test_no_discharge():
    assert timer_discharge_early_end_hhmm("Chg 02:00-03:00 4.0kW cap21%", 2) is None


def test_picks_latest_discharge_end():
    txt = "Dis 22:00-22:30 3kW cap16% | Dis 22:30-22:45 6kW cap16%"
    assert timer_discharge_early_end_hhmm(txt, 22) == "22:45"


def test_discharge_end_times():
    txt = "Dis 22:00-22:45 6.54kW cap16%"
    assert timer_discharge_end_times_hhmm(txt) == ["22:45"]


def test_discharge_end_due_at_45():
    txt = "Dis 22:00-22:45 6.54kW cap16%"
    now = datetime(2026, 6, 26, 22, 45, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, end = timer_discharge_end_due(txt, now, plan_hour=22)
    assert due is True
    assert end == "22:45"


def test_discharge_end_not_due_before_45():
    txt = "Dis 22:00-22:45 6.54kW cap16%"
    now = datetime(2026, 6, 26, 22, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, _ = timer_discharge_end_due(txt, now, plan_hour=22)
    assert due is False


def test_discharge_end_due_on_hour_boundary():
    txt = "Dis 19:00-20:00 7.12kW cap16%"
    now = datetime(2026, 6, 26, 20, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, end = timer_discharge_end_due(txt, now, plan_hour=19)
    assert due is True
    assert end == "20:00"


def test_discharge_end_not_due_stale_previous_hour():
    txt = "Dis 19:00-20:00 7.12kW cap16%"
    now = datetime(2026, 6, 26, 22, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, _ = timer_discharge_end_due(txt, now, plan_hour=19)
    assert due is False


def test_discharge_active_during_full_hour_window():
    txt = "Dis 21:00-22:00 6.96kW cap17%"
    now = datetime(2026, 6, 26, 21, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert timer_discharge_active_at(txt, now) is True


def test_discharge_not_active_after_end():
    txt = "Dis 21:00-22:00 6.96kW cap17%"
    now = datetime(2026, 6, 26, 22, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert timer_discharge_active_at(txt, now) is False


def test_discharge_active_at_window_start():
    txt = "Dis 21:00-22:00 6.96kW cap17%"
    now = datetime(2026, 6, 26, 21, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert timer_discharge_active_at(txt, now) is True


def test_full_hour_discharge_not_due_while_active():
    """Dis 21:00-22:00 still running at 21:45 — end not due on row 21."""
    txt = "Dis 21:00-22:00 6.96kW cap17%"
    now = datetime(2026, 6, 26, 21, 45, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert timer_discharge_active_at(txt, now) is True
    due, end = timer_discharge_end_due(txt, now, plan_hour=21)
    assert due is False
    assert end is None


def test_full_hour_discharge_due_after_end():
    txt = "Dis 21:00-22:00 6.96kW cap17%"
    now = datetime(2026, 6, 26, 22, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert timer_discharge_active_at(txt, now) is False
    due, end = timer_discharge_end_due(txt, now, plan_hour=21)
    assert due is True
    assert end == "22:00"


def test_limit_home_due_when_current_timer_empty():
    now = datetime(2026, 6, 26, 21, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, end = limit_home_due_for_timer("", now, plan_hour=21)
    assert due is True
    assert end is None


def test_limit_home_not_due_when_plan_timer_empty_but_sa_slot_active():
    """07:30 — plan cell cleared by refresh but SA discharge 07:00-08:00 still running."""
    now = datetime(2026, 7, 7, 7, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    sa_rules = {
        "timed_discharge_enabled": False,
        "discharge_slots": [{
            "from": "07:00", "to": "08:00", "power_kw": 8.0, "capacity_pct": 16,
        }],
    }
    due, end = limit_home_due_for_timer(
        "", now, plan_hour=7, sa_rules=sa_rules,
    )
    assert due is False
    assert end is None


def test_limit_home_due_when_timer_empty_even_if_row_already_exported():
    """Empty Timer Schedule stops export. kWh already on the row does not keep it open."""
    now = datetime(2026, 7, 7, 7, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    row = {"grid_export": 5.1, "action": "Discharging to Grid and Load"}
    due, end = limit_home_due_for_timer(
        "", now, plan_hour=7, plan_row=row,
    )
    assert due is True
    assert end is None


def test_limit_home_not_due_when_current_discharge_active():
    txt = "Dis 21:00-22:00 6.96kW cap17%"
    now = datetime(2026, 6, 26, 21, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, end = limit_home_due_for_timer(txt, now, plan_hour=21)
    assert due is False
    assert end is None


def test_charge_end_due_at_half_hour_does_not_trigger_limit_home():
    """Charge end clears Timed charge only — not the discharge Limit-home path."""
    txt = "Chg 03:00-03:30 6.0kW cap24%"
    now = datetime(2026, 7, 21, 3, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    due, end = timer_charge_end_due(txt, now, plan_hour=3)
    assert due is True
    assert end == "03:30"
    lh_due, lh_end = limit_home_due_for_timer(txt, now, plan_hour=3)
    assert lh_due is False
    assert lh_end is None


def test_on_grid_applied_only_when_trigger_this_slot():
    assert on_grid_job_applied({
        "on_grid_trigger_this_slot": True,
        "skipped": True,
        "skip_reason": "already_set",
    })
    assert not on_grid_job_applied({
        "on_grid_trigger_this_slot": False,
        "skipped": True,
        "skip_reason": "already_set",
    })
    assert on_grid_job_applied({
        "on_grid_trigger_this_slot": True,
        "ok": True,
    })


def test_consecutive_full_export_hours_limit_home_skipped():
    """Two back-to-back Dis H:00-(H+1):00 — no Limit home at :45 or :00 boundary."""
    tz = ZoneInfo("Europe/Warsaw")
    timer_h = "Dis 21:00-22:00 6.96kW cap17%"
    timer_next = "Dis 22:00-23:00 6.96kW cap17%"

    at_2145 = datetime(2026, 6, 26, 21, 45, tzinfo=tz)
    assert timer_discharge_active_at(timer_h, at_2145) is True
    due, _ = limit_home_due_for_timer(timer_h, at_2145, plan_hour=21)
    assert due is False

    at_2200 = datetime(2026, 6, 26, 22, 0, tzinfo=tz)
    assert timer_discharge_active_at(timer_h, at_2200) is False
    assert timer_discharge_active_at(timer_next, at_2200) is True
    due_prev, _ = limit_home_due_for_timer(timer_h, at_2200, plan_hour=21)
    assert due_prev is True
    due_curr, _ = limit_home_due_for_timer(timer_next, at_2200, plan_hour=22)
    assert due_curr is False

    at_2215 = datetime(2026, 6, 26, 22, 15, tzinfo=tz)
    due, _ = limit_home_due_for_timer(timer_next, at_2215, plan_hour=22)
    assert due is False
