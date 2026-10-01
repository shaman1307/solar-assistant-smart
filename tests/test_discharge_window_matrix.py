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
    return written, schedule["skip_timer_flags"]


def _zero_dropped(hour: int, live: list[dict]) -> list[int]:
    rules = {
        "timed_charge_enabled": False,
        "timed_discharge_enabled": True,
        "discharge_slots": live,
    }
    apply_mock = AsyncMock(return_value=True)
    with (
        patch.object(hbs.sa_client, "get_rules", AsyncMock(return_value=rules)),
        patch.object(hbs.sa_client, "apply_hourly_schedule_to_sa", apply_mock),
    ):
        status = asyncio.run(hbs._sync_timer_from_hour_row(_cfg(), [], hour))
    assert status["ok"] is True
    assert not apply_mock.await_count
    return []


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_unchanged_discharge_windows(n: int):
    rows = _rows(n)
    live = _empty_slots()
    written, skip_flags = _sync(rows, START, live, discharge_on=False)
    loaded = min(3, n)
    assert skip_flags is False
    assert [_signature(s) for s in written] == [
        (i + 1, f"{START + i:02d}:00", f"{START + i + 1:02d}:00", 8.0, 50 - i)
        for i in range(loaded)
    ]

    for step in range(1, n):
        hour = START + step
        before = [dict(slot) for slot in live]
        written, skip_flags = _sync(rows, hour, live, discharge_on=True)
        if step != 3:
            assert written == []
            assert skip_flags is True
            continue
        assert skip_flags is False
        expect = []
        for offset in range(3):
            src = step + offset
            if src < n:
                expect.append((
                    offset + 1,
                    f"{START + src:02d}:00",
                    f"{START + src + 1:02d}:00",
                    8.0,
                    50 - src,
                ))
            else:
                expect.append((
                    offset + 1,
                    "00:00",
                    "00:00",
                    0.0,
                    int(before[offset]["capacity_pct"]),
                ))
        assert [_signature(s) for s in written] == [
            sig for sig in expect
            if not (
                sig[1] == "00:00"
                and before[sig[0] - 1]["from"] == "00:00"
                and float(before[sig[0] - 1]["power_kw"]) == 0.0
            )
        ]


@pytest.mark.parametrize("n", [2, 3, 4, 5])
@pytest.mark.parametrize("field", ["power", "cap"])
def test_subsequent_power_or_cap_changes_during_the_open_hour(n: int, field: str):
    """Change later windows while hour 10 is open. The next boundary writes those slots."""
    overrides: dict[int, tuple[float, int]] = {}
    for hour in range(START + 1, START + n):
        power, cap = 8.0, 50 - (hour - START)
        if field == "power":
            power = 6.5
        else:
            cap = 29
        overrides[hour] = (power, cap)
    rows = _rows(n, overrides)
    live = _empty_slots()
    _sync(_rows(n), START, live, discharge_on=False)

    written, skip_flags = _sync(rows, START + 1, live, discharge_on=True)
    # The window that covers the open hour stays. Later slots still update.
    future = [h for h in range(START + 2, START + n) if h < START + 3]
    assert skip_flags is True
    assert [s["slot"] for s in written] == list(range(3, 3 + len(future)))
    assert all(int(s["from"][:2]) != START + 1 for s in written)
    for slot in written:
        hour = int(slot["from"][:2])
        if field == "power":
            assert slot["power_kw"] == 6.5
        else:
            assert slot["capacity_pct"] == 29
        assert hour in future

    if n > 3:
        far = START + 3
        assert all(s["from"] != f"{far:02d}:00" for s in written)
        refilled, skip_flags = _sync(rows, far, live, discharge_on=True)
        assert skip_flags is False
        head = refilled[0]
        assert head["from"] == f"{far:02d}:00"
        if field == "power":
            assert head["power_kw"] == 6.5
        else:
            assert head["capacity_pct"] == 29


@pytest.mark.parametrize(
    ("n", "drop_offsets"),
    [
        (2, (1,)),
        (3, (1,)),
        (3, (2,)),
        (3, (1, 2)),
        (4, (1,)),
        (4, (2,)),
        (4, (3,)),
        (4, (1, 2)),
        (4, (1, 2, 3)),
        (5, (1,)),
        (5, (2,)),
        (5, (3,)),
        (5, (1, 2)),
        (5, (2, 3)),
        (5, (1, 2, 3)),
        (5, (1, 2, 3, 4)),
    ],
)
def test_subsequent_windows_disappear_during_the_open_hour(n: int, drop_offsets: tuple[int, ...]):
    """Later windows can be cleared before they start. The open hour's window stays."""
    if max(drop_offsets) >= n:
        pytest.skip("drop is past the end of this run")
    dropped = {START + offset for offset in drop_offsets}
    rows = [row for row in _rows(n) if row["hour"] not in dropped]
    live = _empty_slots()
    _sync(_rows(n), START, live, discharge_on=False)

    for hour in range(START + 1, START + n):
        if hour in dropped:
            before = [dict(slot) for slot in live]
            _zero_dropped(hour, live)
            for prev, slot in zip(before, live):
                if prev["from"] == f"{hour:02d}:00":
                    assert slot["from"] == prev["from"]
                    assert slot["to"] == prev["to"]
                    assert float(slot["power_kw"]) == float(prev["power_kw"])
            continue
        written, _skip = _sync(rows, hour, live, discharge_on=True)
        for slot in written:
            if slot["from"] == "00:00":
                assert slot["power_kw"] == 0.0
                continue
            assert int(slot["from"][:2]) not in dropped


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
