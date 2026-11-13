"""SA timer sync rules."""

import asyncio
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.sa_client import apply_hourly_schedule_to_sa
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


def _dis_row(hour: int, timer: str) -> dict:
    return {
        "hour": hour,
        "plan_date": "2026-09-23",
        "start": f"23-09-2026 {hour + 1:02d}:00",
        "action": "Discharging to Grid and Load",
        "timer_schedule": timer,
    }


def _empty_dis_slots() -> list[dict]:
    return [
        {"slot": n, "from": "00:00", "to": "00:00", "capacity_pct": 15, "power_kw": 0.0, "voltage_v": 42.0}
        for n in (1, 2, 3)
    ]


def test_same_power_full_hours_are_one_window_with_the_last_soc():
    rows = [
        _dis_row(19, "Dis 19:00-20:00 8.0kW cap47%"),
        _dis_row(20, "Dis 20:00-21:00 8.0kW cap44%"),
        _dis_row(21, "Dis 21:00-22:00 8.0kW cap40%"),
        _dis_row(22, "Dis 22:00-23:00 8.0kW cap36%"),
    ]
    schedule = build_sa_schedule_from_hour_row(rows, 19, _cfg(), existing={
        "timed_discharge_enabled": False,
        "discharge_slots": _empty_dis_slots(),
    })
    assert schedule is not None
    written = schedule["discharge_slots_to_write"]
    assert [(s["slot"], s["from"], s["to"], s["capacity_pct"]) for s in written] == [
        (1, "19:00", "23:00", 36),
    ]


def test_later_hour_of_the_same_window_sends_nothing():
    rows = [
        _dis_row(19, "Dis 19:00-20:00 8.0kW cap47%"),
        _dis_row(20, "Dis 20:00-21:00 8.0kW cap44%"),
        _dis_row(21, "Dis 21:00-22:00 8.0kW cap40%"),
        _dis_row(22, "Dis 22:00-23:00 8.0kW cap36%"),
    ]
    live = _empty_dis_slots()
    live[0] = {
        "slot": 1, "from": "19:00", "to": "23:00",
        "capacity_pct": 36, "power_kw": 8.0, "voltage_v": 42.0,
    }
    schedule = build_sa_schedule_from_hour_row(rows, 21, _cfg(), existing={
        "timed_discharge_enabled": True,
        "timed_charge_enabled": False,
        "discharge_slots": live,
    })
    assert schedule is not None
    assert schedule["discharge_slots_to_write"] == []


def test_changed_power_sends_that_hour_and_clears_the_old_window():
    rows = [
        _dis_row(19, "Dis 19:00-20:00 8.0kW cap47%"),
        _dis_row(20, "Dis 20:00-21:00 8.0kW cap44%"),
        _dis_row(21, "Dis 21:00-22:00 7.5kW cap40%"),
        _dis_row(22, "Dis 22:00-23:00 8.0kW cap30%"),
    ]
    live = _empty_dis_slots()
    live[0] = {
        "slot": 1, "from": "19:00", "to": "21:00",
        "capacity_pct": 44, "power_kw": 8.0, "voltage_v": 42.0,
    }
    schedule = build_sa_schedule_from_hour_row(rows, 21, _cfg(), existing={
        "timed_discharge_enabled": True,
        "timed_charge_enabled": False,
        "discharge_slots": live,
    })
    assert schedule is not None
    written = schedule["discharge_slots_to_write"]
    assert [(s["slot"], s["from"], s["to"], s["power_kw"], s["capacity_pct"]) for s in written] == [
        (1, "21:00", "22:00", 7.5, 40),
    ]


def test_changed_window_keeps_a_checkbox_that_is_already_on():
    rows = [
        _dis_row(19, "Dis 19:00-20:00 8.0kW cap47%"),
        _dis_row(21, "Dis 21:00-22:00 7.5kW cap40%"),
    ]
    schedule = build_sa_schedule_from_hour_row(rows, 21, _cfg(), existing={
        "timed_discharge_enabled": True,
        "timed_charge_enabled": False,
        "discharge_slots": _empty_dis_slots(),
    })
    assert schedule is not None
    assert schedule["discharge_slots_to_write"]
    sent: dict = {}

    async def capture(cfg, slim):
        del cfg
        sent.update(slim)
        return True

    with patch("src.sa_client.set_timer_schedule", side_effect=capture):
        ok = asyncio.run(apply_hourly_schedule_to_sa({}, schedule))
    assert ok is True
    assert sent["timed_discharge_enabled"] is None
    assert sent["timed_charge_enabled"] is None
    assert sent["discharge_slots"][0]["power_kw"] == 7.5


