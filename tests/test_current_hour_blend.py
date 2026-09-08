"""Current-hour blended PV/load + unified sim chain for Energy arbitrage."""

from datetime import datetime

import yaml

from src.plan_hourly_actuals import (
    TEN_MIN_KWH_PER_KW,
    blend_current_hour_end,
    blended_q15_pv_load_slots,
    build_blended_current_hour_q15,
    simulate_q15_slots,
    sync_blended_current_hour_row,
)
from src.simulation_config import merge_simulation_defaults


def _series_through_slots(hour: int, kw: float, n_slots: int) -> dict[str, list[float | None]]:
    """Populate only the first n_slots 10-min buckets of the hour (realistic lag)."""
    series: list[float | None] = [None] * 144
    base = hour * 6
    for i in range(n_slots):
        series[base + i] = kw
    return {"pv": list(series), "load": list(series), "soc": list(series)}


def _q15_hour(hour: int, per_q: float) -> list[float]:
    q15 = [0.0] * 96
    for q in range(4):
        q15[hour * 4 + q] = per_q
    return q15


def _minimal_cfg() -> dict:
    with open("sa-config.yaml.example") as f:
        return merge_simulation_defaults(yaml.safe_load(f))


def test_at_hour_start_uses_full_forecast():
    now = datetime(2026, 6, 26, 18, 0)
    pv_q = _q15_hour(18, 0.25)
    load_q = _q15_hour(18, 0.5)
    pv, load = blend_current_hour_end(
        18,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        forecast_pv_hourly=1.0,
        forecast_load_hourly=2.0,
        series_10min=None,
    )
    assert pv == 1.0
    assert load == 2.0


def test_at_15_open_pull_q0_not_frozen():
    """At :15 pull q0 (fact :00-:10 + forecast/3); freeze only at :30."""
    now = datetime(2026, 6, 26, 18, 15)
    hour = 18
    kw = 6.0
    series = _series_through_slots(hour, kw, n_slots=1)
    per_q = 0.1
    pv_q = _q15_hour(hour, per_q)
    load_q = _q15_hour(hour, 0.2)

    partial = kw * TEN_MIN_KWH_PER_KW  # 10 min
    open_q0 = partial + per_q * (5.0 / 15.0)
    forecast_tail = per_q * 3
    pv, load = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        forecast_pv_hourly=99.0,
        forecast_load_hourly=99.0,
        series_10min=series,
    )
    assert pv == round(open_q0 + forecast_tail, 3)
    assert load == round(
        (kw * TEN_MIN_KWH_PER_KW + 0.2 * (5.0 / 15.0)) + 0.2 * 3, 3,
    )

    slots = blended_q15_pv_load_slots(
        hour, now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=load_q,
        series_10min=series,
        pv_hourly=99.0,
        load_hourly=99.0,
    )
    assert slots[0][0] == round(open_q0, 4)
    assert slots[1][0] == 0.1


def test_at_30_freezes_q0_and_q1():
    """At :30 freeze q0–q1 from Influx through :30 (job runs at :31)."""
    now = datetime(2026, 6, 26, 18, 30)
    hour = 18
    kw = 3.0
    series = _series_through_slots(hour, kw, n_slots=3)
    per_q = 0.15
    pv_q = _q15_hour(hour, per_q)

    frozen_q0 = kw * TEN_MIN_KWH_PER_KW + 0.5 * kw * TEN_MIN_KWH_PER_KW
    frozen_q1 = 0.5 * kw * TEN_MIN_KWH_PER_KW + kw * TEN_MIN_KWH_PER_KW
    forecast_tail = per_q * 2
    pv, _ = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        forecast_pv_hourly=0.0,
        forecast_load_hourly=0.0,
        series_10min=series,
    )
    assert pv == round(frozen_q0 + frozen_q1 + forecast_tail, 3)

    slots = blended_q15_pv_load_slots(
        hour, now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        series_10min=series,
    )
    assert slots[0][0] == round(frozen_q0, 4)
    assert slots[1][0] == round(frozen_q1, 4)
    assert slots[2][0] == 0.15
    assert slots[3][0] == 0.15


