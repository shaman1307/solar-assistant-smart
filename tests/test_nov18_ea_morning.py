"""18 Nov 2026, 00:00–13:00: recorded Energy arbitrage rows, then the inverter writes.

The rows are the plan stored on the Pi at 09:00 (history through hour 8, plan from hour 9).
Hour 4 stayed at 24% under cap 25%. The replay sends that stop as live SOC + 5.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from src import hour_boundary_scheduler as hbs
from src import sa_client
from tests.test_nov17_timer_window_writes import FakeInverter

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-11-18"
_FIXTURE = Path(__file__).parent / "fixtures" / "ea_2026_11_18_00_13.json"
EA_ROWS: list[dict] = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _cfg() -> dict:
    return {
        "smart_mode_enabled": True,
        "battery": {"capacity_kwh": 47.6, "max_discharge_power_kw": 8.0, "max_charge_power_kw": 5.8},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {"min_soc_pct": 16},
    }


def _live_soc(hour: int, minute: int) -> float:
    """SOC the meter showed at this tick: previous hour end, else this hour's quarter."""
    row = EA_ROWS[hour]
    if minute == 0:
        if hour == 0:
            return float(row["q15"][0]["soc"])
        return float(EA_ROWS[hour - 1]["soc"])
    quarter = {15: 0, 31: 1, 45: 2}[minute]
    return float(row["q15"][quarter]["soc"])


def _plan_at(hour: int) -> dict:
    return {
        "today_date": TODAY,
        "plan_from_hour": hour,
        "history_rows": [row for row in EA_ROWS if row["hour"] < hour],
        "rows": [row for row in EA_ROWS if row["hour"] >= hour],
    }


def test_fixture_is_the_recorded_morning():
    assert [row["hour"] for row in EA_ROWS] == list(range(13))
    assert all(row["plan_date"] == TODAY and len(row["q15"]) == 4 for row in EA_ROWS)
    by_hour = {row["hour"]: row for row in EA_ROWS}
    assert by_hour[4]["timer_schedule"] == "Chg 04:30-05:00 2.5kW cap25%"
    assert by_hour[4]["bat_charge"] == 0.002
    assert by_hour[4]["soc"] == 24.0
    assert by_hour[4]["grid_import"] == 0.226
    assert by_hour[5]["timer_schedule"] == "Chg 05:00-06:00 5.8kW cap37%"
    assert by_hour[5]["bat_charge"] == 4.96
    assert by_hour[5]["soc"] == 34.0
    assert by_hour[5]["grid_import"] == 5.598
    assert [row["timer_schedule"] for row in EA_ROWS if row["hour"] not in (4, 5)] == [""] * 11


def test_morning_sends_the_close_cap_lifted_and_the_05_window_unchanged():
    inverter = FakeInverter()
    work_mode_ok = AsyncMock(return_value={"ok": True, "skipped": True, "skip_reason": "already_set"})
    limit_ok = AsyncMock(return_value={"ok": True, "skipped": True, "limit_due": False})

    for hour in range(13):
        for minute in (0, 15, 31, 45):
            tick = datetime(2026, 11, 18, hour, minute, 10, tzinfo=TZ)
            inverter.tick = tick.strftime("%H:%M")
            metrics = AsyncMock(return_value={"battery_soc": _live_soc(hour, minute)})
            with (
                patch.object(hbs, "now_warsaw", return_value=tick),
                patch.object(hbs, "load_config", return_value=_cfg()),
                patch.object(hbs, "read_plan", return_value=_plan_at(hour)),
                patch.object(hbs, "run_work_mode_hour_start", work_mode_ok),
                patch.object(hbs, "run_work_mode_limit_home", limit_ok),
                patch.object(sa_client, "get_rules", inverter.get_rules),
                patch.object(sa_client, "get_live_metrics", metrics),
                patch.object(sa_client, "set_timer_schedule", inverter.set_timer_schedule),
                patch.object(sa_client, "set_timed_power_flags", inverter.set_timed_power_flags),
            ):
                if minute == 0:
                    status = asyncio.run(hbs.run_hour_boundary_start(now=tick))
                else:
                    status = asyncio.run(hbs.run_hour_boundary_limit_home(now=tick))
            assert status["ok"] is not False, (inverter.tick, status)

    assert inverter.window_writes == [
        ("04:00", "chg", "04:30", "05:00", 2.5, 29),
        ("05:00", "chg", "05:00", "06:00", 5.8, 37),
    ]
