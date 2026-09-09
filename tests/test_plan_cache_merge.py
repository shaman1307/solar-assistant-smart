"""Tests for SQLite-backed incremental Energy arbitrage plan merge."""

from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.plan_cache_merge import (
    _apply_actual_quarter_if_needed,
    _ensure_q15_length,
    _merge_current_hour_q15,
    attach_immutable_history,
    datafix_completed_quarters_from_live,
    freeze_ready_quarter_tick,
    last_completed_quarter_tick,
    merge_incremental_plan,
    plan_needs_full_rebuild,
    quarter_tick_now,
)


def _cfg():
    return {
        "battery": {"capacity_kwh": 20.0},
        "simulation": {"min_soc_pct": 16},
        "grid": {
            "g12": {
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.4,
            },
            "feed_in_price_pln": 0.2,
        },
    }


def _row(hour: int, *, timer: str = "", action: str = "Idle", locked: bool = False):
    return {
        "hour": hour,
        "plan_date": "2026-07-07",
        "start": f"07-07-2026 {hour + 1:02d}:00",
        "timer_schedule": timer,
        "action": action,
        "hour_labels_locked": locked,
        "production": 1.0,
        "consumption": 0.5,
        "battery": 0.0,
        "bat_charge": 0.0,
        "bat_discharge": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "soc": 50.0,
        "buy_price": 1.0,
        "g12_zone": "offpeak",
        "q15": [
            {"quarter": q, "production": 0.25, "consumption": 0.1, "soc": 50.0,
             "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0,
             "from_actual": False}
            for q in range(4)
        ],
    }


def test_last_completed_quarter_tick():
    tz = ZoneInfo("Europe/Warsaw")
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 0, tzinfo=tz)) == (-1, 3)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 5, tzinfo=tz)) == (0, -1)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 15, tzinfo=tz)) == (0, 0)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 30, tzinfo=tz)) == (0, 1)
    assert last_completed_quarter_tick(datetime(2026, 7, 7, 8, 45, tzinfo=tz)) == (0, 2)


def test_freeze_ready_quarter_tick_at_30_includes_q1():
    """:30/:31 freeze through current q1; other ticks still lag one q15."""
    tz = ZoneInfo("Europe/Warsaw")
    assert freeze_ready_quarter_tick(datetime(2026, 7, 7, 8, 0, tzinfo=tz)) == (-1, 2)
    assert freeze_ready_quarter_tick(datetime(2026, 7, 7, 8, 5, tzinfo=tz)) == (0, -1)
    assert freeze_ready_quarter_tick(datetime(2026, 7, 7, 8, 15, tzinfo=tz)) == (-1, 3)
    assert freeze_ready_quarter_tick(datetime(2026, 7, 7, 8, 30, tzinfo=tz)) == (0, 1)
    assert freeze_ready_quarter_tick(datetime(2026, 7, 7, 8, 31, tzinfo=tz)) == (0, 1)
    assert freeze_ready_quarter_tick(datetime(2026, 7, 7, 8, 45, tzinfo=tz)) == (0, 1)


def test_plan_needs_full_rebuild_on_new_day():
    cached = {"today_date": "2026-07-06"}
    now = datetime(2026, 7, 7, 8, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert plan_needs_full_rebuild(cached, now) is True
    assert plan_needs_full_rebuild(None, now) is True


def test_merge_at_hour_start_preserves_existing_chg_timer():
    """Regression: front-load clears current-hour Chg in fresh; :00 must not wipe SQLite.

    Before 01:00 H01 was future with Chg 01:00-01:30. At 01:00 fresh has empty H01
    (charge deferred to H02). Merge must keep the planned Chg and lock it.
    """
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 21, 1, 0, tzinfo=tz)
    existing = {
        "today_date": "2026-07-21",
        "plan_from_hour": 0,
        "rows": [
            _row(1, timer="Chg 01:00-01:30 6.0kW cap25%", action="Charging from Grid", locked=False),
            _row(2, timer="", action="Discharging to Load", locked=False),
        ],
        "history_rows": [],
        "totals": {},
    }
    # Fix plan_date on helper rows
    for r in existing["rows"]:
        r["plan_date"] = "2026-07-21"
        r["start"] = f"21-07-2026 {int(r['hour']) + 1:02d}:00"

    fresh = {
        "today_date": "2026-07-21",
        "plan_from_hour": 1,
        "rows": [
            _row(1, timer="", action="Discharging to Load", locked=False),
            _row(2, timer="Chg 02:00-02:30 6.0kW cap23%", action="Charging from Grid", locked=False),
        ],
        "history_rows": [],
        "totals": {},
        "plan_soc_q15": {"today": [None] * 96, "tomorrow": [None] * 96},
    }
    for r in fresh["rows"]:
        r["plan_date"] = "2026-07-21"
        r["start"] = f"21-07-2026 {int(r['hour']) + 1:02d}:00"

    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    h1 = next(r for r in merged["rows"] if int(r["hour"]) == 1 and r["plan_date"] == "2026-07-21")
    assert h1["timer_schedule"] == "Chg 01:00-01:30 6.0kW cap25%", h1.get("timer_schedule")
    assert h1["action"] == "Charging from Grid"
    assert h1["hour_labels_locked"] is True