def test_at_45_freezes_q0_q1_open_pulls_q2():
    """At :45 freeze q0–q1; open-pull q2 = fact :30-:40 + forecast/3."""
    now = datetime(2026, 6, 26, 18, 45)
    hour = 18
    kw = 2.0
    series = _series_through_slots(hour, kw, n_slots=4)  # through :40
    per_q = 0.2
    pv_q = _q15_hour(hour, per_q)

    from src.plan_hourly_actuals import actual_q15_slice_kwh

    q0 = actual_q15_slice_kwh(series["pv"], hour, 0)
    q1 = actual_q15_slice_kwh(series["pv"], hour, 1)
    open_q2 = kw * TEN_MIN_KWH_PER_KW + per_q / 3.0
    forecast_q3 = per_q
    pv, _ = blend_current_hour_end(
        hour,
        now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        forecast_pv_hourly=0.0,
        forecast_load_hourly=0.0,
        series_10min=series,
    )
    assert pv == round(q0 + q1 + open_q2 + forecast_q3, 3)

    slots = blended_q15_pv_load_slots(
        hour, now,
        forecast_pv_q15=pv_q,
        forecast_load_q15=pv_q,
        series_10min=series,
    )
    assert slots[2][0] == round(open_q2, 4)
    assert slots[3][0] == per_q


