"""Influx datapatch line protocol, interpolation, and gap timestamps."""

from datetime import datetime, timedelta, timezone

from src.influx_datapatch import (
    escape_measurement,
    interpolate,
    line_protocol,
    parse_utc,
    step_times,
)


def test_escape_measurement_spaces():
    assert escape_measurement("Battery state of charge") == r"Battery\ state\ of\ charge"


def test_line_protocol_integer_soc():
    ts = datetime(2026, 9, 11, 7, 9, 59, tzinfo=timezone.utc)
    line = line_protocol("Battery state of charge", 25.4, ts, integer=True)
    assert line == r"Battery\ state\ of\ charge combined=25i 1789110599"


def test_line_protocol_float_hourly():
    ts = datetime(2026, 9, 11, 7, 0, 0, tzinfo=timezone.utc)
    line = line_protocol("PV power hourly", 3545.0, ts, integer=False)
    assert line.startswith(r"PV\ power\ hourly combined=3545.0 ")


def test_interpolate_midpoint():
    assert interpolate(54.0, 56.0, 0.5) == 55.0


def test_step_times_exclusive_end():
    start = parse_utc("2026-09-11T12:00:00Z")
    end = parse_utc("2026-09-11T12:15:00Z")
    times = step_times(start, end, timedelta(minutes=5))
    assert [t.minute for t in times] == [0, 5, 10]


def test_parse_utc_z_suffix():
    dt = parse_utc("2026-09-11T07:09:38Z")
    assert dt.tzinfo is not None
    assert dt.hour == 7 and dt.minute == 9 and dt.second == 38
