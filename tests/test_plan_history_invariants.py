"""Day-long quarter-tick simulations guarding plan history integrity.

Instead of asserting single merge calls, these tests drive the same call
sequence the Pi runs in production — scheduler quarter merges, forced/window
full rebuilds racing them, restarts, slow sims crossing an hour boundary —
and after EVERY write check the invariants that were violated when history
hours silently disappeared from SQLite:

  I1. History covers exactly hours [0 .. current_hour-1] — no holes, no future.
  I2. Plan rows cover exactly [current_hour .. 23] for today.
  I3. No hour is both in history and in rows.
  I4. Once an hour is in history, its timer_schedule/action never change.
  I5. soc_blended (violet live-SOC highlight in the UI) is never present on
      history rows — otherwise two rows are highlighted at once.
      The current-hour plan row always carries soc_blended (including at :00).
  I6. Once hour_labels_locked, timer_schedule on that hour never changes for
      the rest of the day (current row and after promote to history).
      Action follows battery/grid fact and may update on the live hour.
  I7. Once a q15 slot has from_actual=True, its values are never overwritten
      by later quarter refreshes or forced rebuilds.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.grid_config import merge_grid_defaults
from src.plan_cache_merge import attach_immutable_history, merge_incremental_plan

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-07-07"
SIM_DURATION_S = 6  # realistic Pi full-sim wall time


def _cfg() -> dict:
    cfg = {
        "battery": {"capacity_kwh": 32.0, "min_soc_pct": 10},
        "simulation": {"min_soc_pct": 16},
        "grid": {},
    }
    merge_grid_defaults(cfg)
    return cfg


CFG = _cfg()


def _mk_row(hour: int, *, source: str) -> dict:
    """Plan row as run_simulation produces it (timer marks the producing tick)."""
    return {
        "plan_date": TODAY,
        "hour": hour,
        "start": f"07-07-2026 {(hour + 1) % 24:02d}:00",
        "production": 1.0,
        "consumption": 0.5,
        "battery": 0.0,
        "bat_charge": 0.0,
        "bat_discharge": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "soc": 50.0,
        "timer_schedule": f"Dis {hour:02d}:00-{hour:02d}:45 5kW cap42%",
        "action": f"Plan@{source}",
        "q15": [
            {"quarter": q, "production": 0.25, "consumption": 0.125,
             "soc": 50.0, "battery": 0.0, "grid_import": 0.0,
             "grid_export": 0.0, "from_actual": False}
            for q in range(4)
        ],
    }


def _mk_fresh(sim_now: datetime) -> dict:
    """Fresh sim payload: rows from sim_now.hour, meter history for earlier hours.

    Mid-hour optimizer clears the current hour's Timer Schedule / Action and
    rewrites q15. Merge must keep locked labels and from_actual slots.
    """
    from_hour = sim_now.hour
    label = sim_now.strftime("%H:%M:%S")
    hist = []
    for h in range(from_hour):
        row = _mk_row(h, source=f"meters@{label}")
        row["timer_schedule"] = ""  # meters never carry timers
        row["action"] = "Meters"
        row["history_hour"] = True
        hist.append(row)
    rows = []
    for h in range(from_hour, 24):
        row = _mk_row(h, source=label)
        if h == from_hour:
            # run_simulation marks only the in-progress hour as live SOC.
            row["soc_blended"] = True
        if h == from_hour and sim_now.minute != 0:
            row["timer_schedule"] = ""
            row["action"] = f"WIPED@{label}"
            row["hour_labels_locked"] = False
            for slot in row["q15"]:
                slot["production"] = 99.0
                slot["consumption"] = 99.0
                slot["from_actual"] = False
        rows.append(row)
    return {
        "today_date": TODAY,
        "plan_from_hour": from_hour,
        "computed_at": sim_now.strftime("%Y-%m-%d %H:%M:%S"),
        "delta_kwh": 0.0,
        "history_rows": hist,
        "rows": rows,
        "totals": {},
    }


def _today_history_hours(plan: dict) -> list[int]:
    return sorted(
        int(r["hour"]) for r in plan.get("history_rows") or []
        if str(r.get("plan_date")) == TODAY
    )


def _today_row_hours(plan: dict) -> list[int]:
    return sorted(
        int(r["hour"]) for r in plan.get("rows") or []
        if str(r.get("plan_date")) == TODAY and r.get("start") != "TOTAL"
    )


def _labels(row: dict) -> tuple[str, str]:
    return (str(row.get("timer_schedule") or ""), str(row.get("action") or ""))


def _actual_slot_fingerprint(slot: dict) -> tuple:
    return (
        round(float(slot.get("production") or 0), 4),
        round(float(slot.get("consumption") or 0), 4),
        round(float(slot.get("battery") or 0), 4),
        round(float(slot.get("grid_import") or 0), 4),
        round(float(slot.get("grid_export") or 0), 4),
        round(float(slot.get("soc") or 0), 4),
        True,
    )


class HistoryLedger:
    """Tracks first-seen history content to assert immutability (I4)."""

    def __init__(self) -> None:
        self.seen: dict[int, tuple[str, str]] = {}

    def check(self, plan: dict, context: str) -> None:
        for row in plan.get("history_rows") or []:
            if str(row.get("plan_date")) != TODAY:
                continue
            hour = int(row["hour"])
            content = _labels(row)
            if hour in self.seen:
                assert self.seen[hour] == content, (
                    f"{context}: history hour {hour} mutated "
                    f"{self.seen[hour]} -> {content}"
                )
            else:
                self.seen[hour] = content


class LockedLabelsLedger:
    """I6 — locked timer_schedule never changes once hour_labels_locked."""

    def __init__(self) -> None:
        self.seen: dict[int, str] = {}

    def check(self, plan: dict, context: str) -> None:
        for row in (plan.get("history_rows") or []) + (plan.get("rows") or []):
            if str(row.get("plan_date")) != TODAY or row.get("start") == "TOTAL":
                continue
            if not (row.get("hour_labels_locked") or row.get("timer_schedule_manual")):
                continue
            hour = int(row["hour"])
            content = str(row.get("timer_schedule") or "")
            if hour in self.seen:
                assert self.seen[hour] == content, (
                    f"{context}: locked timer hour {hour} mutated "
                    f"{self.seen[hour]!r} -> {content!r}"
                )
            else:
                self.seen[hour] = content


class ActualQ15Ledger:
    """I7 — from_actual q15 slots are immutable after the first freeze."""

    def __init__(self) -> None:
        self.seen: dict[tuple[int, int], tuple] = {}

    def check(self, plan: dict, context: str) -> None:
        for row in (plan.get("history_rows") or []) + (plan.get("rows") or []):
            if str(row.get("plan_date")) != TODAY or row.get("start") == "TOTAL":
                continue
            hour = int(row["hour"])
            for slot in row.get("q15") or []:
                if not slot.get("from_actual"):
                    continue
                key = (hour, int(slot.get("quarter", 0)))
                fp = _actual_slot_fingerprint(slot)
                if key in self.seen:
                    assert self.seen[key] == fp, (
                        f"{context}: from_actual q{key[1]} hour {hour} mutated "
                        f"{self.seen[key]} -> {fp}"
                    )
                else:
                    self.seen[key] = fp


def _assert_invariants(
    plan: dict,
    wall_hour: int,
    context: str,
    ledger: HistoryLedger,
    labels: LockedLabelsLedger | None = None,
    actuals: ActualQ15Ledger | None = None,
) -> None:
    hist = _today_history_hours(plan)
    rows = _today_row_hours(plan)
    assert hist == list(range(wall_hour)), (
        f"{context}: history {hist} != contiguous 0..{wall_hour - 1}"
    )
    assert rows == list(range(wall_hour, 24)), (
        f"{context}: rows {rows} != {wall_hour}..23"
    )
    assert not set(hist) & set(rows), f"{context}: hour in both history and rows"
    blended_hist = [
        int(r["hour"]) for r in plan.get("history_rows") or [] if r.get("soc_blended")
    ]
    assert not blended_hist, (
        f"{context}: history hours {blended_hist} still carry soc_blended — "
        f"UI would highlight live SOC on more than one row"
    )
    cur_rows = [
        r for r in plan.get("rows") or []
        if str(r.get("plan_date") or "") == TODAY
        and r.get("start") != "TOTAL"
        and int(r.get("hour", -1)) == wall_hour
    ]
    assert cur_rows, f"{context}: missing current-hour row {wall_hour}"
    assert cur_rows[0].get("soc_blended") is True, (
        f"{context}: current hour {wall_hour} missing soc_blended — "
        f"UI would not highlight live SOC"
    )
    ledger.check(plan, context)
    if labels is not None:
        labels.check(plan, context)
    if actuals is not None:
        actuals.check(plan, context)


def _scheduler_tick(stored: dict, now: datetime, *, sim_delay_s: int = SIM_DURATION_S) -> dict:
    """One quarter refresh as hourly_plan_refresh runs it: fresh sim, then merge.

    `now` is the job tick; merge uses that hour even if the fresh payload was
    built a few seconds later.
    """
    fresh = _mk_fresh(now + timedelta(seconds=sim_delay_s))
    return merge_incremental_plan(stored, fresh, now=now, metrics={}, cfg=CFG)


def _forced_rebuild(stored: dict | None, enter_now: datetime, *, sim_delay_s: int = SIM_DURATION_S) -> dict:
    """Full rebuild as build_plan_simulation runs it (window mismatch).

    Same calendar day → merge_incremental_plan (preserves locked timer/action and
    from_actual q15). New day / empty store → attach_immutable_history seed.
    """
    result = _mk_fresh(enter_now + timedelta(seconds=sim_delay_s))
    if stored is not None and str(stored.get("today_date") or "") == TODAY:
        return merge_incremental_plan(stored, result, now=enter_now, metrics={}, cfg=CFG)
    attach_immutable_history(result, stored, now=enter_now)
    return result


def _day_start_plan() -> dict:
    """Midnight full rebuild (new day, empty history)."""
    return _forced_rebuild(None, datetime(2026, 7, 7, 0, 0, 2, tzinfo=TZ), sim_delay_s=0)


def test_clean_day_of_quarter_ticks_keeps_history_contiguous():
    """Baseline: 96 scheduler ticks — history, locked timers, and actual q15 hold."""
    ledger = HistoryLedger()
    labels = LockedLabelsLedger()
    actuals = ActualQ15Ledger()
    stored = _day_start_plan()
    _assert_invariants(stored, 0, "00:00 rebuild", ledger, labels, actuals)

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(
                stored, hour, f"tick {hour:02d}:{minute:02d}", ledger, labels, actuals,
            )
            # After :00 lock, mid-hour wipe must not clear Timer Schedule.
            if minute != 0:
                cur = next(
                    r for r in stored["rows"]
                    if int(r["hour"]) == hour and str(r.get("plan_date")) == TODAY
                )
                assert cur.get("hour_labels_locked") is True, (
                    f"tick {hour:02d}:{minute:02d}: current hour not locked"
                )
                assert str(cur.get("timer_schedule") or "").startswith(f"Dis {hour:02d}:00"), (
                    f"tick {hour:02d}:{minute:02d}: timer wiped to {cur.get('timer_schedule')!r}"
                )

    assert _today_history_hours(stored) == list(range(23))
    # Every completed quarter of hours 0..22 should be frozen (4 per hour).
    # Hour 23 still in rows — quarters frozen through the last :45 tick (q0..q2).
    assert len(actuals.seen) >= 23 * 4
    assert all(
        labels.seen[h].startswith(f"Dis {h:02d}:00")
        for h in range(23)
        if h in labels.seen
    )


def test_forced_rebuild_straddling_hour_boundary_loses_nothing():
    """The exact Pi failure: UI ?refresh=1 enters at HH:59:5x, sim ends after :00."""
    ledger = HistoryLedger()
    labels = LockedLabelsLedger()
    actuals = ActualQ15Ledger()
    stored = _day_start_plan()

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(
                stored, hour, f"tick {hour:02d}:{minute:02d}", ledger, labels, actuals,
            )

        # UI-forced rebuild at HH:59 keeps that hour as current (tick hour).
        enter = datetime(2026, 7, 7, hour, 59, 56, tzinfo=TZ)
        stored = _forced_rebuild(stored, enter)
        _assert_invariants(
            stored, hour, f"straddle rebuild {hour:02d}:59:56", ledger, labels, actuals,
        )


def test_missed_hour_tick_after_restart_recovers_next_quarter():
    """Deploy/restart swallows the :00 tick — the :15 merge must still promote."""
    ledger = HistoryLedger()
    labels = LockedLabelsLedger()
    actuals = ActualQ15Ledger()
    stored = _day_start_plan()

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            if minute == 0 and hour in (8, 13, 14):  # restarts swallow these ticks
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(
                stored, hour, f"tick {hour:02d}:{minute:02d}", ledger, labels, actuals,
            )


def test_forced_rebuild_racing_every_hour_boundary():
    """Forced rebuild right around each :00 in every order vs the scheduler tick."""
    ledger = HistoryLedger()
    labels = LockedLabelsLedger()
    actuals = ActualQ15Ledger()
    stored = _day_start_plan()

    for hour in range(1, 24):
        boundary = datetime(2026, 7, 7, hour, 0, 0, tzinfo=TZ)
        if hour % 2:
            # UI wins the lock first (entered just after :00), scheduler second.
            stored = _forced_rebuild(stored, boundary + timedelta(seconds=1))
            _assert_invariants(stored, hour, f"UI-first {hour:02d}:00", ledger, labels, actuals)
            stored = _scheduler_tick(stored, boundary + timedelta(seconds=8))
            _assert_invariants(
                stored, hour, f"sched-second {hour:02d}:00", ledger, labels, actuals,
            )
        else:
            # Scheduler first, then a UI forced rebuild a few seconds later.
            stored = _scheduler_tick(stored, boundary + timedelta(seconds=2))
            _assert_invariants(
                stored, hour, f"sched-first {hour:02d}:00", ledger, labels, actuals,
            )
            stored = _forced_rebuild(stored, boundary + timedelta(seconds=20))
            _assert_invariants(
                stored, hour, f"UI-second {hour:02d}:00", ledger, labels, actuals,
            )
        for minute in (15, 30, 45):
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(
                stored, hour, f"tick {hour:02d}:{minute:02d}", ledger, labels, actuals,
            )


@pytest.mark.parametrize("seed", [1, 7, 42, 1337])
def test_randomized_day_with_forced_rebuilds_and_slow_sims(seed: int):
    """Fuzzed day: random forced rebuilds, sim durations and missed ticks.

    Any interleaving of writers must keep history contiguous and immutable.
    """
    rng = random.Random(seed)
    ledger = HistoryLedger()
    labels = LockedLabelsLedger()
    actuals = ActualQ15Ledger()
    stored = _day_start_plan()

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            # 10% of scheduler ticks are lost to restarts (never two :00 in a row).
            if rng.random() < 0.10:
                continue
            tick_s = rng.randint(0, 20)
            sim_s = rng.randint(2, 12)
            now = datetime(2026, 7, 7, hour, minute, tick_s, tzinfo=TZ)
            stored = _scheduler_tick(stored, now, sim_delay_s=sim_s)
            _assert_invariants(
                stored,
                now.hour,
                f"seed{seed} tick {hour:02d}:{minute:02d}:{tick_s}",
                ledger,
                labels,
                actuals,
            )

            # 30% chance a UI forced rebuild lands right after, possibly
            # straddling the next hour boundary.
            if rng.random() < 0.30:
                offset_s = rng.choice([5, 30, 300, 880])  # up to :14:40 into the quarter
                enter = now + timedelta(seconds=offset_s)
                sim_s2 = rng.randint(2, 12)
                if enter.day != 7:
                    continue
                stored = _forced_rebuild(stored, enter, sim_delay_s=sim_s2)
                if enter.day != 7:
                    continue
                _assert_invariants(
                    stored,
                    enter.hour,
                    f"seed{seed} rebuild {enter:%H:%M:%S}",
                    ledger,
                    labels,
                    actuals,
                )


def test_history_survives_sqlite_wipe_via_meter_seed():
    """delete_plan mid-day: seed restores meters history and the day continues."""
    ledger = HistoryLedger()
    labels = LockedLabelsLedger()
    actuals = ActualQ15Ledger()
    stored = _day_start_plan()
    for hour in range(10):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            stored = _scheduler_tick(stored, datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ))

    # config save wipes plan_latest, then forces a rebuild with existing=None
    stored = _forced_rebuild(None, datetime(2026, 7, 7, 10, 5, 0, tzinfo=TZ))
    seed_ledger = HistoryLedger()  # seed rebuilds history from meters — new baseline
    seed_labels = LockedLabelsLedger()
    seed_actuals = ActualQ15Ledger()
    _assert_invariants(stored, 10, "post-wipe seed", seed_ledger, seed_labels, seed_actuals)

    for hour in range(10, 24):
        for minute in (0, 15, 30, 45):
            if hour == 10 and minute == 0:
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(
                stored, hour, f"post-wipe tick {hour:02d}:{minute:02d}",
                seed_ledger, seed_labels, seed_actuals,
            )
