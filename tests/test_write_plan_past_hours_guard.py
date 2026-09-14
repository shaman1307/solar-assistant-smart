"""write_plan is the sole SQLite gate: only future quarters may change."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.plan_cache_merge import guard_future_quarters_on_write
from src.sqlite_store import delete_plan, read_plan, write_plan

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-07-20"


@pytest.fixture(autouse=True)
def _clear_plan():
    delete_plan()
    yield
    delete_plan()


def _now(hour: int = 14, minute: int = 30) -> datetime:
    return datetime(2026, 7, 20, hour, minute, 0, tzinfo=TZ)


def _q15(*, marker: float, from_actual: bool = False) -> list[dict]:
    return [
        {
            "quarter": q,
            "production": marker,
            "consumption": 0.1,
            "soc": 50.0,
            "battery": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "from_actual": from_actual and q < 2,
        }
        for q in range(4)
    ]


def _hist_row(hour: int, *, marker: str) -> dict:
    return {
        "plan_date": TODAY,
        "hour": hour,
        "start": f"20-07-2026 {(hour + 1) % 24:02d}:00",
        "timer_schedule": f"Dis {hour:02d}:00-{hour:02d}:45 5kW cap16%",
        "action": marker,
        "history_hour": True,
        "soc": 42.0,
        "production": 1.0,
        "consumption": 0.5,
        "battery": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "q15": _q15(marker=float(hour)),
    }


def _live_row(hour: int, *, marker: str, q_marker: float, locked: bool = False) -> dict:
    return {
        "plan_date": TODAY,
        "hour": hour,
        "start": f"20-07-2026 {(hour + 1) % 24:02d}:00",
        "timer_schedule": f"Dis {hour:02d}:00-{hour:02d}:45 5kW cap16%" if locked else "",
        "action": marker,
        "hour_labels_locked": locked,
        "soc": 50.0,
        "production": 0.0,
        "consumption": 0.0,
        "battery": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "q15": _q15(marker=q_marker),
    }


def test_guard_keeps_history_and_rewrites_future_hours():
    existing = {
        "today_date": TODAY,
        "plan_from_hour": 14,
        "history_rows": [_hist_row(h, marker=f"LOCKED-{h}") for h in range(14)],
        "rows": [_live_row(h, marker=f"LIVE-{h}", q_marker=1.0, locked=(h == 14)) for h in range(14, 24)],
    }
    incoming = {
        "today_date": TODAY,
        "plan_from_hour": 14,
        "history_rows": [_hist_row(h, marker=f"REWRITTEN-{h}") for h in range(14)],
        "rows": [
            *[_live_row(h, marker=f"PAST-IN-ROWS-{h}", q_marker=9.0) for h in range(10, 14)],
            *[_live_row(h, marker=f"NEW-{h}", q_marker=2.0) for h in range(14, 24)],
        ],
        "delta_kwh": 99.0,
    }

    guarded = guard_future_quarters_on_write(incoming, existing, now=_now(14, 30))

    assert len(guarded["history_rows"]) == 14
    for h, row in enumerate(guarded["history_rows"]):
        assert row["action"] == f"LOCKED-{h}"
    cur = next(r for r in guarded["rows"] if int(r["hour"]) == 14)
    # :30 → q0–q1 freeze-ready; q2–q3 from incoming
    assert cur["q15"][0]["production"] == 1.0
    assert cur["q15"][1]["production"] == 1.0
    assert cur["q15"][2]["production"] == 2.0
    assert cur["q15"][3]["production"] == 2.0
    assert cur["timer_schedule"].startswith("Dis 14:00")
    fut = next(r for r in guarded["rows"] if int(r["hour"]) == 15)
    assert fut["action"] == "NEW-15"
    assert fut["q15"][0]["production"] == 2.0
    assert guarded["delta_kwh"] == 99.0


def test_write_plan_blocks_past_hour_overwrite():
    now = _now(14, 30)
    seed = {
        "today_date": TODAY,
        "plan_from_hour": 14,
        "computed_at": "first",
        "history_rows": [_hist_row(10, marker="ORIGINAL-10")],
        "rows": [_live_row(14, marker="CUR", q_marker=1.0, locked=True)],
        "forecast": {},
    }
    write_plan(seed, now=now)

    rewrite = {
        "today_date": TODAY,
        "plan_from_hour": 14,
        "computed_at": "second",
        "history_rows": [_hist_row(10, marker="HACKED-10")],
        "rows": [_live_row(14, marker="CUR2", q_marker=9.0)],
        "forecast": {"meta": {"x": 1}},
    }
    write_plan(rewrite, now=now)

    stored = read_plan()
    assert stored is not None
    assert stored["computed_at"] == "second"
    hist10 = next(r for r in stored["history_rows"] if int(r["hour"]) == 10)
    assert hist10["action"] == "ORIGINAL-10"
    cur = next(r for r in stored["rows"] if int(r["hour"]) == 14)
    assert cur["q15"][0]["production"] == 1.0
    assert cur["q15"][1]["production"] == 1.0
    assert cur["q15"][2]["production"] == 9.0
    assert cur["timer_schedule"].startswith("Dis 14:00")


def test_write_plan_does_not_overwrite_past_quarters():
    """write_plan must not rewrite freeze-ready q15 slots in SQLite.

    At 14:30 q0–q1 are freeze-ready (:00-:30); q2–q3 may take the incoming plan.
    from_actual slots stay immutable even if incoming tries to mutate them.
    """
    now = _now(14, 30)

    def _marked_q15(*, prod: list[float], actual_until: int = -1) -> list[dict]:
        return [
            {
                "quarter": q,
                "production": prod[q],
                "consumption": 0.1,
                "soc": 40.0 + q,
                "battery": -0.05,
                "grid_import": 0.0,
                "grid_export": 0.0,
                "from_actual": q <= actual_until,
            }
            for q in range(4)
        ]

    seed_row = _live_row(14, marker="LOCKED-CUR", q_marker=1.0, locked=True)
    seed_row["q15"] = _marked_q15(prod=[1.1, 1.2, 1.3, 1.4], actual_until=0)
    write_plan(
        {
            "today_date": TODAY,
            "plan_from_hour": 14,
            "computed_at": "seed",
            "history_rows": [_hist_row(12, marker="HIST-12")],
            "rows": [seed_row, _live_row(15, marker="FUT-15", q_marker=5.0)],
        },
        now=now,
    )

    hack_cur = _live_row(14, marker="HACK-CUR", q_marker=9.0, locked=False)
    hack_cur["q15"] = _marked_q15(prod=[9.1, 9.2, 9.3, 9.4], actual_until=-1)
    hack_hist = _hist_row(12, marker="HACK-HIST")
    hack_hist["q15"] = _marked_q15(prod=[8.0, 8.0, 8.0, 8.0], actual_until=3)

    write_plan(
        {
            "today_date": TODAY,
            "plan_from_hour": 14,
            "computed_at": "hack",
            "history_rows": [hack_hist],
            "rows": [hack_cur, _live_row(15, marker="NEW-15", q_marker=7.0)],
        },
        now=now,
    )

    stored = read_plan()
    assert stored is not None

    # Past hour in history — untouched (including its quarters).
    hist = next(r for r in stored["history_rows"] if int(r["hour"]) == 12)
    assert hist["action"] == "HIST-12"
    assert [s["production"] for s in hist["q15"]] == [12.0, 12.0, 12.0, 12.0]

    cur = next(r for r in stored["rows"] if int(r["hour"]) == 14)
    assert cur["timer_schedule"].startswith("Dis 14:00")
    # Freeze-ready q0 stays as seeded.
    assert cur["q15"][0]["production"] == 1.1
    assert cur["q15"][0]["from_actual"] is True
    # q1 is freeze-ready at :30 even without from_actual.
    assert cur["q15"][1]["production"] == 1.2
    # Open quarters 2–3 may come from the incoming write.
    assert cur["q15"][2]["production"] == 9.3
    assert cur["q15"][3]["production"] == 9.4

    fut = next(r for r in stored["rows"] if int(r["hour"]) == 15)
    assert fut["action"] == "NEW-15"
    assert fut["q15"][0]["production"] == 7.0


def test_empty_sqlite_allows_first_seed():
    now = _now(14, 0)
    write_plan(
        {
            "today_date": TODAY,
            "plan_from_hour": 14,
            "history_rows": [_hist_row(5, marker="SEEDED")],
            "rows": [_live_row(14, marker="CUR", q_marker=1.0)],
        },
        now=now,
    )
    stored = read_plan()
    assert stored["history_rows"][0]["action"] == "SEEDED"


def test_guard_keeps_locked_chg_while_window_open_even_if_bat_charge_thin():
    """Current-hour Chg stays locked mid-window even when Bat Charge is still 0."""
    existing_cur = _live_row(14, marker="Charging from Grid", q_marker=1.0, locked=True)
    existing_cur["timer_schedule"] = "Chg 14:00-14:30 4.0kW cap25%"
    existing_cur["action"] = "Charging from Grid"
    existing_cur["bat_charge"] = 0.0
    existing_cur["battery"] = -0.7
    for slot in existing_cur["q15"]:
        slot["battery"] = -0.2
        slot["grid_import"] = 0.0

    incoming_cur = _live_row(14, marker="Discharging to Load", q_marker=2.0, locked=False)
    incoming_cur["timer_schedule"] = ""
    incoming_cur["action"] = "Discharging to Load"
    incoming_cur["bat_charge"] = 0.0
    for slot in incoming_cur["q15"]:
        slot["battery"] = -0.2
        slot["grid_import"] = 0.0

    existing = {
        "today_date": TODAY,
        "plan_from_hour": 14,
        "history_rows": [],
        "rows": [existing_cur],
    }
    incoming = {
        "today_date": TODAY,
        "plan_from_hour": 14,
        "history_rows": [],
        "rows": [incoming_cur],
    }
    guarded = guard_future_quarters_on_write(incoming, existing, now=_now(14, 25))
    cur = next(r for r in guarded["rows"] if int(r["hour"]) == 14)
    assert str(cur.get("timer_schedule") or "").startswith("Chg 14:00-14:30")
    assert cur.get("hour_labels_locked") is True