def test_merge_at_hour_start_keeps_empty_timer():
    """Empty current-hour timer stays empty at :00; fresh Chg/Dis does not land."""
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 21, 1, 0, tzinfo=tz)
    existing = {
        "today_date": "2026-07-21",
        "plan_from_hour": 0,
        "rows": [
            _row(1, timer="", action="Discharging to Load", locked=False),
        ],
        "history_rows": [],
        "totals": {},
    }
    for r in existing["rows"]:
        r["plan_date"] = "2026-07-21"
    fresh = {
        "today_date": "2026-07-21",
        "plan_from_hour": 1,
        "rows": [
            _row(1, timer="Chg 01:00-01:45 6.0kW cap22%", action="Charging from Grid", locked=False),
        ],
        "history_rows": [],
        "totals": {},
        "plan_soc_q15": {"today": [None] * 96, "tomorrow": [None] * 96},
    }
    for r in fresh["rows"]:
        r["plan_date"] = "2026-07-21"

    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    h1 = next(r for r in merged["rows"] if int(r["hour"]) == 1)
    assert not str(h1.get("timer_schedule") or "").strip()
    assert h1["hour_labels_locked"] is True


def test_merge_keeps_locked_timer_on_current_hour():
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [_row(8, timer="Dis 08:00-08:45", action="Discharging to Grid", locked=True)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [_row(8, timer="Dis 08:15-08:45", action="Idle", locked=False)],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    cur = merged["rows"][0]
    # Unparseable legacy text (no kW/cap) is left unchanged by clip.
    assert cur["timer_schedule"] == "Dis 08:00-08:45"
    assert cur["action"] == "Discharging to Grid"


def test_merge_updates_future_hour_timer_from_fresh():
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [
            _row(8, timer="Dis 08:00-08:45", locked=True),
            _row(9, timer="", action="Idle"),
        ],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [
            _row(8, timer="Dis 08:15-08:45"),
            _row(9, timer="Dis 09:00-09:45", action="Discharging to Grid"),
        ],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    future = next(r for r in merged["rows"] if r["hour"] == 9)
    assert future["timer_schedule"] == "Dis 09:00-09:45"
    assert future["action"] == "Discharging to Grid"


def test_merge_preserves_imminent_chg_when_fresh_wipes():
    """Before :00, front-load must not erase next-hour Chg from SQLite."""
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 21, 1, 45, tzinfo=tz)
    existing = {
        "today_date": "2026-07-21",
        "plan_from_hour": 1,
        "rows": [
            _row(1, timer="", action="Discharging to Load", locked=True),
            _row(2, timer="Chg 02:00-02:30 6.0kW cap24%", action="Charging from Grid"),
            _row(3, timer="", action="Discharging to Load"),
        ],
        "history_rows": [],
        "totals": {},
    }
    for r in existing["rows"]:
        r["plan_date"] = "2026-07-21"
    fresh = {
        "today_date": "2026-07-21",
        "plan_from_hour": 1,
        "rows": [
            _row(1, timer="", action="Discharging to Load", locked=False),
            _row(2, timer="", action="Discharging to Load"),
            _row(3, timer="Chg 03:00-03:30 6.0kW cap20%", action="Charging from Grid"),
        ],
        "history_rows": [],
        "totals": {},
        "plan_soc_q15": {"today": [None] * 96, "tomorrow": [None] * 96},
    }
    for r in fresh["rows"]:
        r["plan_date"] = "2026-07-21"

    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    h2 = next(r for r in merged["rows"] if int(r["hour"]) == 2)
    assert h2["timer_schedule"] == "Chg 02:00-02:30 6.0kW cap24%"
    assert h2["action"] == "Charging from Grid"


def test_merge_strips_slipped_next_hour_chg_while_current_open():
    """Do not keep Chg 03:00 while Chg 02:00-02:30 is still the open window."""
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 21, 2, 15, tzinfo=tz)
    existing = {
        "today_date": "2026-07-21",
        "plan_from_hour": 2,
        "rows": [
            _row(2, timer="Chg 02:00-02:30 6.0kW cap24%", action="Charging from Grid", locked=True),
            _row(3, timer="", action="Discharging to Load"),
        ],
        "history_rows": [],
        "totals": {},
    }
    for r in existing["rows"]:
        r["plan_date"] = "2026-07-21"
    fresh = {
        "today_date": "2026-07-21",
        "plan_from_hour": 2,
        "rows": [
            _row(2, timer="", action="Discharging to Load"),
            _row(3, timer="Chg 03:00-03:30 6.0kW cap20%", action="Charging from Grid"),
        ],
        "history_rows": [],
        "totals": {},
        "plan_soc_q15": {"today": [None] * 96, "tomorrow": [None] * 96},
    }
    for r in fresh["rows"]:
        r["plan_date"] = "2026-07-21"

    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    h2 = next(r for r in merged["rows"] if int(r["hour"]) == 2)
    h3 = next(r for r in merged["rows"] if int(r["hour"]) == 3)
    assert "Chg 02:00-02:30" in h2["timer_schedule"]
    assert h3["timer_schedule"] == ""
    assert h3["action"] == "Discharging to Load"


