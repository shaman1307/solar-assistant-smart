"""Quarter reading of capacity settings versus the saved configuration."""

from src.routes.config import capacity_register_writes
from src.inverter_setting_alarm import (
    capacity_setting_mismatches,
    record_capacity_reading,
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


def test_email_only_when_the_alarm_starts():
    sent: list[int] = []

    def _send(_cfg, rows):
        sent.append(len(rows))

    match = {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 26}
    record_capacity_reading(_cfg(), match, send_email=_send)
    rules = {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 100}
    first = record_capacity_reading(_cfg(), rules, send_email=_send)
    second = record_capacity_reading(_cfg(), rules, send_email=_send)
    cleared = record_capacity_reading(
        _cfg(),
        {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 26},
        send_email=_send,
    )
    again = record_capacity_reading(_cfg(), rules, send_email=_send)
    assert first["active"] is True
    assert second["active"] is True
    assert cleared["active"] is False
    assert again["active"] is True
    assert sent == [1, 1]


def test_save_skips_capacity_writes_when_the_stored_values_are_unchanged():
    existing = _cfg()
    assert capacity_register_writes(existing, 16, 26) == []


def test_save_writes_only_the_capacity_register_that_changed():
    existing = _cfg()
    assert capacity_register_writes(existing, 16, 30) == [
        ("inverter_1/back_to_battery_capacity", "30"),
    ]


def test_ui_test_alarm_turns_on_and_off_without_email():
    record_capacity_reading(
        _cfg(),
        {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 26},
        send_email=lambda *_args: None,
    )
    on = set_test_alarm(True)
    off = set_test_alarm(False)
    assert on["active"] is True and on["test"] is True
    assert off["active"] is False and off["test"] is False


def test_real_reading_replaces_the_ui_test():
    set_test_alarm(True)
    status = record_capacity_reading(
        _cfg(),
        {"to_grid_battery_capacity": 16, "back_to_battery_capacity": 26},
        send_email=lambda *_args: None,
    )
    assert status["active"] is False
    assert status["test"] is False