def test_partial_last_hour_is_sent_on_its_own():
    rows = [
        _dis_row(19, "Dis 19:00-20:00 8.0kW cap47%"),
        _dis_row(20, "Dis 20:00-21:00 8.0kW cap44%"),
        _dis_row(21, "Dis 21:00-21:30 5.0kW cap16%"),
    ]
    live = _empty_dis_slots()
    live[0] = {
        "slot": 1, "from": "19:00", "to": "21:00",
        "capacity_pct": 44, "power_kw": 8.0, "voltage_v": 42.0,
    }
    schedule = build_sa_schedule_from_hour_row(rows, 21, _cfg(), existing={
        "timed_discharge_enabled": True,
        "timed_charge_enabled": False,
        "discharge_slots": live,
    })
    assert schedule is not None
    written = schedule["discharge_slots_to_write"]
    assert [(s["slot"], s["from"], s["to"], s["power_kw"], s["capacity_pct"]) for s in written] == [
        (1, "21:00", "21:30", 5.0, 16),
    ]


def test_opening_hour_sends_even_when_an_old_slot_has_the_same_values():
    """Hour 20 starts its own run. A leftover slot with the same numbers is not the previous hour."""
    rows = [
        _dis_row(20, "Dis 20:00-21:00 8.0kW cap44%"),
        _dis_row(22, "Dis 22:00-23:00 8.0kW cap36%"),
    ]
    live = [
        {"slot": 1, "from": "19:00", "to": "20:00", "capacity_pct": 47, "power_kw": 8.0, "voltage_v": 42.0},
        {"slot": 2, "from": "20:00", "to": "21:00", "capacity_pct": 44, "power_kw": 8.0, "voltage_v": 42.0},
        {"slot": 3, "from": "21:00", "to": "22:00", "capacity_pct": 40, "power_kw": 8.0, "voltage_v": 42.0},
    ]
    schedule = build_sa_schedule_from_hour_row(rows, 20, _cfg(), existing={
        "timed_discharge_enabled": True,
        "discharge_slots": live,
    })
    assert schedule is not None
    written = schedule["discharge_slots_to_write"]
    assert written[0]["from"] == "20:00"
    assert written[0]["to"] == "21:00"
    assert written[0]["power_kw"] == 8.0
    assert written[0]["capacity_pct"] == 44


def test_same_power_run_is_rewritten_when_the_recorded_window_is_short():
    rows = [
        _dis_row(19, "Dis 19:00-20:00 8.0kW cap47%"),
        _dis_row(20, "Dis 20:00-21:00 8.0kW cap44%"),
        _dis_row(21, "Dis 21:00-22:00 8.0kW cap40%"),
        _dis_row(22, "Dis 22:00-23:00 8.0kW cap36%"),
        _dis_row(23, "Dis 23:00-00:00 8.0kW cap32%"),
    ]
    live = [
        {"slot": 1, "from": "19:00", "to": "20:00", "capacity_pct": 47, "power_kw": 8.0, "voltage_v": 42.0},
        {"slot": 2, "from": "20:00", "to": "21:00", "capacity_pct": 44, "power_kw": 8.0, "voltage_v": 42.0},
        {"slot": 3, "from": "21:00", "to": "22:00", "capacity_pct": 40, "power_kw": 8.0, "voltage_v": 42.0},
    ]
    schedule = build_sa_schedule_from_hour_row(rows, 22, _cfg(), existing={
        "timed_discharge_enabled": True,
        "discharge_slots": live,
    })
    assert schedule is not None
    written = [(s["slot"], s["from"], s["to"], s["power_kw"]) for s in schedule["discharge_slots_to_write"]]
    assert written[0] == (1, "19:00", "00:00", 8.0)
    assert schedule["discharge_slots"][0]["capacity_pct"] == 32
    assert [item[0] for item in written if item[1] == "00:00"] == [2, 3]


def test_charge_same_power_hours_use_the_last_soc_then_skip():
    def _chg(hour: int, cap: int) -> dict:
        return {
            "hour": hour,
            "plan_date": "2026-11-12",
            "start": f"12-11-2026 {hour + 1:02d}:00",
            "action": "Charging from Grid",
            "timer_schedule": f"Chg {hour:02d}:00-{hour + 1:02d}:00 5.8kW cap{cap}%",
        }

    rows = [_chg(13, 56), _chg(14, 80)]
    cfg = _cfg()
    first = build_sa_schedule_from_hour_row(rows, 13, cfg, existing={
        "timed_charge_enabled": False,
        "charge_slots": _empty_dis_slots(),
    })
    assert first is not None
    written = first["charge_slots_to_write"]
    assert [(s["slot"], s["from"], s["to"], s["power_kw"], s["capacity_pct"]) for s in written] == [
        (1, "13:00", "15:00", 5.8, 80),
    ]
    live = _empty_dis_slots()
    live[0] = dict(written[0])
    second = build_sa_schedule_from_hour_row(rows, 14, cfg, existing={
        "timed_charge_enabled": True,
        "timed_discharge_enabled": False,
        "charge_slots": live,
    })
    assert second is not None
    assert second["charge_slots_to_write"] == []


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