def test_merge_preserves_history_rows():
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    hist = _row(7, timer="Dis 07:00-07:45", locked=True)
    hist["history_hour"] = True
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [hist],
        "rows": [_row(8, locked=True)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [_row(7, timer="CHANGED")],
        "rows": [_row(8), _row(9, timer="NEW")],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    assert merged["history_rows"][0]["timer_schedule"] == "Dis 07:00-07:45"


def _hist_hours(plan: dict) -> list[int]:
    return sorted(int(r["hour"]) for r in plan["history_rows"])


def test_merge_promotes_stale_past_rows_mid_hour():
    """Hour left in rows after a missed :00 tick is promoted at :15, not dropped."""
    now = datetime(2026, 7, 7, 8, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 7,
        "history_rows": [],
        # restart happened at 08:0x — hour 7 was never moved to history
        "rows": [_row(7, timer="Dis 07:00-07:45", locked=True), _row(8)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [_row(8), _row(9)],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    assert _hist_hours(merged) == [7]
    hist7 = merged["history_rows"][0]
    assert hist7["timer_schedule"] == "Dis 07:00-07:45"
    assert hist7["history_hour"] is True
    assert sorted(r["hour"] for r in merged["rows"]) == [8, 9]


def test_merge_backfills_history_holes_from_meters():
    """Hours lost earlier (not in existing.rows nor history) come back from fresh meters."""
    now = datetime(2026, 7, 7, 8, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    hist5 = _row(5, action="Discharging to Load")
    hist5["history_hour"] = True
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [hist5],  # hours 6 and 7 were lost
        "rows": [_row(8)],
    }
    meters6 = _row(6, action="Charging from PV")
    meters7 = _row(7, action="Charging from PV")
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [_row(5, timer="CHANGED"), meters6, meters7],
        "rows": [_row(8), _row(9)],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    assert _hist_hours(merged) == [5, 6, 7]
    by_hour = {int(r["hour"]): r for r in merged["history_rows"]}
    # existing history stays immutable, holes healed from meters
    assert by_hour[5]["timer_schedule"] == ""
    assert by_hour[6]["action"] == "Charging from PV"
    assert by_hour[6]["history_hour"] is True


def test_attach_promotes_hour_straddled_by_slow_rebuild():
    """Forced rebuild entered at 12:59:5x, sim finished after 13:00 (rows from 13).

    The hour-12 row must be promoted to history, not silently dropped
    (this is how hours were lost on the Pi).
    """
    now = datetime(2026, 7, 7, 12, 59, 55, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 12,
        "history_rows": [],
        "rows": [_row(12, timer="Dis 12:00-12:45", locked=True), _row(13)],
    }
    result = {
        "today_date": "2026-07-07",
        "plan_from_hour": 13,  # sim crossed into the next hour
        "history_rows": [],
        "rows": [_row(13), _row(14)],
    }
    attach_immutable_history(result, existing, now=now)
    assert [int(r["hour"]) for r in result["history_rows"]] == [12]
    assert result["history_rows"][0]["timer_schedule"] == "Dis 12:00-12:45"
    assert sorted(r["hour"] for r in result["rows"]) == [13, 14]


def test_merge_promotes_hour_straddled_by_slow_sim():
    """Scheduler tick at 13:59:5x whose fresh sim starts at 14 must not drop hour 13."""
    now = datetime(2026, 7, 7, 13, 59, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 13,
        "history_rows": [],
        "rows": [_row(13, timer="Dis 13:00-13:45", locked=True), _row(14)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 14,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [_row(14), _row(15)],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    assert _hist_hours(merged) == [13]
    assert merged["history_rows"][0]["timer_schedule"] == "Dis 13:00-13:45"
    assert sorted(r["hour"] for r in merged["rows"]) == [14, 15]


def test_promoted_history_rows_drop_blended_soc_flag():
    """soc_blended (violet live SOC in UI) belongs only to the in-progress hour.

    A row promoted to history must lose it, otherwise two rows are
    highlighted at once (the old hour in history + the new current hour).
    """
    now = datetime(2026, 7, 7, 9, 0, 1, tzinfo=ZoneInfo("Europe/Warsaw"))
    prev = _row(8, timer="Dis 08:00-08:45", locked=True)
    prev["soc_blended"] = True  # was the current hour a minute ago
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [prev, _row(9)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 9,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [_row(9), _row(10)],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    hist8 = next(r for r in merged["history_rows"] if int(r["hour"]) == 8)
    assert "soc_blended" not in hist8


def test_merge_sets_soc_blended_on_current_hour_even_if_fresh_omits_it():
    """Current-hour violet highlight must not depend on fresh remembering the flag."""
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing_cur = _row(8, timer="Dis 08:00-08:45", action="Discharging to Grid", locked=True)
    existing_cur["soc_blended"] = True
    fresh_cur = _row(8, timer="OPTIMIZER_WIPE", action="Idle")
    fresh_cur.pop("soc_blended", None)
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [existing_cur, _row(9)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [fresh_cur, _row(9, timer="FUTURE_9")],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    cur = next(r for r in merged["rows"] if r["hour"] == 8)
    assert cur["soc_blended"] is True
    assert "soc_blended" not in next(r for r in merged["rows"] if r["hour"] == 9)


def test_merge_keeps_soc_blended_on_current_hour_from_fresh():
    """Mid-hour merge must keep violet SOC highlight from the fresh blended row."""
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing_cur = _row(8, timer="Dis 08:00-08:45", action="Discharging to Grid", locked=True)
    # SQLite row lost the flag (regression path) while sim still marks blend.
    existing_cur.pop("soc_blended", None)
    fresh_cur = _row(8, timer="OPTIMIZER_WIPE", action="Idle")
    fresh_cur["soc_blended"] = True
    fresh_cur["q15"] = [
        {"quarter": q, "production": 0.3, "consumption": 0.1, "soc": 48.0 - q,
         "battery": -0.05, "grid_import": 0.0, "grid_export": 0.0, "from_actual": False}
        for q in range(4)
    ]
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [existing_cur, _row(9)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [fresh_cur, _row(9, timer="FUTURE_9")],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    cur = next(r for r in merged["rows"] if r["hour"] == 8)
    assert cur["soc_blended"] is True
    assert cur["timer_schedule"] == "Dis 08:00-08:45"
    assert "soc_blended" not in next(r for r in merged["rows"] if r["hour"] == 9)


def test_merge_keeps_soc_blended_at_hour_start():
    """At :00 the new current hour still gets the violet live-SOC highlight."""
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 9,
        "history_rows": [],
        "rows": [_row(9, locked=True)],
    }
    fresh_cur = _row(9)
    fresh_cur["soc_blended"] = True
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 9,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [fresh_cur],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    cur = next(r for r in merged["rows"] if r["hour"] == 9)
    assert cur["soc_blended"] is True


def test_stale_blended_flags_in_stored_history_are_healed():
    """Rows already written with soc_blended by older code lose it on next merge."""
    now = datetime(2026, 7, 7, 9, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    bad = _row(7)
    bad["history_hour"] = True
    bad["soc_blended"] = True  # legacy promotion kept the live flag
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 9,
        "history_rows": [bad],
        "rows": [_row(9)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 9,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [_row(9), _row(10)],
    }
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    assert all("soc_blended" not in r for r in merged["history_rows"])

    result = {
        "today_date": "2026-07-07",
        "plan_from_hour": 9,
        "history_rows": [],
        "rows": [_row(9), _row(10)],
    }
    attach_immutable_history(result, existing, now=now)
    assert all("soc_blended" not in r for r in result["history_rows"])


def test_attach_immutable_history_backfills_holes():
    """Full rebuild heals history holes from the fresh sim's meter rows."""
    now = datetime(2026, 7, 7, 8, 20, tzinfo=ZoneInfo("Europe/Warsaw"))
    hist5 = _row(5, timer="Dis 05:00-05:45", locked=True)
    hist5["history_hour"] = True
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [hist5],  # hours 6 and 7 lost earlier
        "rows": [_row(7, action="Charging from PV"), _row(8)],
    }
    result = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [_row(5, timer="METERS"), _row(6, action="Charging from PV")],
        "rows": [_row(8), _row(9)],
    }
    attach_immutable_history(result, existing, now=now)
    by_hour = {int(r["hour"]): r for r in result["history_rows"]}
    assert sorted(by_hour) == [5, 6, 7]
    assert by_hour[5]["timer_schedule"] == "Dis 05:00-05:45"  # SQLite wins
    assert by_hour[7]["action"] == "Charging from PV"  # promoted from rows
    assert by_hour[6]["history_hour"] is True  # backfilled from meters


# ---------------------------------------------------------------------------
# _apply_actual_quarter_if_needed
# ---------------------------------------------------------------------------

def _make_series_10min(
    *,
    pv_kwh_per_q: float = 0.5,
    load_kwh_per_q: float = 0.2,
    bat_kwh_per_q: float = 0.0,
    grid_export_kwh_per_q: float = 0.0,
    grid_import_kwh_per_q: float = 0.0,
    hour: int = 8,
) -> dict:
    """Minimal series_10min for the given hour (3 × 10-min slots = 1 q15)."""
    size = 24 * 6  # 144 slots total, 6 per hour
    pv = [0.0] * size
    load = [0.0] * size
    bat_charge = [0.0] * size
    bat_discharge = [0.0] * size
    grid_sell = [0.0] * size
    grid_buy = [0.0] * size
    base = hour * 6
    # spread q0 over first 3 10-min slots
    for i in range(3):
        pv[base + i] = pv_kwh_per_q / 3
        load[base + i] = load_kwh_per_q / 3
        bat_charge[base + i] = max(0.0, bat_kwh_per_q) / 3
        bat_discharge[base + i] = max(0.0, -bat_kwh_per_q) / 3
        grid_sell[base + i] = grid_export_kwh_per_q / 3
        grid_buy[base + i] = -grid_import_kwh_per_q / 3
    return {
        "pv": pv, "load": load,
        "bat_charge": bat_charge, "bat_discharge": bat_discharge,
        "grid_sell": grid_sell, "grid_buy": grid_buy,
    }


def test_apply_actual_quarter_writes_from_actual_true():
    """_apply_actual_quarter_if_needed writes q15[0] with from_actual=True."""
    cfg = _cfg()
    row = _row(8)
    series = _make_series_10min(pv_kwh_per_q=0.5, load_kwh_per_q=0.2, hour=8)
    changed = _apply_actual_quarter_if_needed(
        row, 8, 0,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
    )
    assert changed is True
    q15 = row["q15"]
    assert q15[0]["from_actual"] is True
    assert q15[0]["quarter"] == 0
    assert q15[1]["from_actual"] is False


def test_apply_actual_quarter_idempotent():
    """Second call for same quarter does nothing (slot already from_actual)."""
    cfg = _cfg()
    row = _row(8)
    series = _make_series_10min(hour=8)
    _apply_actual_quarter_if_needed(row, 8, 0, series_10min=series,
                                    today_hourly=None, cfg=cfg, battery_cap=20.0)
    first_val = row["q15"][0]["production"]
    # Modify series — second call must NOT change anything
    series["pv"] = [999.0] * len(series["pv"])
    changed = _apply_actual_quarter_if_needed(row, 8, 0, series_10min=series,
                                              today_hourly=None, cfg=cfg, battery_cap=20.0)
    assert changed is False
    assert row["q15"][0]["production"] == first_val


def test_apply_actual_quarter_sequential():
    """At :30 q15[0] already actual; q15[1] is written; q15[2,3] untouched."""
    cfg = _cfg()
    row = _row(8)
    series = _make_series_10min(pv_kwh_per_q=0.4, load_kwh_per_q=0.1, hour=8)

    # Simulate :15 tick — write q0
    _apply_actual_quarter_if_needed(row, 8, 0, series_10min=series,
                                    today_hourly=None, cfg=cfg, battery_cap=20.0)
    assert row["q15"][0]["from_actual"] is True

    # Simulate :30 tick — write q1
    _apply_actual_quarter_if_needed(row, 8, 1, series_10min=series,
                                    today_hourly=None, cfg=cfg, battery_cap=20.0)
    assert row["q15"][1]["from_actual"] is True
    assert row["q15"][2]["from_actual"] is False
    assert row["q15"][3]["from_actual"] is False


# ---------------------------------------------------------------------------
# _merge_current_hour_q15
# ---------------------------------------------------------------------------

def _fresh_q15_row(hour: int, *, soc: float = 55.0) -> dict:
    q15 = [
        {"quarter": q, "production": 0.3, "consumption": 0.1, "soc": soc,
         "battery": -0.1, "grid_import": 0.0, "grid_export": 0.1, "from_actual": False}
        for q in range(4)
    ]
    row = _row(hour)
    row["q15"] = q15
    return row


def test_merge_q15_keeps_actual_and_takes_fresh_for_future():
    """At :30: q0–q1 freeze-ready stay; q2+ from fresh."""
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))

    existing = _row(8)
    existing["q15"][0] = {
        "quarter": 0, "production": 0.5, "consumption": 0.2, "soc": 50.0,
        "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0, "from_actual": True,
    }
    series = _make_series_10min(pv_kwh_per_q=0.6, load_kwh_per_q=0.25, hour=8)
    fresh_row = _fresh_q15_row(8, soc=52.0)

    _merge_current_hour_q15(
        existing,
        now=now, hour=8,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
        fresh_row=fresh_row,
    )

    q15 = existing["q15"]
    assert q15[0]["from_actual"] is True
    assert q15[0]["production"] == 0.5
    assert q15[1]["from_actual"] is True
    assert q15[2]["from_actual"] is False
    assert q15[2]["production"] == 0.3
    assert q15[3]["from_actual"] is False
    assert q15[3]["production"] == 0.3
    assert float(existing["soc"]) == 52.0


def test_merge_q15_at_hour_start_no_actuals():
    """:05 (before first tick) — no Influx data yet, all from SQLite (:00 values)."""
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 5, tzinfo=ZoneInfo("Europe/Warsaw"))

    existing = _row(8)
    series = _make_series_10min(hour=8)

    _merge_current_hour_q15(
        existing,
        now=now, hour=8,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
    )
    q15 = existing["q15"]
    assert all(not s["from_actual"] for s in q15)


# ---------------------------------------------------------------------------
# End-to-end merge_incremental_plan with actuals
# ---------------------------------------------------------------------------

def test_merge_incremental_at_30_quarter_pattern():
    """At :30: freeze q0–q1 (:00-:30); q2+ from fresh optimizer."""
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Europe/Warsaw"))

    cur_row = _row(8, timer="Dis 08:00-08:45", action="Discharging to Grid", locked=True)
    cur_row["q15"][0] = {
        "quarter": 0, "production": 0.5, "consumption": 0.2, "soc": 50.0,
        "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0, "from_actual": True,
    }
    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [cur_row, _row(9)],
    }
    fresh_cur = _row(8, timer="OPTIMIZER_NEW", action="Idle")
    fresh_cur["q15"] = [
        {"quarter": q, "production": 0.3, "consumption": 0.1, "soc": 49.0,
         "battery": -0.05, "grid_import": 0.0, "grid_export": 0.0, "from_actual": False}
        for q in range(4)
    ]
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [fresh_cur, _row(9, timer="FUTURE_9")],
    }
    series = _make_series_10min(pv_kwh_per_q=0.45, load_kwh_per_q=0.18, hour=8)
    metrics = {"series_10min": series}

    merged = merge_incremental_plan(existing, fresh, now=now, metrics=metrics, cfg=cfg)
    cur = next(r for r in merged["rows"] if r["hour"] == 8)

    # timer/action locked — not overwritten by optimizer
    assert cur["timer_schedule"] == "Dis 08:00-08:45"
    assert cur["action"] == "Discharging to Grid"
    # q0 — freeze-ready at :30
    assert cur["q15"][0]["from_actual"] is True
    assert cur["q15"][0]["production"] == 0.5
    # q1 — freeze-ready at :30/:31
    assert cur["q15"][1]["from_actual"] is True
    # q2, q3 — from fresh optimizer
    assert cur["q15"][2]["from_actual"] is False
    assert cur["q15"][2]["production"] == 0.3
    assert cur["q15"][3]["production"] == 0.3
    # future hour gets optimizer value
    future = next(r for r in merged["rows"] if r["hour"] == 9)
    assert future["timer_schedule"] == "FUTURE_9"


def test_datafix_before_first_quarter_keeps_eoh_soc():
    """:00–:14 Reset must not replace end-of-hour SOC with live meter."""
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 5, tzinfo=ZoneInfo("Europe/Warsaw"))
    hist = _row(7, timer="Dis 07:00-07:45", locked=True)
    hist["history_hour"] = True
    hist["soc"] = 40.0

    cur = _row(8, timer="Dis 08:00-08:45", action="Discharging to Grid", locked=True)
    # :00 end-of-hour projection (~22.4 style); live meter is higher mid-hour.
    for q, slot in enumerate(cur["q15"]):
        slot["soc"] = 24.0 - (q + 1) * 0.4  # → EOH 22.4
        slot["battery"] = -0.08
        slot["from_actual"] = False
    cur["soc"] = 22.4
    cur["bat_discharge"] = 0.32

    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [hist],
        "rows": [cur, _row(9)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "live_soc_pct": 24.0,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [_row(8), _row(9, timer="FUTURE_9")],
    }
    metrics = {"sa_online": True, "battery_soc": 24.0}

    merged = merge_incremental_plan(existing, fresh, now=now, metrics=metrics, cfg=cfg)
    assert merged["history_rows"][0]["timer_schedule"] == "Dis 07:00-07:45"
    assert merged["history_rows"][0]["soc"] == 40.0

    out = next(r for r in merged["rows"] if r["hour"] == 8)
    assert out["timer_schedule"] == "Dis 08:00-08:45"
    assert out["action"] == "Discharging to Grid"
    # Hour column stays end-of-hour, not live 24%.
    assert float(out["soc"]) == pytest.approx(22.4, abs=0.15)
    assert float(out["soc"]) != pytest.approx(24.0, abs=0.05)


