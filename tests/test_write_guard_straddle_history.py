"""write_plan guard uses the job tick hour; previous hour promotes at :00."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.plan_cache_merge import guard_future_quarters_on_write

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-08-12"


def _row(hour: int, *, timer: str, in_history: bool = False) -> dict:
    return {
        "plan_date": TODAY,
        "hour": hour,
        "start": f"12-08-2026 {(hour + 1) % 24:02d}:00",
        "production": 0.0,
        "consumption": 1.0,
        "battery": -5.0,
        "bat_charge": 0.0,
        "bat_discharge": 5.0,
        "grid_import": 0.0,
        "grid_export": 4.0,
        "soc": 80.0,
        "timer_schedule": timer,
        "action": "Discharging to Grid and Load",
        "hour_labels_locked": True,
        "history_hour": in_history,
        "q15": [
            {
                "quarter": q,
                "production": 0.0,
                "consumption": 0.25,
                "soc": 80.0 - q,
                "battery": -1.25,
                "grid_import": 0.0,
                "grid_export": 1.0,
                "from_actual": False,
            }
            for q in range(4)
        ],
    }


def test_write_guard_tick_hour_keeps_current_not_plan_from():
    """Incoming plan_from=20 at 19:59 must not promote H19; tick hour is current."""
    existing = {
        "today_date": TODAY,
        "plan_from_hour": 19,
        "history_rows": [_row(h, timer="", in_history=True) for h in range(19)],
        "rows": [
            _row(19, timer="Dis 19:15-20:00 7.0kW cap37%"),
            _row(20, timer="Dis 20:00-21:00 8.0kW cap35%"),
        ],
    }
    incoming = {
        "today_date": TODAY,
        "plan_from_hour": 20,
        "history_rows": existing["history_rows"] + [
            _row(19, timer="Dis 19:15-20:00 7.0kW cap37%", in_history=True),
        ],
        "rows": [
            _row(20, timer="Dis 20:00-21:00 8.0kW cap35%"),
            _row(21, timer="Dis 21:00-22:00 8.0kW cap33%"),
        ],
    }
    now = datetime(2026, 8, 12, 19, 59, 50, tzinfo=TZ)
    out = guard_future_quarters_on_write(incoming, existing, now=now)
    hist_hours = sorted(
        int(r["hour"]) for r in out["history_rows"]
        if str(r.get("plan_date")) == TODAY
    )
    assert 19 not in hist_hours, f"H19 promoted early: {hist_hours}"
    h19 = next(r for r in out["rows"] if int(r["hour"]) == 19)
    assert str(h19.get("timer_schedule") or "").startswith("Dis 19:15")
    assert int(out.get("plan_from_hour")) == 19


def test_write_guard_at_hour_start_promotes_previous_from_existing_rows():
    """At 20:00, H19 leaves live rows into history from existing.rows."""
    existing = {
        "today_date": TODAY,
        "plan_from_hour": 19,
        "history_rows": [_row(h, timer="", in_history=True) for h in range(19)],
        "rows": [
            _row(19, timer="Dis 19:15-20:00 7.0kW cap37%"),
            _row(20, timer="Dis 20:00-21:00 8.0kW cap35%"),
        ],
    }
    incoming = {
        "today_date": TODAY,
        "plan_from_hour": 20,
        "history_rows": list(existing["history_rows"]),
        "rows": [_row(20, timer="Dis 20:00-21:00 8.0kW cap35%")],
    }
    now = datetime(2026, 8, 12, 20, 0, 2, tzinfo=TZ)
    out = guard_future_quarters_on_write(incoming, existing, now=now)
    h19 = next(r for r in out["history_rows"] if int(r["hour"]) == 19)
    assert "Dis 19:15" in str(h19.get("timer_schedule") or "")
    assert h19.get("hour_labels_locked") is True
    assert int(out.get("plan_from_hour")) == 20
