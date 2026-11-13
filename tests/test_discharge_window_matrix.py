"""Discharge slot window: 2–5 hours, unchanged, retuned, or dropped."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from src import hour_boundary_scheduler as hbs
from src import sa_client
from src.timer_plan import build_sa_schedule_from_hour_row

START = 10


def _cfg():
    return {
        "battery": {"capacity_kwh": 20.0, "max_discharge_power_kw": 8.0},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {"min_soc_pct": 16},
    }


def _timer(hour: int, power: float, cap: int) -> str:
    return f"Dis {hour:02d}:00-{hour + 1:02d}:00 {power:.1f}kW cap{cap}%"


def _row(hour: int, power: float = 8.0, cap: int | None = None) -> dict:
    if cap is None:
        cap = 50 - (hour - START)
    return {
        "hour": hour,
        "plan_date": "2026-09-23",
        "start": f"23-09-2026 {hour + 1:02d}:00",
        "action": "Discharging to Grid and Load",
        "timer_schedule": _timer(hour, power, cap),
    }


def _rows(n: int, by_hour: dict[int, tuple[float, int]] | None = None) -> list[dict]:
    by_hour = by_hour or {}
    rows = []
    for hour in range(START, START + n):
        power, cap = by_hour.get(hour, (8.0, 50 - (hour - START)))
        rows.append(_row(hour, power, cap))
    return rows


def _empty_slots() -> list[dict]:
    return [
        {
            "slot": n,
            "from": "00:00",
            "to": "00:00",
            "capacity_pct": 15,
            "power_kw": 0.0,
            "voltage_v": 42.0,
        }
        for n in (1, 2, 3)
    ]


def _signature(slot: dict) -> tuple:
    return (
        slot["slot"],
        slot["from"],
        slot["to"],
        float(slot["power_kw"]),
        int(slot["capacity_pct"]),
    )


def _sync(rows: list[dict], hour: int, live: list[dict], *, discharge_on: bool):
    schedule = build_sa_schedule_from_hour_row(
        rows,
        hour,
        _cfg(),
        existing={
            "timed_discharge_enabled": discharge_on,
            "timed_charge_enabled": False,
            "discharge_slots": live,
        },
    )
    assert schedule is not None
    written = list(schedule["discharge_slots_to_write"])
    for slot in written:
        live[int(slot["slot"]) - 1] = dict(slot)
    return written


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_unchanged_discharge_windows(n: int):
    rows = _rows(n)
    live = _empty_slots()
    written = _sync(rows, START, live, discharge_on=False)
    last_cap = 50 - (n - 1)
    end_hour = START + n
    assert [_signature(s) for s in written] == [
        (1, f"{START:02d}:00", f"{end_hour:02d}:00", 8.0, last_cap),
    ]

    for step in range(1, n):
        written = _sync(rows, START + step, live, discharge_on=True)
        assert written == []


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_later_power_change_rewrites_the_window(n: int):
    """A later hour with a different power is sent as its own run."""
    overrides = {
        hour: (6.5, 50 - (hour - START))
        for hour in range(START + 1, START + n)
    }
    rows = _rows(n, overrides)
    live = _empty_slots()
    _sync(_rows(n), START, live, discharge_on=False)

    written = _sync(rows, START + 1, live, discharge_on=True)
    assert written
    head = written[0]
    assert head["from"] == f"{START + 1:02d}:00"
    assert head["to"] == f"{START + n:02d}:00"
    assert head["power_kw"] == 6.5
    assert head["capacity_pct"] == 50 - (n - 1)


def test_dropped_tail_shortens_the_recorded_window():
    rows = _rows(4)
    live = _empty_slots()
    _sync(rows, START, live, discharge_on=False)
    kept = [row for row in rows if row["hour"] != START + 3]
    written = _sync(kept, START + 1, live, discharge_on=True)
    assert written[0]["from"] == f"{START:02d}:00"
    assert written[0]["to"] == f"{START + 3:02d}:00"
    assert written[0]["capacity_pct"] == 50 - 2


def _idle_row(hour: int) -> dict:
    return {
        "hour": hour,
        "plan_date": "2026-09-23",
        "start": f"23-09-2026 {hour + 1:02d}:00",
        "action": "Idle",
        "timer_schedule": "",
        "grid_export": 0.0,
    }


def _end_registers(rows: list[dict], live: list[dict], hour: int) -> list:
    """Registers written by the :00 job at *hour*."""
    writes: list = []
    rules = {
        "work_mode": sa_client.WORK_MODE_ON_GRID,
        "battery_discharge_mode": sa_client.BATTERY_DISCHARGE_MODE_GRID_EXPORT,
        "timed_charge_enabled": False,
        "timed_discharge_enabled": True,
        "discharge_slots": [dict(slot) for slot in live],
        "charge_slots": _empty_slots(),
    }

    async def write_metrics(cfg, pairs, **kwargs):
        del cfg, kwargs
        writes.extend(pairs)

    async def grid_charge(cfg, *, enabled, **kwargs):
        del cfg, kwargs
        writes.append(("inverter_1/grid_charge", "Enabled" if enabled else "Disabled"))
        return True

    async def work_mode(cfg, mode, **kwargs):
        del cfg, kwargs
        writes.append(("work_mode", mode))
        return True

    async def battery_mode(cfg, mode, **kwargs):
        del cfg, kwargs
        writes.append(("battery_discharge_mode", mode))
        return True

    class _Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    now = datetime(2026, 9, 23, hour, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    cfg = {**_cfg(), "smart_mode_enabled": True, "sa": {"settings": {}}}
    with (
        patch.object(hbs, "load_config", return_value=cfg),
        patch.object(hbs, "_plan_rows", AsyncMock(return_value=rows)),
        patch.object(hbs, "now_warsaw", return_value=now),
        patch.object(hbs.sa_client, "get_rules", AsyncMock(return_value=rules)),
        patch.object(hbs.sa_client, "get_live_metrics", AsyncMock(return_value={"battery_soc": 40.0})),
        patch.object(hbs.sa_client, "_write_metrics", side_effect=write_metrics),
        patch.object(hbs.sa_client, "set_grid_charging", side_effect=grid_charge),
        patch.object(hbs.sa_client, "_set_work_mode_only", side_effect=work_mode),
        patch.object(hbs.sa_client, "_set_battery_discharge_mode_only", side_effect=battery_mode),
        patch.object(hbs.sa_client, "_get_enum_setting_lock", return_value=_Lock()),
        patch("src.work_mode_scheduler.load_config", return_value=cfg),
        patch("src.work_mode_scheduler._plan_rows", AsyncMock(return_value=rows)),
        patch("src.work_mode_scheduler.sa_client.get_rules", AsyncMock(return_value=rules)),
        patch("src.work_mode_scheduler.sa_client.get_live_metrics", AsyncMock(return_value={"battery_soc": 40.0, "pv_power": 0.0})),
        patch("src.work_mode_scheduler.sa_client.ensure_paired_battery_discharge_mode", AsyncMock(return_value=True)),
        patch("src.work_mode_scheduler.sa_client._write_metrics", side_effect=write_metrics),
        patch("src.work_mode_scheduler.sa_client.set_grid_charging", side_effect=grid_charge),
        patch("src.work_mode_scheduler.sa_client._set_work_mode_only", side_effect=work_mode),
        patch("src.work_mode_scheduler.sa_client._set_battery_discharge_mode_only", side_effect=battery_mode),
        patch("src.work_mode_scheduler.sa_client._get_enum_setting_lock", return_value=_Lock()),
    ):
        status = asyncio.run(hbs.run_hour_boundary_start(now))
    assert status["ok"] is not False
    return writes


_END_TAIL = [
    ("inverter_1/timed_charge", "0"),
    ("inverter_1/timed_discharge", "0"),
    ("inverter_1/grid_charge", "Disabled"),
    ("work_mode", sa_client.WORK_MODE_LIMIT_HOME_LOAD),
    ("battery_discharge_mode", sa_client.BATTERY_DISCHARGE_MODE_UPS_AND_HOME),
]


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_last_export_hour_then_writes_end_registers(n: int):
    rows = _rows(n)
    live = _empty_slots()
    _sync(rows, START, live, discharge_on=False)
    for step in range(1, n):
        _sync(rows, START + step, live, discharge_on=True)
    end_hour = START + n
    writes = _end_registers(rows + [_idle_row(end_hour)], live, end_hour)
    assert writes[-len(_END_TAIL):] == _END_TAIL


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_open_export_hour_does_not_write_end_registers(n: int):
    if n < 2:
        return
    rows = _rows(n)
    live = _empty_slots()
    _sync(rows, START, live, discharge_on=False)
    writes = _end_registers(rows, live, START + 1)
    assert ("inverter_1/timed_discharge", "0") not in writes
    assert ("work_mode", sa_client.WORK_MODE_LIMIT_HOME_LOAD) not in writes
