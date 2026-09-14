"""Debug smart column uses the same EA plan rows as Rules."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.debug_plan import (
    ea_row_to_smart_view,
    ea_rows_by_hour_for_date,
    merge_ea_plan_into_debug_day,
)
from src.simulation import (
    apply_locked_hour_labels_from_plan,
    build_energy_arbitrage_plan,
    run_simulation,
)

WARSAW = ZoneInfo("Europe/Warsaw")


def _cfg() -> dict:
    return {
        "battery": {
            "capacity_kwh": 10.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 15,
            "epsilon_kwh": 0.05,
            "horizon_hours": 30,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.3,
            },
            "feed_in_price_pln": 0.4,
        },
    }


def _forecast() -> dict:
    pv = [0.0] * 24
    load = [1.0] * 24
    pv[12] = 4.0
    return {
        "today": {
            "pv": pv,
            "load": load,
            "pv_forecast": pv,
            "load_forecast": load,
            "pv_total": sum(pv),
            "load_total": sum(load),
        },
        "tomorrow": {
            "pv": [0.5] * 24,
            "load": [1.2] * 24,
            "pv_total": 12.0,
            "load_total": 28.8,
        },
        "meta": {},
    }


def _metrics() -> dict:
    return {
        "battery_soc": 55.0,
        "today_hourly": {
            "pv": [0.0] * 24,
            "load": [1.0] * 24,
            "soc": [55.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
    }


def test_run_simulation_delegates_to_builder():
    cfg = _cfg()
    forecast = _forecast()
    metrics = _metrics()
    rules: dict = {}
    direct = build_energy_arbitrage_plan(forecast, metrics, rules, cfg)
    wrapped = run_simulation(forecast, metrics, rules, cfg)
    assert direct.keys() == wrapped.keys()
    assert len(direct["rows"]) == len(wrapped["rows"])


def test_merge_ea_plan_attaches_smart_action_timer():
    today = "2026-07-16"
    ea_plan = {
        "today_date": today,
        "plan_from_hour": 2,
        "history_rows": [
            {
                "hour": 0,
                "plan_date": today,
                "grid_import": 0.5,
                "grid_export": 0.0,
                "bat_charge": 0.1,
                "bat_discharge": 0.0,
                "soc": 54.0,
                "action": "Charge",
                "timer_schedule": "00:00-00:15 Chg",
                "buy_price": 0.5,
                "rce_q15": [0.4, 0.4, 0.4, 0.4],
            },
        ],
        "rows": [
            {
                "hour": 10,
                "plan_date": today,
                "grid_import": 0.0,
                "grid_export": 1.2,
                "bat_charge": 0.0,
                "bat_discharge": 0.8,
                "soc": 62.0,
                "action": "Export",
                "timer_schedule": "10:00-10:30 Dis",
                "buy_price": 0.6,
                "rce_q15": [0.7, 0.7, 0.7, 0.7],
            },
            {
                "hour": 3,
                "plan_date": "2026-07-17",
                "grid_import": 0.2,
                "grid_export": 0.0,
                "bat_charge": 0.0,
                "bat_discharge": 0.0,
                "soc": 60.0,
                "action": "Hold",
                "timer_schedule": "",
            },
        ],
    }
    day = {
        "date": today,
        "rows": [
            {"hour": 0, "pv": 0.0, "load": 1.0, "actual": {}, "sim": {}},
            {"hour": 10, "pv": 4.0, "load": 1.0, "actual": {}, "sim": {}},
            {"hour": 11, "pv": 0.0, "load": 1.0, "actual": {}, "sim": {}},
        ],
    }
    merge_ea_plan_into_debug_day(day, ea_plan, today)

    h0 = day["rows"][0]
    assert h0["action"] == "Charge"
    assert h0["timer_schedule"] == "00:00-00:15 Chg"
    assert h0["smart"]["grid_used"] == 0.5
    assert h0["smart"]["soc"] == 54.0
    assert "cost" not in h0["smart"]

    h10 = day["rows"][1]
    assert h10["action"] == "Export"
    assert h10["smart"]["grid_export"] == 1.2

    h11 = day["rows"][2]
    assert "smart" not in h11 or h11.get("smart") is None


def test_ea_rows_by_hour_includes_history_and_plan():
    today = "2026-07-16"
    ea_plan = {
        "today_date": today,
        "history_rows": [{"hour": 1, "plan_date": today, "action": "A"}],
        "rows": [
            {"hour": 5, "plan_date": today, "action": "B"},
            {"hour": 2, "plan_date": "2026-07-17", "action": "C"},
        ],
    }
    by_hour = ea_rows_by_hour_for_date(ea_plan, today)
    assert by_hour[1]["action"] == "A"
    assert by_hour[5]["action"] == "B"
    assert 2 not in by_hour

    tomorrow = ea_rows_by_hour_for_date(ea_plan, "2026-07-17")
    assert tomorrow[2]["action"] == "C"


def test_apply_locked_hour_labels_restores_mid_hour():
    today = "2026-07-16"
    hour = 10
    existing = {
        "rows": [
            {
                "plan_date": today,
                "hour": hour,
                "hour_labels_locked": True,
                "timer_schedule": "10:00-10:45 Dis",
                "action": "Discharge",
            },
        ],
    }
    fresh = {
        "rows": [
            {
                "plan_date": today,
                "hour": hour,
                "timer_schedule": "10:00-10:15 Chg",
                "action": "Charge",
            },
        ],
    }
    now = datetime(2026, 7, 16, 10, 30, tzinfo=WARSAW)
    apply_locked_hour_labels_from_plan(fresh, existing, now)
    row = fresh["rows"][0]
    assert row["timer_schedule"] == "10:00-10:45 Dis"
    assert row["action"] == "Charge"
    assert row["hour_labels_locked"] is True


def test_attach_immutable_history_keeps_sqlite_past():
    """Full rebuild must keep SQLite history_rows; discard fresh Influx rebuild."""
    from src.plan_cache_merge import attach_immutable_history

    today = "2026-07-18"
    existing = {
        "today_date": today,
        "history_rows": [
            {
                "plan_date": today,
                "hour": 18,
                "timer_schedule": "Dis 18:15-18:45 7.5kW cap16%",
                "action": "Discharging to Grid and Load",
                "hour_labels_locked": True,
                "grid_export": 3.0,
            },
            {
                "plan_date": today,
                "hour": 19,
                "timer_schedule": "Chg 19:00-19:45 5.0kW cap37%",
                "action": "Charging from Grid",
                "hour_labels_locked": True,
                "grid_import": 2.5,
            },
        ],
        "rows": [],
    }
    fresh = {
        "today_date": today,
        "plan_from_hour": 20,
        "history_rows": [
            {
                "plan_date": today,
                "hour": 18,
                "timer_schedule": "",
                "action": "Discharging to Load",
                "grid_export": 9.99,
            },
            {
                "plan_date": today,
                "hour": 19,
                "timer_schedule": "",
                "action": "Idle - Grid Usage for Load",
                "grid_import": 9.99,
            },
        ],
        "rows": [
            {"plan_date": today, "hour": 20, "timer_schedule": "Dis 20:00-20:30 8kW cap16%", "action": "X"},
            {"plan_date": today, "hour": 18, "timer_schedule": "SHOULD_DROP", "action": "past"},
        ],
    }
    now = datetime(2026, 7, 18, 20, 10, tzinfo=WARSAW)
    attach_immutable_history(fresh, existing, now=now)
    assert len(fresh["history_rows"]) == 2
    h18 = fresh["history_rows"][0]
    h19 = fresh["history_rows"][1]
    assert h18["timer_schedule"] == "Dis 18:15-18:45 7.5kW cap16%"
    assert h18["action"] == "Discharging to Grid and Load"
    assert h18["grid_export"] == 3.0
    assert h19["timer_schedule"] == "Chg 19:00-19:45 5.0kW cap37%"
    assert [r["hour"] for r in fresh["rows"]] == [20]
    assert fresh["rows"][0]["timer_schedule"] == "Dis 20:00-20:30 8kW cap16%"


def test_attach_immutable_history_promotes_past_rows():
    """Hours still in existing.rows but now before current hour → history append."""
    from src.plan_cache_merge import attach_immutable_history

    today = "2026-07-18"
    existing = {
        "today_date": today,
        "history_rows": [
            {
                "plan_date": today,
                "hour": 17,
                "timer_schedule": "Dis 17:00-17:30 8kW cap16%",
                "action": "Discharging to Grid",
            },
        ],
        "rows": [
            {
                "plan_date": today,
                "hour": 18,
                "timer_schedule": "Dis 18:00-18:45 8kW cap16%",
                "action": "Discharging to Grid",
                "hour_labels_locked": True,
            },
            {
                "plan_date": today,
                "hour": 19,
                "timer_schedule": "Chg 19:00-20:00 5kW cap40%",
                "action": "Charging from Grid",
            },
        ],
    }
    fresh = {
        "today_date": today,
        "history_rows": [{"plan_date": today, "hour": 17, "timer_schedule": "", "action": "X"}],
        "rows": [
            {"plan_date": today, "hour": 19, "timer_schedule": "fresh19", "action": "Y"},
            {"plan_date": "2026-07-19", "hour": 0, "timer_schedule": "tomorrow", "action": "Z"},
        ],
    }
    now = datetime(2026, 7, 18, 19, 5, tzinfo=WARSAW)
    attach_immutable_history(fresh, existing, now=now)
    hours = [(r["hour"], r["timer_schedule"]) for r in fresh["history_rows"]]
    assert hours == [
        (17, "Dis 17:00-17:30 8kW cap16%"),
        (18, "Dis 18:00-18:45 8kW cap16%"),
    ]
    assert [(r["plan_date"], r["hour"]) for r in fresh["rows"]] == [
        (today, 19),
        ("2026-07-19", 0),
    ]
    assert fresh["rows"][0]["timer_schedule"] == "fresh19"


def test_attach_immutable_history_seeds_on_empty_sqlite():
    """First run / new day keeps fresh history seed (no inventing timers)."""
    from src.plan_cache_merge import attach_immutable_history

    today = "2026-07-18"
    fresh = {
        "today_date": today,
        "history_rows": [
            {
                "plan_date": today,
                "hour": 10,
                "timer_schedule": "",
                "action": "Idle",
                "grid_import": 0.5,
            },
        ],
        "rows": [{"plan_date": today, "hour": 14, "timer_schedule": "live", "action": "A"}],
    }
    now = datetime(2026, 7, 18, 14, 0, tzinfo=WARSAW)
    attach_immutable_history(fresh, None, now=now)
    assert len(fresh["history_rows"]) == 1
    assert fresh["history_rows"][0]["timer_schedule"] == ""
    assert fresh["history_rows"][0]["grid_import"] == 0.5


def test_ea_row_to_smart_view_maps_grid_import():
    view = ea_row_to_smart_view({
        "grid_import": 1.5,
        "grid_export": 0.2,
        "bat_charge": 0.3,
        "bat_discharge": 0.4,
        "soc": 71.5,
        "cost": 9.99,
    })
    assert view["grid_used"] == 1.5
    assert view["soc"] == 71.5
    assert "cost" not in view
