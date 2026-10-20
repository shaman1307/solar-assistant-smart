"""RCE quarters stay in the store. PSE is called only for a day that is not stored yet."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import src.rce as rce

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-10-05"
TOMORROW = "2026-10-06"


def _day_records(date: str, mwh: float) -> list[dict]:
    start = datetime.strptime(date, "%Y-%m-%d")
    return [
        {
            "dtime": (start + timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M"),
            "rce_pln": mwh,
        }
        for i in range(1, 97)
    ]


def _install(monkeypatch, now: datetime, records_for):
    blob = {"raw": None}
    calls: list[tuple[str, str]] = []

    def read_meta(key):
        return blob["raw"] if key == rce._STORE_KEY else None

    def write_meta(key, value):
        if key == rce._STORE_KEY:
            blob["raw"] = value

    def fetch(date_from, date_to):
        calls.append((date_from, date_to))
        return records_for(date_from, date_to)

    monkeypatch.setattr(rce, "read_meta", read_meta)
    monkeypatch.setattr(rce, "write_meta", write_meta)
    monkeypatch.setattr(rce, "_fetch_rce", fetch)
    monkeypatch.setattr(rce, "now_warsaw", lambda: now)
    monkeypatch.setattr(rce, "_quarters", None)
    return blob, calls


def test_stored_day_is_not_fetched_again(monkeypatch):
    blob, calls = _install(
        monkeypatch,
        datetime(2026, 10, 5, 10, 0, tzinfo=TZ),
        lambda a, b: _day_records(TODAY, 1000.0) if a <= TODAY <= b else [],
    )
    first = rce.quarter_rce_for_dates(TODAY, TOMORROW)
    assert first[TODAY][0] == rce.rce_pln_kwh_brutto_from_mwh(1000.0)
    assert calls == [(TODAY, TODAY)]
    monkeypatch.setattr(rce, "_quarters", None)
    second = rce.quarter_rce_for_dates(TODAY, TOMORROW)
    assert second[TODAY] == first[TODAY]
    assert calls == [(TODAY, TODAY)]
    stored = json.loads(blob["raw"])
    assert TODAY in stored
    assert TOMORROW not in stored


def test_before_14_tomorrow_is_not_requested(monkeypatch):
    _blob, calls = _install(
        monkeypatch,
        datetime(2026, 10, 5, 13, 45, tzinfo=TZ),
        lambda a, b: _day_records(TODAY, 800.0),
    )
    rce.quarter_rce_for_dates(TODAY)
    calls.clear()
    monkeypatch.setattr(rce, "_quarters", None)
    out = rce.quarter_rce_for_dates(TODAY, TOMORROW)
    assert calls == []
    assert out[TOMORROW] == [None] * 96
    assert out[TODAY][0] is not None


def test_from_14_a_failed_fetch_keeps_stored_prices(monkeypatch):
    blob, calls = _install(
        monkeypatch,
        datetime(2026, 10, 5, 14, 0, tzinfo=TZ),
        lambda a, b: _day_records(TODAY, 900.0),
    )
    rce.quarter_rce_for_dates(TODAY)
    saved = blob["raw"]
    assert TODAY in json.loads(saved)

    def fail(_a, _b):
        calls.append(("fail", "fail"))
        return []

    monkeypatch.setattr(rce, "_fetch_rce", fail)
    monkeypatch.setattr(
        rce, "now_warsaw", lambda: datetime(2026, 10, 5, 18, 15, tzinfo=TZ),
    )
    out = rce.quarter_rce_for_dates(TODAY, TOMORROW)
    assert out[TODAY][0] == rce.rce_pln_kwh_brutto_from_mwh(900.0)
    assert out[TOMORROW] == [None] * 96
    assert json.loads(blob["raw"])[TODAY] == json.loads(saved)[TODAY]
    assert calls[-1] == ("fail", "fail")
