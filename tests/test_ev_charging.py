"""EV charging session normalization and persistence."""

import pytest

from src.ev_charging import (
    build_ev_q15,
    default_session,
    separate_edited_slot,
    get_session_for_ui,
    normalize_session,
    set_session,
)


def _cfg() -> dict:
    return {"ev": {"max_power_kw": 11.0}}


def test_normalize_extends_end_when_before_start():
    session = normalize_session({
        "day": {"enabled": True, "start": "17:00", "end": "16:00", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, _cfg())
    assert session["day"]["start"] == "17:00"
    assert session["day"]["end"] == "17:15"
    assert session["day"]["enabled"] is True


def test_normalize_keeps_valid_range():
    session = normalize_session({
        "day": {"enabled": True, "start": "17:00", "end": "23:45", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, _cfg())
    assert session["day"]["end"] == "23:45"
    assert session["day"]["enabled"] is True


def test_default_session_has_three_slots_at_5kw():
    defaults = default_session()
    assert list(defaults) == ["night", "extra", "day"]
    assert [(s["start"], s["end"], s["power_kw"]) for s in defaults.values()] == [
        ("00:00", "04:00", 5.0),
        ("13:00", "15:00", 5.0),
        ("22:00", "00:00", 5.0),
    ]
    assert not any(s["enabled"] for s in defaults.values())


def test_end_at_midnight_is_end_of_day():
    session = normalize_session({
        "day": {"enabled": True, "start": "22:00", "end": "00:00", "power_kw": 5},
        "night": {"enabled": True, "start": "00:00", "end": "04:00", "power_kw": 5},
    }, _cfg())
    assert session["day"] == {"enabled": True, "start": "22:00", "end": "00:00", "power_kw": 5.0}
    assert session["night"]["enabled"] is True
    q15 = build_ev_q15(session)
    assert abs(sum(q15[88:96]) - 10.0) < 1e-6  # 22:00-24:00 at 5 kW
    assert abs(sum(q15[0:16]) - 20.0) < 1e-6  # 00:00-04:00 at 5 kW
    assert sum(q15[16:88]) == 0.0


def test_start_equal_end_at_midnight_is_extended():
    session = normalize_session({
        "night": {"enabled": True, "start": "00:00", "end": "00:00", "power_kw": 5},
    }, _cfg())
    assert session["night"]["end"] == "00:15"


def _session(**slots):
    base = default_session()
    for key, slot in slots.items():
        base[key].update(slot)
    return base


def test_overlapping_start_is_pushed_to_the_next_quarter():
    session = _session(
        night={"start": "00:00", "end": "04:00"},
        extra={"start": "03:30", "end": "04:30"},
    )
    separate_edited_slot(session, "extra", "start")
    assert session["extra"]["start"] == "04:15"
    assert session["extra"]["end"] == "04:30"
    assert session["night"]["end"] == "04:00"


def test_extending_end_stops_one_step_before_the_next_slot():
    session = _session(
        night={"start": "00:00", "end": "14:00"},
        extra={"start": "13:00", "end": "15:00"},
    )
    separate_edited_slot(session, "night", "end")
    assert session["night"]["start"] == "00:00"
    assert session["night"]["end"] == "12:45"
    assert session["extra"]["start"] == "13:00"


def test_midnight_end_does_not_overlap_midnight_start():
    session = _session(
        night={"start": "00:00", "end": "04:00"},
        day={"start": "22:00", "end": "00:00"},
    )
    separate_edited_slot(session, "day", "end")
    assert session["day"]["start"] == "22:00"
    assert session["day"]["end"] == "00:00"
    assert session["night"]["start"] == "00:00"


def test_third_slot_adds_load():
    session = normalize_session({
        "extra": {"enabled": True, "start": "13:00", "end": "15:00", "power_kw": 5},
    }, _cfg())
    assert session["extra"]["enabled"] is True
    assert abs(sum(build_ev_q15(session)) - 10.0) < 1e-6


def test_set_session_persists_disabled_slot_times(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr("src.ev_charging.now_warsaw", lambda: __import__("datetime").datetime(2026, 6, 30, 12, 0))
    cfg = _cfg()
    date_str = "2026-06-30"
    set_session(date_str, {
        "day": {"enabled": False, "start": "17:00", "end": "23:45", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, cfg)
    ui = get_session_for_ui(date_str, cfg)
    assert ui["day"]["start"] == "17:00"
    assert ui["day"]["end"] == "23:45"
    assert ui["day"]["enabled"] is False


def test_set_session_persists_enabled_day_slot(tmp_path, monkeypatch):
    """Enabled Day must stick — regression for UI checkbox not saving."""
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr(
        "src.ev_charging.now_warsaw",
        lambda: __import__("datetime").datetime(2026, 7, 27, 15, 0),
    )
    cfg = _cfg()
    date_str = "2026-07-27"
    set_session(date_str, {
        "day": {"enabled": True, "start": "14:15", "end": "16:00", "power_kw": 5},
        "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
    }, cfg)
    ui = get_session_for_ui(date_str, cfg)
    assert ui["day"]["enabled"] is True
    assert ui["day"]["start"] == "14:15"
    assert ui["day"]["end"] == "16:00"
    assert ui["day"]["power_kw"] == 5.0


def test_concurrent_set_session_last_write_wins(tmp_path, monkeypatch):
    """Serialized RMW: parallel enables must not leave a corrupt/empty store."""
    import threading

    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr(
        "src.ev_charging.now_warsaw",
        lambda: __import__("datetime").datetime(2026, 7, 27, 15, 0),
    )
    cfg = _cfg()
    date_str = "2026-07-27"
    errors: list[BaseException] = []

    def _write(enabled: bool, start: str) -> None:
        try:
            set_session(date_str, {
                "day": {"enabled": enabled, "start": start, "end": "16:00", "power_kw": 5},
                "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
            }, cfg)
        except BaseException as exc:  # noqa: BLE001 — collect worker errors
            errors.append(exc)

    threads = [
        threading.Thread(target=_write, args=(True, "14:15")),
        threading.Thread(target=_write, args=(True, "14:30")),
        threading.Thread(target=_write, args=(False, "14:00")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    ui = get_session_for_ui(date_str, cfg)
    assert ui["day"]["start"] in ("14:15", "14:30", "14:00")
    assert ui["day"]["end"] == "16:00"


def test_get_session_for_ui_returns_defaults_when_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr("src.ev_charging.now_warsaw", lambda: __import__("datetime").datetime(2026, 6, 30, 12, 0))
    defaults = default_session()
    ui = get_session_for_ui("2026-06-30", _cfg())
    assert ui["day"]["start"] == defaults["day"]["start"]


def test_tomorrow_session_becomes_today_at_midnight_and_survives(tmp_path, monkeypatch):
    """Tomorrow plan rolls to today once at 00:00; the 23:59 job leaves the store alone."""
    from datetime import datetime

    from src import forecast_cache

    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    clock = {"now": datetime(2026, 11, 26, 20, 0)}
    monkeypatch.setattr("src.ev_charging.now_warsaw", lambda: clock["now"])
    cfg = _cfg()
    night = {"enabled": True, "start": "00:00", "end": "04:00", "power_kw": 6}
    off = {"enabled": False, "start": "11:00", "end": "16:00", "power_kw": 5}
    set_session("2026-11-27", {"day": off, "night": night}, cfg)

    # 23:59 job: store must stay untouched.
    clock["now"] = datetime(2026, 11, 26, 23, 59)
    monkeypatch.setattr(forecast_cache, "compute_pv_profiles_for_date", lambda *a, **k: ([0.0] * 24, [0.0] * 96, None))
    monkeypatch.setattr(forecast_cache, "build_day_forecast", lambda *a, **k: {})
    monkeypatch.setattr(forecast_cache, "save_day_cache", lambda *a, **k: None)
    forecast_cache.build_nightly_cache(cfg, now=clock["now"])
    assert get_session_for_ui("2026-11-27", cfg)["night"]["enabled"] is True

    # 00:00: tomorrow plan is today's plan, nothing archived under the old date.
    clock["now"] = datetime(2026, 11, 27, 0, 0)
    ui = get_session_for_ui("2026-11-27", cfg)
    assert ui["night"]["enabled"] is True
    assert ui["night"]["start"] == "00:00"
    assert ui["night"]["end"] == "04:00"
    assert ui["night"]["power_kw"] == 6.0
    assert get_session_for_ui("2026-11-26", cfg)["night"]["enabled"] is False


def test_api_save_returns_enabled_session_without_plan_refresh(tmp_path, monkeypatch):
    """POST saves EV + forecast cache only; client triggers plan rebuild separately."""
    import asyncio

    from src.routes import ev as ev_routes

    db_path = tmp_path / "solar_smart.db"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr(
        "src.ev_charging.now_warsaw",
        lambda: __import__("datetime").datetime(2026, 7, 27, 15, 0),
    )
    monkeypatch.setattr(ev_routes, "load_config", _cfg)

    async def _cache(_cfg):
        return {}

    monkeypatch.setattr(ev_routes.forecast_mod, "refresh_effective_forecast", _cache)

    result = asyncio.run(
        ev_routes.api_save_ev_charging({
            "date": "2026-07-27",
            "day": {"enabled": True, "start": "14:15", "end": "16:00", "power_kw": 5},
            "night": {"enabled": False, "start": "02:00", "end": "07:00", "power_kw": 8},
        })
    )
    assert result["status"] == "saved"
    assert result["session"]["day"]["enabled"] is True
    assert result["session"]["day"]["start"] == "14:15"