def test_datafix_after_quarter_rechains_from_hour_start_not_live():
    """After :15, EOH stays on the :00 chain — not replaced by live meter."""
    import copy

    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 21, tzinfo=ZoneInfo("Europe/Warsaw"))
    cur = _row(8, timer="Dis 08:00-08:45", action="Discharging to Load", locked=True)
    starts = [25.0, 24.1, 23.6, 23.0, 22.4]
    for q, slot in enumerate(cur["q15"]):
        slot["soc"] = starts[q + 1]
        slot["battery"] = round((starts[q + 1] - starts[q]) / 100.0 * 20.0, 4)
        slot["from_actual"] = q == 0
    cur["soc"] = 22.4

    existing = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "history_rows": [],
        "rows": [cur, _row(9)],
    }
    fresh = {
        "today_date": "2026-07-07",
        "plan_from_hour": 8,
        "live_soc_pct": 24.0,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [copy.deepcopy(cur), _row(9, timer="FUTURE_9")],
    }
    metrics = {
        "sa_online": True,
        "battery_soc": 24.0,
        "today_hourly": {"soc": [None] * 24},
        "series_10min": None,
    }
    merged = merge_incremental_plan(existing, fresh, now=now, metrics=metrics, cfg=cfg)
    out = next(r for r in merged["rows"] if r["hour"] == 8)
    assert float(out["soc"]) == pytest.approx(22.4, abs=0.15)
    assert float(out["soc"]) != pytest.approx(24.0, abs=0.05)


