"""Quarter reading of capacity settings versus the saved configuration."""

import asyncio

from src.routes.config import capacity_register_writes
from src.inverter_setting_alarm import (
    alarm_status,
    capacity_setting_mismatches,
    overlay_live_capacity,
    reconcile_inverter_capacity,
    reset_capacity_sync_state,
    set_test_alarm,
)


def _cfg(**inv) -> dict:
    return {
        "inverter": {
            "to_grid_battery_capacity": 16,
            "back_to_battery_capacity": 26,
            **inv,
        },
        "alert_email": "ops@example.com",
    }


def test_matching_reading_is_not_a_mismatch():
    assert capacity_setting_mismatches(
        _cfg(),
        {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 26},
    ) == []


def test_inverter_100_against_saved_26_is_a_mismatch():
    rows = capacity_setting_mismatches(
        _cfg(),
        {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 100},
    )
    assert rows == [{
        "key": "back_to_battery_capacity",
        "label": "Back to battery capacity (%)",
        "inverter": 100,
        "configured": 26,
    }]


def test_missing_inverter_reading_is_not_a_mismatch():
    assert capacity_setting_mismatches(
        _cfg(),
        {"to_grid_battery_capacity": None, "back_to_battery_capacity": None},
    ) == []


def _match() -> dict:
    return {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 26}


def _mismatch(back: int = 60) -> dict:
    return {"to_grid_battery_capacity": 16, "back_to_battery_capacity": back}


def _run(initial: dict, reads: list[dict]) -> tuple[dict, list, list, list]:
    reset_capacity_sync_state()
    sent: list = []
    writes: list = []
    sleeps: list = []
    pending = list(reads)

    async def read():
        return pending.pop(0)

    async def write(items):
        writes.append(list(items))

    async def sleep():
        sleeps.append(1)

    latest = asyncio.run(reconcile_inverter_capacity(
        _cfg(),
        initial,
        read=read,
        write=write,
        sleep=sleep,
        send_email=lambda _cfg, rows: sent.append(rows),
    ))
    return latest, writes, sleeps, sent


def test_three_failed_writes_send_one_alarm_and_return_the_inverter_value():
    latest, writes, sleeps, sent = _run(_mismatch(60), [_mismatch(60), _mismatch(60), _mismatch(60)])
    assert len(writes) == 3
    assert writes[0] == [("inverter_1/back_to_battery_capacity", "26")]
    assert len(sleeps) == 3
    assert len(sent) == 1
    assert sent[0][0]["inverter"] == 60
    assert latest["back_to_battery_capacity"] == 60
    assert alarm_status()["active"] is True


def test_a_read_that_matches_skips_the_remaining_writes():
    latest, writes, sleeps, sent = _run(_mismatch(100), [_match()])
    assert writes == [[("inverter_1/back_to_battery_capacity", "26")]]
    assert sleeps == [1]
    assert sent == []
    assert latest == _match()
    assert alarm_status()["active"] is False


def test_the_next_divergence_after_a_successful_write_alarms_without_writing():
    _run(_mismatch(40), [_match()])
    sent: list = []
    writes: list = []

    async def unused_read():
        raise AssertionError("read")

    async def write(items):
        writes.append(items)

    latest = asyncio.run(reconcile_inverter_capacity(
        _cfg(),
        _mismatch(60),
        read=unused_read,
        write=write,
        sleep=unused_read,
        send_email=lambda _cfg, rows: sent.append(rows),
    ))
    assert writes == []
    assert len(sent) == 1
    assert latest["back_to_battery_capacity"] == 60
    assert alarm_status()["active"] is True


def test_a_held_alarm_does_not_write_or_email_again():
    _run(_mismatch(60), [_mismatch(60), _mismatch(60), _mismatch(60)])
    sent: list = []
    writes: list = []

    async def unused():
        raise AssertionError("unused")

    latest = asyncio.run(reconcile_inverter_capacity(
        _cfg(),
        _mismatch(70),
        read=unused,
        write=unused,
        sleep=unused,
        send_email=lambda _cfg, rows: sent.append(rows),
    ))
    assert writes == []
    assert sent == []
    assert latest["back_to_battery_capacity"] == 70
    assert alarm_status()["active"] is True


def test_incomplete_reading_does_not_clear_the_alarm_or_send_again():
    _run(_mismatch(60), [_mismatch(60), _mismatch(60), _mismatch(60)])
    sent: list = []
    async def unused():
        raise AssertionError("unused")

    held = asyncio.run(reconcile_inverter_capacity(
        _cfg(),
        {"to_grid_battery_capacity": None, "back_to_battery_capacity": None},
        read=unused,
        write=unused,
        sleep=unused,
        send_email=lambda _cfg, rows: sent.append(rows),
    ))
    assert held["back_to_battery_capacity"] is None
    assert alarm_status()["active"] is True
    assert sent == []


def test_plan_uses_the_inverter_reading():
    cfg = _cfg()
    overlay_live_capacity(cfg, _mismatch(60))
    assert cfg["inverter"]["back_to_battery_capacity"] == 60
    assert cfg["inverter"]["to_grid_battery_capacity"] == 16


def test_save_skips_capacity_writes_when_the_stored_values_are_unchanged():
    existing = _cfg()
    assert capacity_register_writes(existing, 16, 26) == []


def test_save_writes_only_the_capacity_register_that_changed():
    existing = _cfg()
    assert capacity_register_writes(existing, 16, 30) == [
        ("inverter_1/back_to_battery_capacity", "30"),
    ]


def test_ui_test_alarm_turns_on_and_off_without_email():
    reset_capacity_sync_state()
    on = set_test_alarm(True)
    off = set_test_alarm(False)
    assert on["active"] is True and on["test"] is True
    assert off["active"] is False and off["test"] is False


def test_real_reading_replaces_the_ui_test():
    reset_capacity_sync_state()
    set_test_alarm(True)
    asyncio.run(reconcile_inverter_capacity(
        _cfg(),
        _match(),
        read=lambda: (_ for _ in ()).throw(AssertionError("read")),
        write=lambda items: (_ for _ in ()).throw(AssertionError("write")),
        sleep=lambda: (_ for _ in ()).throw(AssertionError("sleep")),
        send_email=lambda *_args: None,
    ))
    assert alarm_status()["active"] is False
    assert alarm_status()["test"] is False
