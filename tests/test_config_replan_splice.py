"""Config replan splices from the next q15 boundary."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.plan_cache_merge import next_replan_boundary, splice_replan_from_quarter

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-07-20"
TOMORROW = "2026-07-21"


def _q15(*, old: bool) -> list[dict]:
    marker = 1.0 if old else 9.0
    return [
        {
            "quarter": q,
            "production": marker,
            "consumption": 0.1,
            "soc": 50.0,
            "battery": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "from_actual": False,
        }
        for q in range(4)
    ]


def _row(date: str, hour: int, *, marker: str, old_q15: bool = True) -> dict:
    return {
        "plan_date": date,
        "hour": hour,
        "start": f"x-{hour}",
        "timer_schedule": f"KEEP {hour}" if marker.startswith("OLD") else f"NEW {hour}",
        "action": marker,
        "hour_labels_locked": marker.startswith("OLD"),
        "q15": _q15(old=old_q15),
        "production": 0.0,
        "consumption": 0.0,
        "battery": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "soc": 50.0,
    }


def test_next_replan_boundary_examples():
    assert next_replan_boundary(datetime(2026, 7, 20, 22, 44, tzinfo=TZ)) == (TODAY, 22, 3)
    assert next_replan_boundary(datetime(2026, 7, 20, 22, 48, tzinfo=TZ)) == (TODAY, 23, 0)
    assert next_replan_boundary(datetime(2026, 7, 20, 22, 45, 0, tzinfo=TZ)) == (TODAY, 22, 3)
    assert next_replan_boundary(datetime(2026, 7, 20, 23, 50, tzinfo=TZ)) == (TOMORROW, 0, 0)


def test_splice_at_22_44_rewrites_from_22_45():
    now = datetime(2026, 7, 20, 22, 44, tzinfo=TZ)
    existing = {
        "today_date": TODAY,
        "plan_from_hour": 22,
        "history_rows": [_row(TODAY, 20, marker="OLD-H20")],
        "rows": [
            _row(TODAY, 22, marker="OLD-22", old_q15=True),
            _row(TODAY, 23, marker="OLD-23", old_q15=True),
            _row(TOMORROW, 0, marker="OLD-T0", old_q15=True),
        ],
    }
    fresh = {
        "today_date": TODAY,
        "plan_from_hour": 22,
        "history_rows": [_row(TODAY, 20, marker="HACK-H20")],
        "rows": [
            _row(TODAY, 22, marker="NEW-22", old_q15=False),
            _row(TODAY, 23, marker="NEW-23", old_q15=False),
            _row(TOMORROW, 0, marker="NEW-T0", old_q15=False),
        ],
        "delta_kwh": 1.0,
    }

    out = splice_replan_from_quarter(
        existing, fresh, now=now, boundary=next_replan_boundary(now),
    )

    assert out["history_rows"][0]["action"] == "OLD-H20"
    cur = next(r for r in out["rows"] if r["hour"] == 22 and r["plan_date"] == TODAY)
    assert cur["timer_schedule"] == "KEEP 22"
    assert cur["q15"][0]["production"] == 1.0
    assert cur["q15"][1]["production"] == 1.0
    assert cur["q15"][2]["production"] == 1.0  # in-progress 22:30-45 kept
    assert cur["q15"][3]["production"] == 9.0  # from 22:45 fresh
    assert next(r for r in out["rows"] if r["hour"] == 23)["action"] == "NEW-23"
    assert next(r for r in out["rows"] if r["plan_date"] == TOMORROW)["action"] == "NEW-T0"