def test_quarter_tick_now_floors_to_boundary():
    tz = ZoneInfo("Europe/Warsaw")
    assert quarter_tick_now(datetime(2026, 8, 1, 20, 0, 40, tzinfo=tz)).minute == 0
    assert quarter_tick_now(datetime(2026, 8, 1, 20, 16, 5, tzinfo=tz)).minute == 15
    assert quarter_tick_now(datetime(2026, 8, 1, 20, 31, 2, tzinfo=tz)).minute == 30
    assert quarter_tick_now(datetime(2026, 8, 1, 20, 47, tzinfo=tz)).minute == 45


def test_late_promote_finalizes_q3_from_influx():
    """Missed exact :00 still Influx-updates then freezes q3 on late promote."""
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 8, 1, 20, 15, tzinfo=tz)
    cfg = _cfg()

    def slot(q, fa=False, gi=0.2, soc=40.0):
        return {
            "quarter": q, "production": 0.0, "consumption": 0.1, "soc": soc,
            "battery": 0.05, "grid_import": gi, "grid_export": 0.0,
            "from_actual": fa,
        }

    prev_q15 = [slot(q, fa=(q < 3), gi=1.0 if q < 3 else 0.2, soc=30 + q) for q in range(4)]
    prev = _row(19, timer="Dis 19:00-19:45", action="Discharging to Grid", locked=True)
    prev["plan_date"] = "2026-08-01"
    prev["q15"] = prev_q15
    cur = _row(20, locked=True)
    cur["plan_date"] = "2026-08-01"
    for r in (prev, cur):
        r["start"] = f"01-08-2026 {int(r['hour']) + 1:02d}:00"

    series = _make_series_10min(
        hour=19, pv_kwh_per_q=0.0, load_kwh_per_q=0.2,
        grid_import_kwh_per_q=1.5,
    )
    # Fill all six 10-min slots so q3 has Influx energy (helper only seeds q0 window).
    base = 19 * 6
    for i in range(6):
        series["grid_buy"][base + i] = -9.0
        series["load"][base + i] = 0.2

    existing = {
        "today_date": "2026-08-01",
        "plan_from_hour": 19,
        "history_rows": [],
        "rows": [prev, cur],
    }
    fresh_cur = _row(20)
    fresh_cur["plan_date"] = "2026-08-01"
    fresh_next = _row(21)
    fresh_next["plan_date"] = "2026-08-01"
    fresh = {
        "today_date": "2026-08-01",
        "plan_from_hour": 20,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [fresh_cur, fresh_next],
        "live_soc_pct": 40.0,
    }
    metrics = {
        "series_10min": series,
        "today_hourly": {"soc": [None] * 24},
        "sa_online": True,
    }
    merged = merge_incremental_plan(existing, fresh, now=now, metrics=metrics, cfg=cfg)
    h19 = next(r for r in merged["history_rows"] if int(r["hour"]) == 19)
    assert all(s["from_actual"] for s in h19["q15"]), h19["q15"]
    assert float(h19["q15"][3]["grid_import"]) > 0.5


