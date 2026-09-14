"""Charge timer writes must not abort on SRNE-unsupported registers; repair mode desync."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from src.sa_client import (
    BATTERY_DISCHARGE_MODE_GRID_EXPORT,
    BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
    WORK_MODE_LIMIT_HOME_LOAD,
    WORK_MODE_ON_GRID,
    _build_schedule_writes,
    _grid_charge_current_a,
    work_mode_battery_modes_paired,
)
from src.timer_plan import timer_charge_active_at
from src.work_mode_scheduler import limit_home_due_for_timer, run_work_mode_hour_start


def test_work_mode_battery_modes_paired_detects_desync():
    assert work_mode_battery_modes_paired(
        WORK_MODE_ON_GRID, BATTERY_DISCHARGE_MODE_GRID_EXPORT,
    )
    assert work_mode_battery_modes_paired(
        WORK_MODE_LIMIT_HOME_LOAD, BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
    )
    # Desync: On-grid work mode left with UPS/home battery (blocks proper export/charge).
    assert not work_mode_battery_modes_paired(
        WORK_MODE_ON_GRID, BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
    )
    assert not work_mode_battery_modes_paired(
        WORK_MODE_LIMIT_HOME_LOAD, BATTERY_DISCHARGE_MODE_GRID_EXPORT,
    )


def test_charge_schedule_writes_skip_unsupported_srne_registers():
    settings = {
        "grid_charge_switch": "inverter_1/grid_charge",
        "charge_current_limit": "inverter_1/charge_current",
    }
    schedule = {
        "timed_charge_enabled": True,
        "timed_discharge_enabled": False,
        "charge_slots": [{
            "slot": 1,
            "from": "03:00",
            "to": "04:00",
            "capacity_pct": 19,
            "voltage_v": 58.0,
            "power_kw": 6.0,
            "grid": True,
            "generator": False,
        }],
        "discharge_slots": [],
    }
    writes = _build_schedule_writes(
        schedule,
        charge_slot_nums=(1,),
        discharge_slot_nums=(),
        settings=settings,
    )
    topics = [t for t, _ in writes]
    assert "inverter_1/grid_charge" in topics
    assert topics.index("inverter_1/grid_charge") < topics.index("inverter_1/timed_charge")
    assert "inverter_1/charge_power_slot_1" in topics
    assert "inverter_1/charge_using_grid_slot_1" not in topics
    assert "inverter_1/charge_using_generator_slot_1" not in topics
    assert ("inverter_1/charge_power_slot_1", "6000") in writes
    assert ("inverter_1/charge_current", "100") in writes
    assert _grid_charge_current_a(6.0) == 100
    assert _grid_charge_current_a(4.0) == 68  # 4000 / 58


def test_limit_home_not_due_during_active_charge_window():
    txt = "Chg 03:00-04:00 6kW cap19%"
    now = datetime(2026, 7, 20, 3, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert timer_charge_active_at(txt, now) is True
    due, end = limit_home_due_for_timer(txt, now, plan_hour=3)
    assert due is False
    assert end is None


def test_limit_home_not_due_when_charge_window_ended():
    """03:30 — Chg ended; Limit home path stays off (only Timed charge untick)."""
    txt = "Chg 03:00-03:30 6.0kW cap24%"
    now = datetime(2026, 7, 21, 3, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert timer_charge_active_at(txt, now) is False
    due, end = limit_home_due_for_timer(txt, now, plan_hour=3)
    assert due is False
    assert end is None


def test_charge_hour_repairs_on_grid_ups_desync():
    """Charge hour must move On-grid+UPS desync → Limit home + UPS before timer."""
    rows = [{
        "hour": 3,
        "start": "20-07-2026 04:00",
        "action": "Charging from Grid",
        "timer_schedule": "Chg 03:00-04:00 6kW cap19%",
        "grid_export": 0.0,
    }]
    cfg = {"smart_mode_enabled": True}
    now = datetime(2026, 7, 20, 3, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    async def run():
        with (
            patch("src.work_mode_scheduler.now_warsaw", return_value=now),
            patch("src.work_mode_scheduler.load_config", return_value=cfg),
            patch(
                "src.work_mode_scheduler._plan_rows",
                new_callable=AsyncMock,
                return_value=rows,
            ),
            patch(
                "src.work_mode_scheduler.sa_client.get_live_metrics",
                new_callable=AsyncMock,
                return_value={"battery_soc": 17.0},
            ),
            patch(
                "src.work_mode_scheduler.sa_client.get_rules",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "work_mode": WORK_MODE_ON_GRID,
                        "battery_discharge_mode": BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
                    },
                    {
                        "work_mode": WORK_MODE_LIMIT_HOME_LOAD,
                        "battery_discharge_mode": BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
                    },
                ],
            ),
            patch(
                "src.work_mode_scheduler.sa_client.apply_home_modes",
                new_callable=AsyncMock,
                return_value=True,
            ) as set_home,
        ):
            status = await run_work_mode_hour_start()

        assert status.get("charge_grid_prepare") is True
        assert status["work_mode_target"] == WORK_MODE_LIMIT_HOME_LOAD
        assert status["on_grid_trigger_this_slot"] is False
        set_home.assert_awaited_once_with(cfg)
        assert status["ok"] is True
        # After repair, modes must be the charge-safe pair.
        assert work_mode_battery_modes_paired(
            status["work_mode_after"],
            status["battery_discharge_mode_after"],
        )

    asyncio.run(run())
