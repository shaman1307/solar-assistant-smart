"""Energy arbitrage plan — SQLite plan_latest (no in-memory layer)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src import plan_simulation as ps
from src.sqlite_store import delete_plan, read_plan, read_plan_forecast, write_plan


@pytest.fixture(autouse=True)
def _clear_plan_sqlite():
    delete_plan()
    yield
    delete_plan()


def _warsaw_now(*, hour: int = 14, minute: int = 0) -> datetime:
    return datetime(2026, 7, 3, hour, minute, 0)


def _cfg() -> dict:
    """Minimal config for build_plan_simulation."""
    return {
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {"min_block_minutes": 30},
        "grid": {
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.8,
                "offpeak_energy_only_pln_kwh": 0.4,
                "peak_hours": [[7, 13], [16, 22]],
            },
        },
    }


def _fresh_plan_entry(now: datetime) -> dict:
    return {
        "today_date": now.strftime("%Y-%m-%d"),
        "plan_from_hour": now.hour,
        "computed_at": "2026-07-03 12:00:00",
        "rows": [{"hour": now.hour, "plan_date": now.strftime("%Y-%m-%d"), "start": "x"}],
        "history_rows": [],
        "delta_kwh": 0.0,
        "forecast": {"meta": {}},
        "rce": {"current_price_pln_kwh": 0.42},
        "buy_tariff": {"rows": []},
    }


def test_plan_window_stale_when_hour_differs(monkeypatch):
    now = _warsaw_now(hour=14)
    monkeypatch.setattr(ps, "now_warsaw", lambda: now)
    stored = _fresh_plan_entry(now)
    stored["plan_from_hour"] = 13
    assert ps._plan_window_matches(stored, now) is False


def test_plan_window_fresh_when_matches(monkeypatch):
    now = _warsaw_now(hour=14, minute=45)
    monkeypatch.setattr(ps, "now_warsaw", lambda: now)
    stored = _fresh_plan_entry(now.replace(minute=0, second=0, microsecond=0))
    assert ps._plan_window_matches(stored, now) is True


def test_delete_plan_clears_sqlite():
    write_plan(_fresh_plan_entry(_warsaw_now()))
    delete_plan()
    assert read_plan() is None
    assert read_plan_forecast() is None


def test_read_plan_reads_sqlite():
    entry = _fresh_plan_entry(_warsaw_now())
    write_plan(entry)
    assert read_plan()["computed_at"] == entry["computed_at"]
    assert read_plan_forecast() == entry["forecast"]


async def _immediate_to_thread(fn, /, *args, **kwargs):
    return fn(*args, **kwargs)


def _patch_build_deps(monkeypatch, *, now: datetime, fetch: AsyncMock, sim_result: dict):
    monkeypatch.setattr(ps, "now_warsaw", lambda: now)
    monkeypatch.setattr(ps, "fetch_plan_inputs", fetch)
    monkeypatch.setattr(asyncio, "to_thread", _immediate_to_thread)
    monkeypatch.setattr(ps, "run_simulation", lambda *a, **k: sim_result)
    monkeypatch.setattr(ps, "build_hourly_schedule", lambda *a, **k: {})
    monkeypatch.setattr(ps, "build_buy_tariff_payload", lambda *a, **k: {"rows": []})
    monkeypatch.setattr(ps, "extract_actual_soc_q15", lambda sim, now=None: {"today": [None] * 96, "tomorrow": [None] * 96})
    monkeypatch.setattr(
        ps,
        "compose_plan_soc_q15",
        lambda existing, fresh, **kw: (fresh or {}).get("plan_soc_q15")
        or {"today": [None] * 96, "tomorrow": [None] * 96},
    )


def test_build_plan_sqlite_hit_skips_fetch(monkeypatch):
    now = _warsaw_now(hour=14)
    write_plan(_fresh_plan_entry(now))
    fetch = AsyncMock()
    _patch_build_deps(
        monkeypatch,
        now=now,
        fetch=fetch,
        sim_result={"rows": [], "today_date": now.strftime("%Y-%m-%d"), "plan_from_hour": now.hour},
    )

    result = asyncio.run(ps.build_plan_simulation(_cfg()))

    fetch.assert_not_called()
    assert result["computed_at"] == "2026-07-03 12:00:00"


def test_build_plan_stale_hour_triggers_rebuild(monkeypatch):
    now = _warsaw_now(hour=14)
    stale = _fresh_plan_entry(now)
    stale["plan_from_hour"] = 10
    write_plan(stale)

    sim = {
        "rows": [{"hour": 14, "rebuilt": True, "plan_date": "2026-07-03", "start": "x"}],
        "history_rows": [],
        "delta_kwh": 1.0,
        "today_date": now.strftime("%Y-%m-%d"),
        "plan_from_hour": now.hour,
        "plan_soc_q15": {"today": [None] * 96, "tomorrow": [None] * 96},
    }
    fetch = AsyncMock(
        return_value=({"meta": {}}, {}, {}, {"current_price_pln_kwh": 0.5}),
    )
    _patch_build_deps(monkeypatch, now=now, fetch=fetch, sim_result=sim)

    result = asyncio.run(ps.build_plan_simulation(_cfg()))

    fetch.assert_awaited_once()
    # Same calendar day uses incremental merge — current hour keeps SQLite row;
    # plan window advances to the new hour.
    assert result["plan_from_hour"] == 14
    assert read_plan()["plan_from_hour"] == 14


def test_hourly_refresh_mid_hour_preserves_locked_timer(monkeypatch):
    """hourly_plan_refresh at :28 keeps the full locked Dis window (no mid-hour clip)."""
    now = _warsaw_now(hour=22, minute=28)
    locked_plan = _fresh_plan_entry(now.replace(minute=0, second=0, microsecond=0))
    locked_plan["plan_from_hour"] = 22
    locked_plan["rows"] = [{
        "hour": 22,
        "plan_date": "2026-07-03",
        "start": "03-07-2026 23:00",
        "timer_schedule": "Dis 22:00-22:45 8.0kW cap16%",
        "action": "Discharging to Grid and Load",
        "hour_labels_locked": True,
        "q15": [
            {"quarter": q, "production": 0.0, "consumption": 0.1, "soc": 50.0,
             "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0,
             "from_actual": False}
            for q in range(4)
        ],
    }]
    write_plan(locked_plan)

    sim = {
        "rows": [{
            "hour": 22,
            "plan_date": "2026-07-03",
            "start": "03-07-2026 23:00",
            "timer_schedule": "Dis 22:00-22:30 8.0kW cap16%",
            "action": "Idle",
            "hour_labels_locked": False,
            "q15": [
                {"quarter": q, "production": 9.0, "consumption": 0.1, "soc": 10.0,
                 "battery": 0.0, "grid_import": 0.0, "grid_export": 0.0,
                 "from_actual": False}
                for q in range(4)
            ],
        }],
        "history_rows": [],
        "delta_kwh": 0.0,
        "today_date": "2026-07-03",
        "plan_from_hour": 22,
    }
    fetch = AsyncMock(
        return_value=({"meta": {}}, {}, {}, {"current_price_pln_kwh": 0.5}),
    )
    _patch_build_deps(monkeypatch, now=now, fetch=fetch, sim_result=sim)

    result = asyncio.run(ps.hourly_plan_refresh(_cfg()))

    cur = next(r for r in result["rows"] if r["hour"] == 22)
    # Locked Dis kept verbatim (not Idle / not clipped / not fresh optimizer text).
    assert cur["timer_schedule"] == "Dis 22:00-22:45 8.0kW cap16%"
    assert cur["hour_labels_locked"] is True
    # :28 — current-hour q0 not freeze-ready until :30
    assert cur["q15"][0]["from_actual"] is False

def test_invalidate_all_caches_keeps_sqlite_plan(monkeypatch):
    from src.cache_registry import invalidate_all_caches

    write_plan(_fresh_plan_entry(_warsaw_now()))
    invalidate_all_caches()
    assert read_plan() is not None


def test_invalidate_input_caches_keeps_sqlite_plan(monkeypatch):
    from src.cache_registry import invalidate_input_caches

    write_plan(_fresh_plan_entry(_warsaw_now()))
    invalidate_input_caches()
    assert read_plan() is not None