def test_datafix_does_not_rewrite_frozen_earlier_quarter():
    """At :30, already-frozen q0 stays; q1 freezes from Influx."""
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 7, 8, 30, tzinfo=tz)
    cfg = _cfg()
    row = _row(8, locked=True)
    row["q15"][0] = {
        "quarter": 0, "production": 0.4, "consumption": 0.1, "soc": 48.0,
        "battery": 0.2, "grid_import": 1.23, "grid_export": 0.0, "from_actual": True,
    }
    series = _make_series_10min(
        hour=8, grid_import_kwh_per_q=9.0, pv_kwh_per_q=0.5, load_kwh_per_q=0.2,
    )
    datafix_completed_quarters_from_live(
        row, hour=8, now=now, series_10min=series, today_hourly=None,
        cfg=cfg, battery_cap=20.0, live_soc_kwh=10.0,
    )
    assert row["q15"][0]["from_actual"] is True
    assert row["q15"][0]["grid_import"] == 1.23
    assert row["q15"][1]["from_actual"] is True


def test_write_guard_absorbs_newly_frozen_q3():
    """Past-hour immutability still allows Influx-finalized q3 into history."""
    from src.plan_cache_merge import guard_future_quarters_on_write

    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 8, 1, 20, 15, tzinfo=tz)
    hist = _row(19, timer="Dis 19:00-19:45", locked=True)
    hist["plan_date"] = "2026-08-01"
    hist["history_hour"] = True
    for q, slot in enumerate(hist["q15"]):
        slot["from_actual"] = q < 3
        slot["grid_import"] = 1.0 if q < 3 else 0.2
    incoming_hist = copy.deepcopy(hist)
    incoming_hist["q15"][3] = {
        "quarter": 3, "production": 0.0, "consumption": 0.1, "soc": 35.0,
        "battery": 0.1, "grid_import": 1.4, "grid_export": 0.0, "from_actual": True,
    }
    existing = {
        "today_date": "2026-08-01",
        "plan_from_hour": 20,
        "history_rows": [hist],
        "rows": [_row(20)],
    }
    existing["rows"][0]["plan_date"] = "2026-08-01"
    incoming = {
        "today_date": "2026-08-01",
        "plan_from_hour": 20,
        "history_rows": [incoming_hist],
        "rows": [_row(20), _row(21)],
        "delta_kwh": 0.0,
    }
    for r in incoming["rows"]:
        r["plan_date"] = "2026-08-01"
    guarded = guard_future_quarters_on_write(incoming, existing, now=now)
    h19 = next(r for r in guarded["history_rows"] if int(r["hour"]) == 19)
    assert h19["timer_schedule"] == "Dis 19:00-19:45"
    assert all(s["from_actual"] for s in h19["q15"])
    assert float(h19["q15"][3]["grid_import"]) == pytest.approx(1.4)


