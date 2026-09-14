"""run_hour_boundary_start reads timer_schedule from SQLite and writes to SA."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from src import hour_boundary_scheduler as hbs
from src.sqlite_store import write_plan


def _plan_with_timer(hour: int, timer: str, today: str = "2026-07-07") -> dict:
    return {
        "today_date": today,
        "plan_from_hour": hour,
        "history_rows": [],
        "rows": [
            {
                "hour": hour,
                "plan_date": today,
                "start": f"07-07-2026 {hour + 1:02d}:00",
                "timer_schedule": timer,
                "action": "Discharging to Grid and Load",
                "hour_labels_locked": True,
                "production": 1.0,
                "consumption": 0.5,
                "battery": -2.0,
                "bat_charge": 0.0,
                "bat_discharge": 2.0,
                "grid_import": 0.0,
                "grid_export": 1.5,
                "soc": 50.0,
                "buy_price": 1.0,
                "g12_zone": "peak",
                "q15": [],
            }
        ],
    }


def _cfg():
    return {
        "smart_mode_enabled": True,
        "battery": {"capacity_kwh": 20.0},
        "simulation": {"min_soc_pct": 16},
        "inverter": {"ac_capacity_kw": 8.0},
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


@pytest.fixture(autouse=True)
def _clear_plan(tmp_path):
    from src import sqlite_store
    orig = sqlite_store._DB_PATH
    sqlite_store._DB_PATH = tmp_path / "test.db"
    sqlite_store._conn = None
    yield
    sqlite_store._DB_PATH = orig
    sqlite_store._conn = None


def test_plan_rows_reads_from_sqlite():
    """_plan_rows returns rows from SQLite without calling build_plan_simulation."""
    plan = _plan_with_timer(8, "Dis 08:00-08:45 8.0kW cap16%")
    write_plan(plan)

    rows = asyncio.run(hbs._plan_rows(_cfg()))

    assert len(rows) == 1
    assert rows[0]["timer_schedule"] == "Dis 08:00-08:45 8.0kW cap16%"


def test_plan_rows_returns_empty_when_sqlite_has_no_plan():
    """When SQLite is empty, _plan_rows returns [] — no fallback to simulation."""
    rows = asyncio.run(hbs._plan_rows(_cfg()))
    assert rows == []


def test_sync_timer_reads_timer_from_sqlite_rows():
    """_sync_timer_from_hour_row uses timer_schedule from rows (read from SQLite upstream)."""
    rows = _plan_with_timer(8, "Dis 08:00-08:45 8.0kW cap16%")["rows"]
    cfg = _cfg()
    now = datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    sa_rules = {
        "timed_discharge_enabled": False,
        "timed_charge_enabled": False,
        "discharge_slots": [{"slot": 1, "from": "00:00", "to": "00:00",
                              "capacity_pct": 16, "power_kw": 8.0}],
        "charge_slots": [{"slot": 1, "from": "00:00", "to": "00:00", "power_kw": 0}],
    }
    apply_mock = AsyncMock(return_value=True)
    get_rules_mock = AsyncMock(return_value=sa_rules)

    with (
        patch.object(hbs, "now_warsaw", return_value=now),
        patch.object(hbs.sa_client, "get_rules", get_rules_mock),
        patch.object(
            hbs.sa_client, "get_live_metrics",
            AsyncMock(return_value={"battery_soc": 16.0}),
        ),
        patch.object(hbs.sa_client, "apply_hourly_schedule_to_sa", apply_mock),
    ):
        status = asyncio.run(hbs._sync_timer_from_hour_row(cfg, rows, 8))

    assert status["ok"] is True
    assert status["skipped"] is False
    assert "Dis 08:00-08:45" in (status["timer_schedule"] or "")
    apply_mock.assert_awaited_once()
    schedule_sent = apply_mock.call_args[0][1]
    assert schedule_sent["timed_discharge_enabled"] is True
    dis = schedule_sent["discharge_slots"][0]
    assert dis["from"] == "08:00"
    assert dis["to"] == "08:45"


def test_sync_timer_skips_when_empty_timer_schedule():
    rows = _plan_with_timer(8, "")["rows"]
    get_rules = AsyncMock(return_value={"timed_charge_enabled": False})

    with patch.object(hbs.sa_client, "get_rules", get_rules):
        status = asyncio.run(hbs._sync_timer_from_hour_row(_cfg(), rows, 8))

    assert status["skipped"] is True
    assert status["skip_reason"] == "empty_timer_schedule"
    assert status["ok"] is True


def test_sync_timer_skips_when_no_row_for_hour():
    rows = _plan_with_timer(9, "Dis 09:00-09:45 8.0kW cap16%")["rows"]
    get_rules = AsyncMock(return_value={"timed_charge_enabled": False})

    with patch.object(hbs.sa_client, "get_rules", get_rules):
        status = asyncio.run(hbs._sync_timer_from_hour_row(_cfg(), rows, 8))

    assert status["skipped"] is True
    assert status["ok"] is True


def test_sync_timer_clears_stale_timed_charge_when_empty():
    rows = _plan_with_timer(8, "")["rows"]
    get_rules = AsyncMock(return_value={
        "timed_charge_enabled": True,
        "timed_discharge_enabled": False,
    })
    set_flags = AsyncMock(return_value=True)

    with (
        patch.object(hbs.sa_client, "get_rules", get_rules),
        patch.object(hbs.sa_client, "set_timed_power_flags", set_flags),
    ):
        status = asyncio.run(hbs._sync_timer_from_hour_row(_cfg(), rows, 8))

    assert status["skip_reason"] == "empty_timer_schedule"
    assert status["stale_clear"]["cleared"] is True
    set_flags.assert_awaited_once()
    kwargs = set_flags.await_args.kwargs
    assert kwargs["timed_charge_enabled"] is False


def test_sync_timer_writes_plan_start_without_shift():
    """Late :00 job still writes Chg 02:00-02:30 — no clip to :15 or next minute."""
    rows = _plan_with_timer(
        2, "Chg 02:00-02:30 5.0kW cap24%", today="2026-07-21",
    )["rows"]
    rows[0]["action"] = "Charging from Grid"
    now = datetime(2026, 7, 21, 2, 0, 12, tzinfo=ZoneInfo("Europe/Warsaw"))
    sa_rules = {
        "timed_discharge_enabled": False,
        "timed_charge_enabled": False,
        "discharge_slots": [{"slot": 1, "from": "00:00", "to": "00:00",
                              "capacity_pct": 16, "power_kw": 0}],
        "charge_slots": [{"slot": 1, "from": "03:00", "to": "04:00",
                           "capacity_pct": 19, "power_kw": 6.0}],
    }
    apply_mock = AsyncMock(return_value=True)

    with (
        patch.object(hbs, "now_warsaw", return_value=now),
        patch.object(hbs.sa_client, "get_rules", AsyncMock(return_value=sa_rules)),
        patch.object(
            hbs.sa_client, "get_live_metrics",
            AsyncMock(return_value={"battery_soc": 16.0}),
        ),
        patch.object(hbs.sa_client, "apply_hourly_schedule_to_sa", apply_mock),
    ):
        status = asyncio.run(hbs._sync_timer_from_hour_row(_cfg(), rows, 2))

    assert status["skipped"] is False
    assert status["ok"] is True
    assert status["timer_schedule"] == "Chg 02:00-02:30 5.0kW cap24%"
    slot = apply_mock.call_args[0][1]["charge_slots"][0]
    assert slot["from"] == "02:00"
    assert slot["to"] == "02:30"
    assert slot["power_kw"] == 5.0


def test_mid_quarter_limit_home_retries_active_charge_write():
    """:15 must recover a missed :00 Chg write while the window is still open."""
    today = "2026-07-21"
    plan = _plan_with_timer(2, "Chg 02:00-02:30 6.0kW cap24%", today=today)
    plan["rows"][0]["action"] = "Charging from Grid"
    write_plan(plan)
    now = datetime(2026, 7, 21, 2, 15, tzinfo=ZoneInfo("Europe/Warsaw"))

    sa_rules = {
        "timed_charge_enabled": False,
        "timed_discharge_enabled": False,
        "work_mode": "Limit power to home load",
        "battery_discharge_mode": "UPS and home loads",
        "charge_slots": [{
            "slot": 1, "from": "03:00", "to": "04:00",
            "capacity_pct": 19, "power_kw": 6.0,
        }],
        "discharge_slots": [{"slot": 1, "from": "00:00", "to": "00:00",
                             "capacity_pct": 16, "power_kw": 0}],
    }
    apply_mock = AsyncMock(return_value=True)
    get_rules = AsyncMock(return_value=sa_rules)

    with (
        patch.object(hbs, "now_warsaw", return_value=now),
        patch.object(hbs, "load_config", return_value=_cfg()),
        patch.object(hbs.sa_client, "get_rules", get_rules),
        patch.object(
            hbs.sa_client, "get_live_metrics",
            AsyncMock(return_value={"battery_soc": 16.0}),
        ),
        patch.object(hbs.sa_client, "apply_hourly_schedule_to_sa", apply_mock),
        patch.object(hbs, "run_work_mode_hour_start", new_callable=AsyncMock) as wm_start,
        patch.object(hbs, "run_work_mode_limit_home", new_callable=AsyncMock) as wm_limit,
    ):
        wm_start.return_value = {
            "ok": True, "charge_grid_prepare": True, "skipped": True,
            "skip_reason": "already_set",
        }
        wm_limit.return_value = {
            "ok": True, "skipped": True, "skip_reason": "discharge_not_ended",
            "limit_due": False,
        }
        status = asyncio.run(hbs.run_hour_boundary_limit_home())

    assert status["timer_sync"]["skipped"] is False
    assert "Chg" in (status["timer_sync"].get("timer_schedule") or "")
    apply_mock.assert_awaited_once()
    schedule = apply_mock.call_args[0][1]
    assert schedule["timed_charge_enabled"] is True
    assert schedule["charge_slots"][0]["from"] == "02:00"
    assert schedule["charge_slots"][0]["to"] == "02:30"
    wm_start.assert_awaited()


def test_mid_quarter_charge_end_clears_timed_charge_only():
    """:30 after Chg …-03:30 — untick Timed charge; do not run discharge Limit-home clear."""
    today = "2026-07-21"
    plan = _plan_with_timer(3, "Chg 03:00-03:30 6.0kW cap24%", today=today)
    plan["rows"][0]["action"] = "Charging from Grid"
    write_plan(plan)
    now = datetime(2026, 7, 21, 3, 30, tzinfo=ZoneInfo("Europe/Warsaw"))

    sa_rules = {
        "timed_charge_enabled": True,
        "timed_discharge_enabled": False,
        "work_mode": "Limit power to home load",
        "battery_discharge_mode": "UPS and home loads",
        "charge_slots": [{
            "slot": 1, "from": "03:00", "to": "03:30",
            "capacity_pct": 24, "power_kw": 6.0,
        }],
        "discharge_slots": [{"slot": 1, "from": "00:00", "to": "00:00",
                             "capacity_pct": 16, "power_kw": 0}],
    }
    set_flags = AsyncMock(return_value=True)

    with (
        patch.object(hbs, "now_warsaw", return_value=now),
        patch.object(hbs, "load_config", return_value=_cfg()),
        patch.object(hbs.sa_client, "get_rules", AsyncMock(return_value=sa_rules)),
        patch.object(hbs.sa_client, "set_timed_power_flags", set_flags),
        patch.object(hbs.sa_client, "apply_hourly_schedule_to_sa", AsyncMock()) as apply_mock,
        patch.object(hbs, "run_work_mode_limit_home", new_callable=AsyncMock) as wm_limit,
        patch.object(hbs, "_clear_timed_power_flags", new_callable=AsyncMock) as clear_both,
    ):
        wm_limit.return_value = {
            "ok": True, "skipped": True, "skip_reason": "discharge_not_ended",
            "limit_due": False,
        }
        status = asyncio.run(hbs.run_hour_boundary_limit_home())

    assert status.get("charge_end_hhmm") == "03:30"
    assert status.get("timed_charge_clear", {}).get("cleared") is True
    clear_both.assert_not_awaited()
    apply_mock.assert_not_awaited()
    set_flags.assert_awaited()
    kwargs = set_flags.await_args.kwargs
    assert kwargs.get("timed_charge_enabled") is False
    # Preserve discharge flag as currently read from SA (do not force both off).
    assert kwargs.get("timed_discharge_enabled") is False


def test_hour_boundary_start_uses_passed_tick_hour():
    """SA hour comes from the job tick, not a later wall-clock now_warsaw()."""
    write_plan(_plan_with_timer(10, "Dis 10:00-10:45 8.0kW cap16%"))
    wall = datetime(2026, 7, 7, 11, 0, 5, tzinfo=ZoneInfo("Europe/Warsaw"))
    tick = datetime(2026, 7, 7, 10, 0, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    wm = AsyncMock(return_value={
        "ok": True, "on_grid_trigger_this_slot": True, "skipped": False,
    })
    sync = AsyncMock(return_value={"ok": True, "skipped": False})
    lim = AsyncMock(return_value={
        "ok": True, "skipped": True, "limit_due": False,
    })
    with (
        patch.object(hbs, "now_warsaw", return_value=wall),
        patch.object(hbs, "load_config", return_value=_cfg()),
        patch.object(hbs, "run_work_mode_hour_start", wm),
        patch.object(hbs, "run_work_mode_limit_home", lim),
        patch.object(hbs, "_sync_timer_from_hour_row", sync),
    ):
        status = asyncio.run(hbs.run_hour_boundary_start(now=tick))
    assert status["hour"] == 10
    sync.assert_awaited_once()
    assert sync.call_args.args[2] == 10
    assert wm.await_args.kwargs.get("now") == tick
