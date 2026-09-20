"""SA timer sync rules."""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.timer_plan import build_sa_schedule_from_hour_row, sa_schedule_matches_plan_row


def _cfg():
    return {
        "battery": {"capacity_kwh": 20.0, "max_discharge_power_kw": 8.0},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {"min_soc_pct": 16},
    }


def test_sa_schedule_mismatch_when_sa_has_stale_discharge_slot():
    rows = [{
        "hour": 8,
        "start": "07-07-2026 09:00",
        "action": "Discharging to Grid and Load",
        "timer_schedule": "Dis 08:00-08:45 8.0kW cap16%",
    }]
    rules = {
        "timed_discharge_enabled": False,
        "timed_charge_enabled": False,
        "discharge_slots": [{
            "slot": 1, "from": "07:00", "to": "08:00",
            "capacity_pct": 16, "power_kw": 8.0, "voltage_v": 42.0,
        }],
        "charge_slots": [{"slot": 1, "from": "00:00", "to": "00:00", "power_kw": 0}],
    }
    cfg = _cfg()
    assert sa_schedule_matches_plan_row(rows, 8, cfg, rules) is False

    expected = build_sa_schedule_from_hour_row(rows, 8, cfg, existing=rules)
    assert expected is not None
    assert expected["timed_discharge_enabled"] is True
    dis = expected["discharge_slots"][0]
    assert dis["from"] == "08:00"
    assert dis["to"] == "08:45"


def test_sa_schedule_matches_when_sa_matches_plan():
    rows = [{
        "hour": 8,
        "start": "07-07-2026 09:00",
        "action": "Discharging to Grid and Load",
        "timer_schedule": "Dis 08:00-08:45 8.0kW cap16%",
    }]
    cfg = _cfg()
    rules = {
        "timed_discharge_enabled": True,
        "timed_charge_enabled": False,
        "discharge_slots": [{
            "slot": 1, "from": "08:00", "to": "08:45",
            "capacity_pct": 16, "power_kw": 8.0, "voltage_v": 42.0,
        }],
        "charge_slots": [{"slot": 1, "from": "00:00", "to": "00:00", "power_kw": 0}],
    }
    assert sa_schedule_matches_plan_row(rows, 8, cfg, rules) is True


def test_sa_charge_cap_at_least_five_above_live_soc():
    rows = [{
        "hour": 22,
        "start": "13-09-2026 23:00",
        "action": "Charging from Grid",
        "timer_schedule": "Chg 22:00-22:30 4.0kW cap18%",
    }]
    rules = {
        "timed_charge_enabled": True,
        "timed_discharge_enabled": False,
        "charge_slots": [{
            "slot": 1, "from": "22:00", "to": "22:30",
            "capacity_pct": 18, "power_kw": 4.0, "voltage_v": 56.0,
        }],
        "discharge_slots": [{"slot": 1, "from": "00:00", "to": "00:00", "power_kw": 0}],
    }
    expected = build_sa_schedule_from_hour_row(
        rows, 22, _cfg(), existing=rules, live_soc_pct=17.0,
    )
    assert expected is not None
    assert expected["charge_slots"][0]["capacity_pct"] == 22


def test_write_metrics_retries_crc_then_succeeds():
    """CRC on the first WS+REST pass retries so a :00 Chg write is not idle until :15."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from src import sa_client

    n_set = {"n": 0}

    class Sock:
        async def set_setting(self, topic, value):
            del topic, value
            n_set["n"] += 1
            if n_set["n"] == 1:
                raise ValueError("CRC error")

        async def close(self):
            return None

    class Rest:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def set_metric(self, topic, value):
            del topic, value
            raise RuntimeError("API error 422: CRC error")

    async def fake_connect(opts):
        del opts
        return Sock()

    cfg = {"sa": {"host": "127.0.0.1", "password": "x"}}
    with (
        patch.object(sa_client, "_acquire_sa_lock", AsyncMock(return_value=True)),
        patch.object(sa_client, "_release_sa_lock"),
        patch.object(sa_client, "_build_client", return_value=Rest()),
        patch.object(sa_client.asyncio, "sleep", AsyncMock()) as sleep,
        patch("py_solar_assistant.connect", fake_connect),
    ):
        asyncio.run(sa_client._write_metrics(cfg, [("inverter_1/timed_charge", "1")]))
    assert n_set["n"] == 2
    sleep.assert_awaited_once()
    assert sleep.await_args.args[0] == 2.0