def test_write_guard_fills_missing_rce_on_frozen_hour():
    """Frozen past hour keeps meters; RCE holes fill from this tick's incoming row."""
    from src.plan_cache_merge import guard_future_quarters_on_write

    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 8, 22, 20, 15, tzinfo=tz)
    hist = _row(19, locked=True)
    hist["plan_date"] = "2026-08-22"
    hist["history_hour"] = True
    hist["rce_price"] = None
    hist["rce_q15"] = [None, None, None, None]
    incoming_hist = copy.deepcopy(hist)
    incoming_hist["rce_q15"] = [0.6795, 0.7344, 0.805, 0.8473]
    incoming_hist["rce_price"] = 0.7666
    existing = {
        "today_date": "2026-08-22",
        "plan_from_hour": 20,
        "history_rows": [hist],
        "rows": [_row(20)],
    }
    existing["rows"][0]["plan_date"] = "2026-08-22"
    incoming = {
        "today_date": "2026-08-22",
        "plan_from_hour": 20,
        "history_rows": [incoming_hist],
        "rows": [_row(20)],
        "delta_kwh": 0.0,
    }
    incoming["rows"][0]["plan_date"] = "2026-08-22"
    guarded = guard_future_quarters_on_write(incoming, existing, now=now)
    h19 = next(r for r in guarded["history_rows"] if int(r["hour"]) == 19)
    assert h19["rce_q15"] == [0.6795, 0.7344, 0.805, 0.8473]
    assert h19["rce_price"] == pytest.approx(0.7666)


