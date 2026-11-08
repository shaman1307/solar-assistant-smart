"""Day-roll archive keeps the day's cached forecast and SOC plan."""

import asyncio
from datetime import datetime

from src import sqlite_store
from src.plan_cost import compute_plan_totals
from src.routes import data as data_routes


def test_archive_stores_cached_forecast_and_plan_soc(tmp_path, monkeypatch):
    monkeypatch.setattr("src.sqlite_store._DB_PATH", tmp_path / "solar_smart.db")
    monkeypatch.setattr("src.sqlite_store._conn", None)
    radiation = {"time": ["2026-11-07T12:00"], "direct": [300.0], "diffuse": [80.0]}
    monkeypatch.setattr("src.forecast_cache.load_day_cache", lambda: {"days": {"2026-11-07": {
        "pv": {"effective_q15": [0.5] * 96, "radiation": radiation},
        "load": {"effective_q15": [0.3] * 96},
    }}})
    plan = {
        "today_date": "2026-11-07",
        "history_rows": [{"plan_date": "2026-11-07", "hour": 10, "start": "07-11-2026 10:00", "production": 1.0}],
        "rows": [],
        "plan_soc_q15": {"today": [40.0] * 96, "tomorrow": [50.0] * 96},
    }
    with sqlite_store._lock:
        conn = sqlite_store._connect()
        sqlite_store._archive_plan_day_locked(conn, plan, "2026-11-07", compute_plan_totals)
        conn.commit()

    archived = sqlite_store.load_plan_day_archive("2026-11-07")
    assert archived["forecast"]["pv_q15"] == [0.5] * 96
    assert archived["forecast"]["load_q15"] == [0.3] * 96
    assert archived["forecast"]["pv_radiation"] == radiation
    assert archived["plan_soc_q15"] == {"today": [40.0] * 96}


def test_archive_without_cached_day_has_no_forecast(tmp_path, monkeypatch):
    monkeypatch.setattr("src.sqlite_store._DB_PATH", tmp_path / "solar_smart.db")
    monkeypatch.setattr("src.sqlite_store._conn", None)
    monkeypatch.setattr("src.forecast_cache.load_day_cache", lambda: {"days": {}})
    plan = {
        "today_date": "2026-11-07",
        "history_rows": [{"plan_date": "2026-11-07", "hour": 10, "start": "07-11-2026 10:00"}],
        "rows": [],
    }
    with sqlite_store._lock:
        conn = sqlite_store._connect()
        sqlite_store._archive_plan_day_locked(conn, plan, "2026-11-07", compute_plan_totals)
        conn.commit()

    archived = sqlite_store.load_plan_day_archive("2026-11-07")
    assert archived["forecast"] is None
    assert archived["plan_soc_q15"] == {"today": []}


def test_past_day_recompute_uses_archived_radiation_without_writes(monkeypatch):
    radiation = {"time": ["2026-11-07T12:00"], "direct": [300.0], "diffuse": [80.0]}
    monkeypatch.setattr(data_routes, "now_warsaw", lambda: datetime(2026, 11, 8, 15, 0))
    monkeypatch.setattr(data_routes, "load_plan_day_archive",
                        lambda day: {"forecast": {"pv_radiation": radiation}})
    monkeypatch.setattr(data_routes, "load_config", lambda: {"solar": {}})
    seen = {}

    def fake_q15(cfg, rad, date_str):
        seen["args"] = (rad, date_str)
        return [0.25] * 96

    def forbid(*_a, **_k):
        raise AssertionError("past-day recompute must not write")

    monkeypatch.setattr(data_routes.forecast_mod, "pv_q15_from_radiation", fake_q15)
    monkeypatch.setattr(sqlite_store, "save_plan_day_archive", forbid)
    monkeypatch.setattr(data_routes.fc, "save_day_cache", forbid)

    out = asyncio.run(data_routes.api_forecast_recompute_pv(date="2026-11-07"))
    assert out["ok"] is True
    assert out["pv_forecast_q15"] == [0.25] * 96
    assert out["pv_total"] == 24.0
    assert seen["args"] == (radiation, "2026-11-07")