def test_at_30_blended_battery_db_on_frozen_open_on_pull():
    """At :30 q0–q1 from_actual; q2–q3 sim."""
    from src.plan_hourly_actuals import simulate_blended_current_hour_q15

    cfg = _minimal_cfg()
    hour = 18
    now = datetime(2026, 6, 26, 18, 30)
    kw = 2.0
    series = _series_through_slots(hour, kw, n_slots=3)
    for key in ("bat_charge", "bat_discharge", "grid_buy", "grid_sell"):
        series[key] = [None] * 144
    base = hour * 6
    series["bat_charge"][base] = 1.0
    series["bat_charge"][base + 1] = 1.0
    series["grid_buy"][base + 1] = -2.0

    pv_by_q = [0.2, 0.2, 0.3, 0.3]
    load_by_q = [0.1, 0.1, 0.1, 0.1]
    opt = [{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4

    q15, _ = simulate_blended_current_hour_q15(
        5.0, hour, now, pv_by_q, load_by_q, opt, series, cfg,
    )

    assert q15[0]["from_actual"] is True
    assert q15[1]["from_actual"] is True
    assert q15[2]["from_actual"] is False
    assert q15[3]["from_actual"] is False
    assert q15[0]["battery"] > 0


def test_sim_chain_soc_accumulates_from_start():
    """SOC at each q15 = previous + sim delta (not Influx / optimizer override)."""
    cfg = _minimal_cfg()
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    soc_start_kwh = 5.0  # 50% of 10 kWh
    pv_by_q = [0.5, 0.5, 0.5, 0.5]
    load_by_q = [0.3, 0.3, 0.3, 0.3]
    opt = [{"grid_charge_kw": 0.0, "ctrl_battery_export_kwh": 0.0}] * 4

    q15, soc_end = simulate_q15_slots(
        soc_start_kwh, 10, pv_by_q, load_by_q, opt, cfg,
    )
    assert len(q15) == 4
    assert q15[0]["soc"] >= 50.0
    assert q15[-1]["soc"] == round((soc_end / battery_cap) * 100.0, 1)
    assert soc_end > soc_start_kwh


def test_build_blended_uses_sim_not_influx_battery():
    """Before :15 current-hour Influx pull has not started — all q15 from forecast/sim."""
    cfg = _minimal_cfg()
    hour = 17
    now = datetime(2026, 6, 28, 17, 10)
    series = {
        "pv": [None] * 144,
        "load": [None] * 144,
        "grid_buy": [None] * 144,
        "grid_sell": [None] * 144,
    }
    base = hour * 6
    series["pv"][base] = 3.0
    series["load"][base] = 0.6

    opt_slots = [
        {
            "quarter": q,
            "grid_charge_kw": 0.0,
            "ctrl_battery_export_kwh": 0.0,
        }
        for q in range(4)
    ]

    q15 = build_blended_current_hour_q15(
        hour,
        now,
        forecast_pv_q15=[0.25] * 96,
        forecast_load_q15=[0.4] * 96,
        series_10min=series,
        soc_start_kwh=7.0,
        opt_slots=opt_slots,
        cfg=cfg,
    )

    assert q15[0]["production"] == 0.25
    assert q15[0]["from_actual"] is False
    assert q15[0]["battery"] != 1.5
    assert q15[0]["soc"] != 0.0


def test_sync_blended_current_hour_row_matches_q15_sums():
    q15 = [
        {"quarter": 0, "production": 0.5, "consumption": 0.3, "soc": 70.0,
         "battery": 0.1, "grid_import": 0.0, "grid_export": 0.0},
        {"quarter": 1, "production": 0.2, "consumption": 0.4, "soc": 71.0,
         "battery": -0.05, "grid_import": 0.1, "grid_export": 0.0},
        {"quarter": 2, "production": 0.15, "consumption": 0.25, "soc": 71.5,
         "battery": 0.02, "grid_import": 0.05, "grid_export": 0.03},
        {"quarter": 3, "production": 0.1, "consumption": 0.2, "soc": 72.0,
         "battery": 0.0, "grid_import": 0.0, "grid_export": 0.02},
    ]
    row = {
        "production": 9.99,
        "consumption": 8.88,
        "battery": 0.4,
        "bat_charge": 0.4,
        "bat_discharge": 0.0,
        "grid_import": 0.2,
        "grid_export": 0.0,
        "soc": 75.0,
        "buy_price": 0.5,
        "rce_price": 0.3,
        "g12_zone": "offpeak",
    }
    cfg = {
        "grid": {
            "g12": {
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.4,
            },
            "feed_in_price_pln": 0.2,
        },
    }

    sync_blended_current_hour_row(
        row,
        q15,
        production=0.95,
        consumption=1.15,
        soc=72.0,
        cfg=cfg,
        epsilon=0.05,
    )

    assert row["production"] == 0.95
    assert row["consumption"] == 1.15
    assert row["soc"] == 72.0
    assert row["soc_blended"] is True
    assert row["bat_charge"] == 0.12
    assert row["bat_discharge"] == 0.05
    assert row["battery"] == 0.07


def test_sync_blended_current_hour_row_sets_export_action():
    q15 = [
        {"quarter": 0, "production": 0.1, "consumption": 0.13, "soc": 64.0,
         "battery": -1.4, "grid_import": 0.0, "grid_export": 1.3},
        {"quarter": 1, "production": 0.09, "consumption": 0.13, "soc": 59.0,
         "battery": -2.1, "grid_import": 0.0, "grid_export": 1.9},
        {"quarter": 2, "production": 0.2, "consumption": 0.14, "soc": 54.0,
         "battery": -1.9, "grid_import": 0.0, "grid_export": 1.8},
        {"quarter": 3, "production": 0.03, "consumption": 0.14, "soc": 54.0,
         "battery": -0.1, "grid_import": 0.0, "grid_export": 0.0},
    ]
    row = {
        "action": "Discharging to Load",
        # Timer Schedule for the current hour is frozen (computed at :00).
        "timer_schedule": "Dis 07:00-08:00 8.0kW cap16%",
        "buy_price": 1.24,
        "rce_price": 0.69,
        "g12_zone": "peak",
    }
    cfg = {
        "grid": {
            "g12": {
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.4,
            },
            "feed_in_price_pln": 0.2,
        },
        "battery": {"capacity_kwh": 20.0},
        "simulation": {"min_soc_pct": 16},
    }
    rules = {
        "timed_discharge_enabled": True,
        "discharge_slots": [{
            "from": "07:00", "to": "08:00", "power_kw": 8.0, "capacity_pct": 16,
        }],
    }
    from datetime import datetime
    from src.timer_plan import ACTION_DISCHARGE_GRID, sa_discharge_timer_for_hour

    sa_timer = sa_discharge_timer_for_hour(rules, 7)
    sync_blended_current_hour_row(
        row,
        q15,
        production=0.42,
        consumption=0.54,
        soc=54.0,
        cfg=cfg,
        epsilon=0.05,
        hour=7,
        opt_slots=[],
        sa_timer_txt=sa_timer,
        now=datetime(2026, 7, 7, 7, 47),
    )

    assert row["action"] == ACTION_DISCHARGE_GRID
    assert row["grid_export"] == 5.0
    assert row["timer_schedule"] == "Dis 07:00-08:00 8.0kW cap16%"