def test_copy_future_keeps_rce_when_fresh_is_empty():
    from src.plan_cache_merge import _copy_future_row, _keep_rce_if_incoming_empty

    existing = _row(21)
    existing["rce_q15"] = [0.7, 0.71, 0.72, 0.73]
    existing["rce_price"] = 0.715
    fresh = copy.deepcopy(existing)
    fresh["rce_q15"] = [None, None, None, None]
    fresh["rce_price"] = None
    dst = copy.deepcopy(existing)
    _copy_future_row(dst, fresh)
    _keep_rce_if_incoming_empty(dst, existing)
    assert dst["rce_q15"] == [0.7, 0.71, 0.72, 0.73]
    assert dst["rce_price"] == pytest.approx(0.715)


def test_merge_fills_rce_hole_on_history_from_fresh():
    """Frozen history keeps meters; empty RCE fills from this tick's history row."""
    now = datetime(2026, 8, 22, 20, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    hist = _row(19, timer="Dis 19:00-19:45", locked=True)
    hist["plan_date"] = "2026-08-22"
    hist["start"] = "22-08-2026 20:00"
    hist["history_hour"] = True
    hist["rce_price"] = None
    hist["rce_q15"] = [None, None, None, None]
    hist["grid_export"] = 1.2
    fresh_hist = copy.deepcopy(hist)
    fresh_hist["timer_schedule"] = "CHANGED"
    fresh_hist["rce_q15"] = [0.6795, 0.7344, 0.805, 0.8473]
    fresh_hist["rce_price"] = 0.7666
    existing = {
        "today_date": "2026-08-22",
        "plan_from_hour": 20,
        "history_rows": [hist],
        "rows": [_row(20, locked=True)],
    }
    existing["rows"][0]["plan_date"] = "2026-08-22"
    existing["rows"][0]["start"] = "22-08-2026 21:00"
    fresh = {
        "today_date": "2026-08-22",
        "plan_from_hour": 20,
        "delta_kwh": 0.0,
        "history_rows": [fresh_hist],
        "rows": [_row(20), _row(21)],
    }
    for r in fresh["rows"]:
        r["plan_date"] = "2026-08-22"
        r["start"] = f"22-08-2026 {int(r['hour']) + 1:02d}:00"
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    h19 = next(r for r in merged["history_rows"] if int(r["hour"]) == 19)
    assert h19["timer_schedule"] == "Dis 19:00-19:45"
    assert h19["rce_q15"] == [0.6795, 0.7344, 0.805, 0.8473]
    assert h19["rce_price"] == pytest.approx(0.7666)


def test_merge_fills_rce_on_current_hour_from_fresh():
    now = datetime(2026, 8, 22, 19, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    cur = _row(19, timer="Dis 19:00-19:45", locked=True)
    cur["plan_date"] = "2026-08-22"
    cur["start"] = "22-08-2026 20:00"
    cur["rce_price"] = None
    cur["rce_q15"] = [None, None, None, None]
    fresh_cur = copy.deepcopy(cur)
    fresh_cur["timer_schedule"] = ""
    fresh_cur["rce_q15"] = [0.6795, 0.7344, 0.805, 0.8473]
    fresh_cur["rce_price"] = 0.7666
    existing = {
        "today_date": "2026-08-22",
        "plan_from_hour": 19,
        "history_rows": [],
        "rows": [cur],
    }
    fresh = {
        "today_date": "2026-08-22",
        "plan_from_hour": 19,
        "delta_kwh": 0.0,
        "history_rows": [],
        "rows": [fresh_cur, _row(20)],
    }
    fresh["rows"][1]["plan_date"] = "2026-08-22"
    fresh["rows"][1]["start"] = "22-08-2026 21:00"
    merged = merge_incremental_plan(existing, fresh, now=now, cfg=_cfg())
    h19 = next(r for r in merged["rows"] if int(r["hour"]) == 19)
    assert h19["timer_schedule"] == "Dis 19:00-19:45"
    assert h19["rce_q15"] == [0.6795, 0.7344, 0.805, 0.8473]
    assert h19["rce_price"] == pytest.approx(0.7666)


def test_apply_actual_quarter_keeps_planned_rce():
    cfg = _cfg()
    row = _row(8)
    row["rce_q15"] = [0.42, 0.43, 0.44, 0.45]
    row["q15"][0]["rce"] = 0.42
    series = _make_series_10min(pv_kwh_per_q=0.5, load_kwh_per_q=0.2, hour=8)
    _apply_actual_quarter_if_needed(
        row, 8, 0,
        series_10min=series,
        today_hourly=None,
        cfg=cfg,
        battery_cap=20.0,
    )
    assert row["q15"][0]["from_actual"] is True
    assert row["q15"][0]["rce"] == pytest.approx(0.42)

