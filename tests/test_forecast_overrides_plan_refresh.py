"""Forecast Overrides must rebuild Energy arbitrage (plan + solid SOC unlock)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from src.plan_simulation import get_simulation_for_date
from src.routes import config as config_routes
from src.routes import data as data_routes


def test_api_save_overrides_rebuilds_energy_arbitrage_with_soc_unlock():
    """POST /api/overrides must refresh EA with unlock_plan_soc=True.

    Without unlock, solid SOC stays frozen and the Energy arbitrage table can
    keep a stale plan after Forecast Overrides change.
    """
    cfg: dict = {
        "battery": {"capacity_kwh": 48},
        "overrides": {
            "today_pv_kwh": None,
            "today_load_kwh": None,
            "tomorrow_pv_kwh": None,
            "tomorrow_load_kwh": None,
        },
    }
    refresh = AsyncMock(return_value={"rows": [], "computed_at": "x"})
    cache = AsyncMock(return_value={})

    with (
        patch.object(config_routes, "load_config", return_value=cfg),
        patch.object(config_routes, "save_config") as save_cfg,
        patch.object(config_routes.forecast_mod, "apply_overrides_to_cache", cache),
        patch.object(config_routes, "hourly_plan_refresh", refresh),
    ):
        result = asyncio.run(
            config_routes.api_save_overrides({
                "today_pv_kwh": 42.5,
                "today_load_kwh": 18.0,
                "tomorrow_pv_kwh": None,
                "tomorrow_load_kwh": 12.0,
            })
        )

    assert result == {"status": "saved"}
    assert cfg["overrides"]["today_pv_kwh"] == 42.5
    assert cfg["overrides"]["today_load_kwh"] == 18.0
    assert cfg["overrides"]["tomorrow_pv_kwh"] is None
    assert cfg["overrides"]["tomorrow_load_kwh"] == 12.0
    save_cfg.assert_called_once_with(cfg)
    cache.assert_awaited_once_with(cfg)
    refresh.assert_awaited_once()
    assert refresh.await_args.args[0] is cfg
    assert refresh.await_args.kwargs.get("unlock_plan_soc") is True


def test_api_save_overrides_updates_load_cache_before_ea_rebuild():
    """New Load must hit the forecast cache before Energy arbitrage rebuilds."""
    cfg: dict = {
        "battery": {"capacity_kwh": 48},
        "overrides": {
            "today_pv_kwh": None,
            "today_load_kwh": None,
            "tomorrow_pv_kwh": None,
            "tomorrow_load_kwh": None,
        },
    }
    order: list[str] = []

    async def _cache(_cfg):
        order.append("cache")
        return {}

    async def _refresh(_cfg, unlock_plan_soc=False):
        order.append(f"ea:{unlock_plan_soc}")
        return {"rows": []}

    with (
        patch.object(config_routes, "load_config", return_value=cfg),
        patch.object(config_routes, "save_config"),
        patch.object(config_routes.forecast_mod, "apply_overrides_to_cache", _cache),
        patch.object(config_routes, "hourly_plan_refresh", _refresh),
    ):
        asyncio.run(
            config_routes.api_save_overrides({
                "today_pv_kwh": None,
                "today_load_kwh": 30.0,
                "tomorrow_pv_kwh": None,
                "tomorrow_load_kwh": None,
            })
        )

    assert order == ["cache", "ea:True"]
    assert cfg["overrides"]["today_load_kwh"] == 30.0


def test_ea_future_hour_consumption_follows_forecast_load():
    """Energy arbitrage future hours must use the (override/EV) forecast load."""
    from datetime import datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from src.grid_config import merge_grid_defaults
    from src.simulation import build_energy_arbitrage_plan
    from src.simulation_config import merge_battery_defaults, merge_simulation_defaults

    today = "2026-07-27"
    now = datetime(2026, 7, 27, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    # Flat 0.5 kWh/h house load, but H16 bumped to 5.5 (EV-like override).
    load = [0.5] * 24
    load[16] = 5.5
    pv = [0.0] * 24
    pv[12] = 2.0
    forecast = {
        "today": {
            "pv": list(pv),
            "load": list(load),
            "pv_forecast": list(pv),
            "load_forecast": list(load),
            "pv_q15": [v / 4 for v in pv for _ in range(4)],
            "load_q15": [v / 4 for v in load for _ in range(4)],
            "pv_total": sum(pv),
            "load_total": sum(load),
        },
        "tomorrow": {
            "pv": [1.0] * 24,
            "load": [0.5] * 24,
            "pv_total": 24.0,
            "load_total": 12.0,
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 40.0,
        "today_hourly": {
            "pv": [0.0] * 12 + [None] * 12,
            "load": [0.5] * 12 + [None] * 12,
            "soc": [40.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
        "series_10min": None,
    }
    cfg = {
        "battery": {"capacity_kwh": 48.0},
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {"epsilon_kwh": 0.05, "horizon_hours": 36},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
        "grid": {
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.24,
                "offpeak_price_pln_kwh": 0.62,
                "peak_energy_only_pln_kwh": 0.9,
                "offpeak_energy_only_pln_kwh": 0.4,
                "peak_hours": [[7, 13], [16, 22]],
            },
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)

    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=None),
        patch("src.plan_orchestrator.sa_discharge_timer_for_hour", return_value=""),
        patch("src.simulation.quarter_rce_for_dates", return_value={today: [0.1] * 96}),
    ):
        plan = build_energy_arbitrage_plan(forecast, metrics, {}, cfg)

    h16 = next(
        r for r in plan["rows"]
        if r.get("start") != "TOTAL"
        and str(r.get("plan_date")) == today
        and int(r["hour"]) == 16
    )
    assert abs(float(h16["consumption"]) - 5.5) < 0.15, (
        f"EA H16 must follow new load plan, got consumption={h16.get('consumption')}"
    )


def test_api_simulation_unlock_plan_soc_forwards_to_hourly_refresh():
    """Forecast Overrides Refresh uses ?refresh=1&unlock_plan_soc=1."""
    cfg = {"battery": {"capacity_kwh": 48}}
    refresh = AsyncMock(return_value={"rows": [{"hour": 15}], "computed_at": "y"})

    with (
        patch.object(data_routes, "load_config", return_value=cfg),
        patch(
            "src.plan_simulation.hourly_plan_refresh",
            refresh,
        ),
        patch(
            "src.plan_simulation.now_warsaw",
            return_value=__import__("datetime").datetime(
                2026, 7, 27, 15, 20,
                tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Warsaw"),
            ),
        ),
    ):
        out = asyncio.run(
            data_routes.api_simulation(refresh=True, unlock_plan_soc=True, date=None)
        )

    assert out["computed_at"] == "y"
    refresh.assert_awaited_once()
    assert refresh.await_args.kwargs.get("unlock_plan_soc") is True


def test_get_simulation_plain_refresh_keeps_soc_locked_flag_false():
    """Quarter Refresh (?refresh=1 alone) must not unlock solid SOC by default."""
    cfg = {"battery": {"capacity_kwh": 48}}
    refresh = AsyncMock(return_value={"ok": True})

    with (
        patch("src.plan_simulation.hourly_plan_refresh", refresh),
        patch(
            "src.plan_simulation.now_warsaw",
            return_value=__import__("datetime").datetime(
                2026, 7, 27, 15, 20,
                tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Warsaw"),
            ),
        ),
    ):
        asyncio.run(get_simulation_for_date(cfg, refresh=True, unlock_plan_soc=False))

    refresh.assert_awaited_once()
    assert refresh.await_args.kwargs.get("unlock_plan_soc") is False
