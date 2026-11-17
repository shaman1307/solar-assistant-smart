"""Nov 17 2026: one inverter write per charge/discharge window across the whole day.

Plan timers:
  H00–H05  Chg 5.8 kW  → one window 00:00–06:00; H05 plan SOC moves 80→81 at 02:45,
                         so the last hour resends the window with 81
  H15–H16  Dis 8.0 kW  → one window 15:00–17:00
  H17      Dis 7.5 kW  → one window 17:00–18:00

Each tick reads the plan the way SQLite holds it: ``rows`` start at the current
hour and earlier hours of today sit in ``history_rows``.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from src import hour_boundary_scheduler as hbs
from src import sa_client

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-11-17"
TOMORROW = "2026-11-18"

CHARGE_CAPS = {0: 26, 1: 37, 2: 47, 3: 59, 4: 70, 5: 80}
DISCHARGE = {15: (8.0, 52), 16: (8.0, 50), 17: (7.5, 48)}


def _cfg() -> dict:
    return {
        "smart_mode_enabled": True,
        "battery": {"capacity_kwh": 47.6, "max_discharge_power_kw": 8.0, "max_charge_power_kw": 5.8},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {"min_soc_pct": 16},
    }


def _timer(hour: int, tick_min: int) -> str:
    if hour in CHARGE_CAPS:
        cap = CHARGE_CAPS[hour]
        if hour == 5 and tick_min >= 2 * 60 + 45:
            cap = 81
        return f"Chg {hour:02d}:00-{hour + 1:02d}:00 5.8kW cap{cap}%"
    if hour in DISCHARGE:
        kw, cap = DISCHARGE[hour]
        return f"Dis {hour:02d}:00-{hour + 1:02d}:00 {kw}kW cap{cap}%"
    return ""


def _action(timer: str) -> str:
    if timer.startswith("Chg"):
        return "Charging from Grid"
    if timer.startswith("Dis"):
        return "Discharging to Grid and Load"
    return "Discharging to Load"


def _row(plan_date: str, hour: int, timer: str) -> dict:
    day = plan_date[8:10] + "-" + plan_date[5:7] + "-" + plan_date[:4]
    return {
        "hour": hour,
        "plan_date": plan_date,
        "start": f"{day} {hour + 1:02d}:00" if hour < 23 else f"{day} 00:00",
        "timer_schedule": timer,
        "action": _action(timer),
    }


def _plan_at(tick: datetime) -> dict:
    tick_min = tick.hour * 60 + tick.minute
    today = {h: _row(TODAY, h, _timer(h, tick_min)) for h in range(24)}
    rows = [today[h] for h in range(tick.hour, 24)]
    rows += [_row(TOMORROW, h, "") for h in range(0, tick.hour)]
    return {
        "today_date": TODAY,
        "plan_from_hour": tick.hour,
        "rows": rows,
        "history_rows": [today[h] for h in range(0, tick.hour)],
    }


def _empty_slots(kind: str) -> list[dict]:
    voltage = 56.0 if kind == "charge" else 42.0
    return [
        {"slot": n, "from": "00:00", "to": "00:00", "capacity_pct": 15, "power_kw": 0.0, "voltage_v": voltage}
        for n in (1, 2, 3)
    ]


class FakeInverter:
    def __init__(self) -> None:
        self.state = {
            "timed_charge_enabled": False,
            "timed_discharge_enabled": False,
            "work_mode": "Limit power to home load",
            "battery_discharge_mode": "UPS and home loads",
            "charge_current_a": 100,
            "charge_slots": _empty_slots("charge"),
            "discharge_slots": _empty_slots("discharge"),
        }
        self.window_writes: list[tuple[str, str, str, str, float, int]] = []
        self.tick = ""

    async def get_rules(self, cfg: dict, *args, **kwargs) -> dict:
        del cfg, args, kwargs
        return copy.deepcopy(self.state)

    async def set_timer_schedule(self, cfg: dict, schedule: dict) -> bool:
        del cfg
        for key, kind in (("charge_slots", "chg"), ("discharge_slots", "dis")):
            for slot in schedule.get(key) or []:
                live = self.state[key]
                live[int(slot["slot"]) - 1] = {**live[int(slot["slot"]) - 1], **slot}
                if float(slot.get("power_kw") or 0) > 0:
                    self.window_writes.append((
                        self.tick, kind, slot["from"], slot["to"],
                        float(slot["power_kw"]), int(slot["capacity_pct"]),
                    ))
        for flag in ("timed_charge_enabled", "timed_discharge_enabled"):
            if schedule.get(flag) is not None:
                self.state[flag] = bool(schedule[flag])
        return True

    async def set_timed_power_flags(
        self, cfg: dict, *, timed_charge_enabled=None, timed_discharge_enabled=None,
    ) -> bool:
        del cfg
        if timed_charge_enabled is not None:
            self.state["timed_charge_enabled"] = bool(timed_charge_enabled)
        if timed_discharge_enabled is not None:
            self.state["timed_discharge_enabled"] = bool(timed_discharge_enabled)
        return True


def _run_day() -> FakeInverter:
    inverter = FakeInverter()
    work_mode_ok = AsyncMock(return_value={"ok": True, "skipped": True, "skip_reason": "already_set"})
    limit_ok = AsyncMock(return_value={"ok": True, "skipped": True, "limit_due": False})

    for hour in range(24):
        for minute in (0, 15, 31, 45):
            tick = datetime(2026, 11, 17, hour, minute, 10, tzinfo=TZ)
            inverter.tick = tick.strftime("%H:%M")
            plan = _plan_at(tick)
            with (
                patch.object(hbs, "now_warsaw", return_value=tick),
                patch.object(hbs, "load_config", return_value=_cfg()),
                patch.object(hbs, "read_plan", return_value=plan),
                patch.object(hbs, "run_work_mode_hour_start", work_mode_ok),
                patch.object(hbs, "run_work_mode_limit_home", limit_ok),
                patch.object(sa_client, "get_rules", inverter.get_rules),
                patch.object(sa_client, "set_timer_schedule", inverter.set_timer_schedule),
                patch.object(sa_client, "set_timed_power_flags", inverter.set_timed_power_flags),
            ):
                if minute == 0:
                    status = asyncio.run(hbs.run_hour_boundary_start(now=tick))
                else:
                    status = asyncio.run(hbs.run_hour_boundary_limit_home(now=tick))
            assert status["ok"] is not False, (inverter.tick, status)
    return inverter


def test_nov17_day_sends_one_write_per_window_and_last_hour_soc():
    inverter = _run_day()
    assert inverter.window_writes == [
        ("00:00", "chg", "00:00", "06:00", 5.8, 80),
        ("05:00", "chg", "00:00", "06:00", 5.8, 81),
        ("15:00", "dis", "15:00", "17:00", 8.0, 50),
        ("17:00", "dis", "17:00", "18:00", 7.5, 48),
    ]
