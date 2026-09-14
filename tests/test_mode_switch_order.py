"""Ordered SA writes for export start/end: timed flags vs work/battery modes."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from src import hour_boundary_scheduler as hbs
from src import sa_client
from src.sa_client import (
    BATTERY_DISCHARGE_MODE_GRID_EXPORT,
    BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
    WORK_MODE_LIMIT_HOME_LOAD,
    WORK_MODE_ON_GRID,
)
from src.sqlite_store import write_plan


def _cfg():
    return {
        "smart_mode_enabled": True,
        "battery": {"capacity_kwh": 20.0},
        "simulation": {"min_soc_pct": 16},
        "inverter": {"ac_capacity_kw": 8.0},
        "sa": {"host": "localhost", "password": "x", "settings": {}},
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


def _plan(hour: int, timer: str, today: str = "2026-07-20") -> dict:
    return {
        "today_date": today,
        "plan_from_hour": hour,
        "history_rows": [],
        "rows": [{
            "hour": hour,
            "plan_date": today,
            "start": f"20-07-2026 {hour + 1:02d}:00",
            "timer_schedule": timer,
            "action": "Discharging to Grid and Load",
            "hour_labels_locked": True,
            "production": 0.0,
            "consumption": 0.5,
            "battery": -2.0,
            "bat_charge": 0.0,
            "bat_discharge": 2.0,
            "grid_import": 0.0,
            "grid_export": 1.5,
            "soc": 40.0,
            "buy_price": 1.0,
            "g12_zone": "peak",
            "q15": [],
        }],
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


def test_apply_export_start_modes_work_mode_then_battery():
    cfg = _cfg()
    order: list[str] = []

    async def bdm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"bdm:{mode}")
        return True

    async def wm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"wm:{mode}")
        return True

    async def run():
        with (
            patch("src.sa_client._get_enum_setting_lock") as lock_fn,
            patch("src.sa_client._set_battery_discharge_mode_only", side_effect=bdm_only),
            patch("src.sa_client._set_work_mode_only", side_effect=wm_only),
        ):
            class _Lock:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

            lock_fn.return_value = _Lock()
            ok = await sa_client.apply_export_start_modes(cfg)
        assert ok is True
        assert order == [
            f"wm:{WORK_MODE_ON_GRID}",
            f"bdm:{BATTERY_DISCHARGE_MODE_GRID_EXPORT}",
        ]

    asyncio.run(run())


def test_apply_export_start_modes_soft_fails_bdm():
    """On-grid OK + BDM verify fail → still True so timer can apply."""
    cfg = _cfg()
    order: list[str] = []

    async def bdm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"bdm:{mode}")
        return False

    async def wm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"wm:{mode}")
        return True

    async def run():
        with (
            patch("src.sa_client._get_enum_setting_lock") as lock_fn,
            patch("src.sa_client._set_battery_discharge_mode_only", side_effect=bdm_only),
            patch("src.sa_client._set_work_mode_only", side_effect=wm_only),
        ):
            class _Lock:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

            lock_fn.return_value = _Lock()
            ok = await sa_client.apply_export_start_modes(cfg)
        assert ok is True
        assert order == [
            f"wm:{WORK_MODE_ON_GRID}",
            f"bdm:{BATTERY_DISCHARGE_MODE_GRID_EXPORT}",
        ]

    asyncio.run(run())


def test_apply_export_start_modes_hard_fails_without_on_grid():
    """If On-grid does not confirm, do not write BDM and return False."""
    cfg = _cfg()
    order: list[str] = []

    async def bdm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"bdm:{mode}")
        return True

    async def wm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"wm:{mode}")
        return False

    async def run():
        with (
            patch("src.sa_client._get_enum_setting_lock") as lock_fn,
            patch("src.sa_client._set_battery_discharge_mode_only", side_effect=bdm_only),
            patch("src.sa_client._set_work_mode_only", side_effect=wm_only),
        ):
            class _Lock:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

            lock_fn.return_value = _Lock()
            ok = await sa_client.apply_export_start_modes(cfg)
        assert ok is False
        assert order == [f"wm:{WORK_MODE_ON_GRID}"]

    asyncio.run(run())


def test_apply_home_modes_work_mode_then_battery():
    cfg = _cfg()
    order: list[str] = []

    async def bdm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"bdm:{mode}")
        return True

    async def wm_only(cfg_arg, mode, **kwargs):
        del cfg_arg, kwargs
        order.append(f"wm:{mode}")
        return True

    async def run():
        with (
            patch("src.sa_client._get_enum_setting_lock") as lock_fn,
            patch("src.sa_client._set_battery_discharge_mode_only", side_effect=bdm_only),
            patch("src.sa_client._set_work_mode_only", side_effect=wm_only),
        ):
            class _Lock:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

            lock_fn.return_value = _Lock()
            ok = await sa_client.apply_home_modes(cfg)
        assert ok is True
        assert order == [
            f"wm:{WORK_MODE_LIMIT_HOME_LOAD}",
            f"bdm:{BATTERY_DISCHARGE_MODE_UPS_AND_HOME}",
        ]

    asyncio.run(run())


def test_limit_home_boundary_clears_timed_before_home_modes():
    """Export end at :45 — timed_discharge off, then Limit/UPS modes."""
    write_plan(_plan(20, "Dis 20:00-20:45 6.5kW cap16%"))
    now = datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Europe/Warsaw"))
    order: list[str] = []

    async def clear_flags(cfg, **kwargs):
        del cfg
        order.append("timed_off")
        assert kwargs.get("timed_discharge_enabled") is False
        return True

    async def home_modes(cfg):
        del cfg
        order.append("home_modes")
        return True

    async def get_rules(cfg, fresh=False):
        del cfg, fresh
        return {
            "work_mode": WORK_MODE_ON_GRID,
            "battery_discharge_mode": BATTERY_DISCHARGE_MODE_GRID_EXPORT,
            "timed_discharge_enabled": True,
            "discharge_slots": [{
                "from": "20:00", "to": "20:45", "power_kw": 6.5, "capacity_pct": 16,
            }],
        }

    async def get_metrics(cfg):
        del cfg
        return {"pv_power": 0.0, "battery_soc": 32.0}

    with (
        patch.object(hbs, "load_config", return_value=_cfg()),
        patch.object(hbs, "now_warsaw", return_value=now),
        patch.object(hbs.sa_client, "set_timed_power_flags", side_effect=clear_flags),
        patch.object(hbs.sa_client, "apply_home_modes", side_effect=home_modes),
        patch.object(hbs.sa_client, "get_rules", side_effect=get_rules),
        patch.object(hbs.sa_client, "get_live_metrics", side_effect=get_metrics),
        patch(
            "src.work_mode_scheduler.sa_client.apply_home_modes",
            side_effect=home_modes,
        ),
        patch("src.work_mode_scheduler.sa_client.get_rules", side_effect=get_rules),
        patch(
            "src.work_mode_scheduler.sa_client.get_live_metrics",
            side_effect=get_metrics,
        ),
        patch("src.work_mode_scheduler.load_config", return_value=_cfg()),
        patch("src.work_mode_scheduler.now_warsaw", return_value=now),
    ):
        status = asyncio.run(hbs.run_hour_boundary_limit_home())

    assert status["ok"] is True
    assert status["work_mode_limit"]["limit_due"] is True
    assert order[0] == "timed_off"
    assert "home_modes" in order
    assert order.index("timed_off") < order.index("home_modes")


def test_start_boundary_export_modes_before_timer_write():
    """Export start at :00 — WM→BDM (export start modes), then timed discharge on."""
    write_plan(_plan(20, "Dis 20:00-20:45 6.5kW cap16%"))
    now = datetime(2026, 7, 20, 20, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    order: list[str] = []

    async def export_start(cfg):
        del cfg
        order.append("export_start_modes")
        return True

    async def apply_schedule(cfg, schedule):
        del cfg
        order.append("timer_write")
        assert schedule.get("timed_discharge_enabled") is True
        return True

    async def get_rules(cfg, fresh=False):
        del cfg, fresh
        return {
            "work_mode": WORK_MODE_LIMIT_HOME_LOAD,
            "battery_discharge_mode": BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
            "timed_discharge_enabled": False,
            "timed_charge_enabled": False,
            "discharge_slots": [{"slot": 1, "from": "00:00", "to": "00:00",
                                 "capacity_pct": 16, "power_kw": 0}],
            "charge_slots": [{"slot": 1, "from": "00:00", "to": "00:00", "power_kw": 0}],
        }

    async def get_metrics(cfg):
        del cfg
        return {"pv_power": 0.0, "battery_soc": 50.0}

    with (
        patch.object(hbs, "load_config", return_value=_cfg()),
        patch.object(hbs, "now_warsaw", return_value=now),
        patch.object(hbs.sa_client, "get_rules", side_effect=get_rules),
        patch.object(hbs.sa_client, "get_live_metrics", side_effect=get_metrics),
        patch.object(hbs.sa_client, "apply_hourly_schedule_to_sa", side_effect=apply_schedule),
        patch("src.work_mode_scheduler.load_config", return_value=_cfg()),
        patch("src.work_mode_scheduler.now_warsaw", return_value=now),
        patch(
            "src.work_mode_scheduler.sa_client.apply_export_start_modes",
            side_effect=export_start,
        ),
        patch("src.work_mode_scheduler.sa_client.get_rules", side_effect=get_rules),
        patch(
            "src.work_mode_scheduler.sa_client.get_live_metrics",
            side_effect=get_metrics,
        ),
    ):
        status = asyncio.run(hbs.run_hour_boundary_start())

    assert status["ok"] is True
    assert status["work_mode"].get("on_grid_trigger_this_slot") is True
    assert order == ["export_start_modes", "timer_write"]
