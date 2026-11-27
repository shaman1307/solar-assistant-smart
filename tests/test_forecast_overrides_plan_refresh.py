"""Power Management Refresh and EV load must rebuild Energy arbitrage (plan + solid SOC unlock)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from src.plan_simulation import get_simulation_for_date
from src.routes import data as data_routes


def test_ea_future_hour_consumption_follows_forecast_load():
    """Energy arbitrage future hours must use the (EV-inclusive) forecast load."""
    from datetime import datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from src.grid_config import merge_grid_defaults
    from src.simulation import build_energy_arbitrage_plan
    from src.simulation_config import merge_battery_defaults, merge_simulation_defaults

    today = "2026-07-27"
    now = datetime(2026, 7, 27, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    # Flat 0.5 kWh/h house load, but H16 bumped to 5.5 (EV-like load).
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
    """Power Management Refresh uses ?refresh=1&unlock_plan_soc=1."""
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
