"""Current hour with a planned Timer must not be wiped when it becomes current."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from src.plan_q15 import run_day_smart_q15_plan
from src.grid_config import merge_grid_defaults
from src.plan_optimizer import HourControl, plan_battery_grid_charge
from src.simulation import (
    committed_current_hour_row,
    plan_row_end_soc_kwh,
    apply_locked_hour_labels_from_plan,
)
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
    plan_min_soc_kwh,
)


OFF = 0.5
PEAK = 1.2


def _cfg() -> dict:
    cfg = {
        "battery": {
            "capacity_kwh": 48.0,
            "min_soc_pct": 16,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "horizon_hours": 24,
            "epsilon_kwh": 0.01,
            "losses_pct": {
                "grid_to_battery": 0.0,
                "battery_to_load_or_grid": 0.0,
                "pv_to_grid": 0.0,
                "pv_to_load": 0.0,
                "pv_to_battery": 0.0,
            },
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.5,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": PEAK,
                "offpeak_price_pln_kwh": OFF,
                "peak_energy_only_pln_kwh": 0.9,
                "offpeak_energy_only_pln_kwh": 0.4,
                "peak_hours": [[7, 13], [16, 22]],
            },
        },
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 0.5},
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def test_front_load_skip_zero_keeps_charge_in_first_slot():
    """skip_leading_slots=0 must not clear DP charge already in step 0."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    eps = float(params["epsilon_kwh"])
    # Pre-peak: charge in hour 0 and 1; peak at hour 3.
    controls = [
        HourControl(6.0, 0.0, False),
        HourControl(3.0, 0.0, False),
        HourControl(0.0, 0.0, False),
        HourControl(0.0, 0.0, False),
    ]
    pv = [0.0] * 4
    load = [0.2] * 4
    buy = [OFF, OFF, OFF, PEAK]
    reserves = [plan_min_soc_kwh(cfg)] * 4
    kept = plan_battery_grid_charge(
        controls,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        offpeak_buy=OFF,
        charge_targets=[0.0] * 4,
        initial_soc_kwh=plan_min_soc_kwh(cfg) + 1.0,
        battery_cap=48.0,
        min_kwh=plan_min_soc_kwh(cfg),
        charge_ac_step=6.0,
        discharge_dc_step=8.0,
        inverter_ac_step=8.0,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        eps_step=eps,
        reserves=reserves,
        step_scale=1.0,
        skip_leading_slots=0,
    )
    assert kept[0].grid_charge_kw > 0.05

    cleared = plan_battery_grid_charge(
        controls,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        offpeak_buy=OFF,
        charge_targets=[0.0] * 4,
        initial_soc_kwh=plan_min_soc_kwh(cfg) + 1.0,
        battery_cap=48.0,
        min_kwh=plan_min_soc_kwh(cfg),
        charge_ac_step=6.0,
        discharge_dc_step=8.0,
        inverter_ac_step=8.0,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        eps_step=eps,
        reserves=reserves,
        step_scale=1.0,
        skip_leading_slots=1,
    )
    assert cleared[0].grid_charge_kw < 0.05
    assert cleared[1].grid_charge_kw > 0.05



def test_plan_row_end_soc_from_q15():
    row = {
        "soc": 20.0,
        "q15": [
            {"quarter": 0, "soc": 20.0},
            {"quarter": 1, "soc": 22.0},
            {"quarter": 2, "soc": 24.5},
            {"quarter": 3, "soc": 23.6},
        ],
    }
    assert abs(plan_row_end_soc_kwh(row, 48.0) - 0.236 * 48.0) < 1e-6


def test_committed_current_hour_row_requires_timer():
    plan = {
        "rows": [
            {
                "plan_date": "2026-07-21",
                "hour": 1,
                "timer_schedule": "Chg 01:00-01:30 6.0kW cap25%",
                "action": "Charging from Grid",
                "soc": 25.0,
                "q15": [{"quarter": q, "soc": 20.0 + q} for q in range(4)],
            },
        ],
    }
    with patch("src.sqlite_store.read_plan", return_value=plan):
        row = committed_current_hour_row("2026-07-21", 1)
    assert row is not None
    assert "Chg 01:00-01:30" in row["timer_schedule"]

    with patch("src.sqlite_store.read_plan", return_value=plan):
        assert committed_current_hour_row("2026-07-21", 2) is None

    empty = {
        "rows": [{
            "plan_date": "2026-07-21",
            "hour": 1,
            "timer_schedule": "",
            "action": "Discharging to Load",
        }],
    }
    with patch("src.sqlite_store.read_plan", return_value=empty):
        assert committed_current_hour_row("2026-07-21", 1) is None


def test_apply_locked_at_hour_start_keeps_existing_chg():
    """Full-rebuild :00 path must not adopt fresh empty timer over planned Chg."""
    now = datetime(2026, 7, 21, 1, 0)
    existing = {
        "rows": [{
            "plan_date": "2026-07-21",
            "hour": 1,
            "start": "21-07-2026 02:00",
            "timer_schedule": "Chg 01:00-01:30 6.0kW cap25%",
            "action": "Charging from Grid",
            "soc": 25.0,
            "hour_labels_locked": False,
            "q15": [{"quarter": q, "soc": 20.0 + q * 1.5} for q in range(4)],
        }],
    }
    result = {
        "rows": [{
            "plan_date": "2026-07-21",
            "hour": 1,
            "start": "21-07-2026 02:00",
            "timer_schedule": "",
            "action": "Discharging to Load",
            "soc": 19.0,
            "hour_labels_locked": False,
            "q15": [{"quarter": q, "soc": 19.0} for q in range(4)],
        }],
    }
    apply_locked_hour_labels_from_plan(result, existing, now)
    row = result["rows"][0]
    assert row["timer_schedule"] == "Chg 01:00-01:30 6.0kW cap25%"
    assert row["action"] == "Charging from Grid"
    assert row["hour_labels_locked"] is True
    assert float(row["soc"]) == 25.0


def test_apply_locked_at_hour_start_keeps_empty_timer():
    """At :00 an empty SQLite current hour stays empty; fresh Dis does not land."""
    now = datetime(2026, 7, 21, 20, 0)
    existing = {
        "rows": [{
            "plan_date": "2026-07-21",
            "hour": 20,
            "start": "21-07-2026 21:00",
            "timer_schedule": "",
            "action": "Discharging to Load",
            "soc": 45.0,
            "hour_labels_locked": False,
        }],
    }
    result = {
        "rows": [{
            "plan_date": "2026-07-21",
            "hour": 20,
            "start": "21-07-2026 21:00",
            "timer_schedule": "Dis 20:00-20:30 7.5kW cap30%",
            "action": "Discharging to Grid and Load",
            "soc": 40.0,
            "hour_labels_locked": False,
        }],
    }
    apply_locked_hour_labels_from_plan(result, existing, now)
    row = result["rows"][0]
    assert not str(row.get("timer_schedule") or "").strip()
    assert row["hour_labels_locked"] is True


def test_run_day_from_next_hour_with_skip_zero_uses_seed_soc():
    """Replan from hour+1 with committed end SOC — first control hour may charge."""
    cfg = _cfg()
    # Low SOC at end of committed H01 → need more overnight charge from H02.
    end_soc = plan_min_soc_kwh(cfg) + 1.0
    pv = [0.0] * 24
    load = [0.4] * 6 + [1.5] * 3 + [0.4] * 15
    plan = run_day_smart_q15_plan(
        date_str="2026-07-21",
        pv_hourly=pv,
        load_hourly=load,
        tomorrow_pv=[2.0] * 24,
        tomorrow_load=[0.4] * 24,
        cfg=cfg,
        initial_soc_kwh=end_soc,
        from_hour=2,
        front_load_skip_leading_slots=0,
    )
    assert plan is not None
    # Hour 1 must be absent (starts at 2); hour 2 may have charge slots.
    assert not (plan["q15_by_hour"].get(1) or [])
    h2 = plan["q15_by_hour"].get(2) or []
    assert h2, "expected optimizer slots for hour 2"


def test_mid_hour_forward_soc_uses_planned_eoh_not_live_blend():
    """Committed hour end SOC=50% seeds H+1 even when live blend is ~21%."""
    from zoneinfo import ZoneInfo

    from src.simulation import build_energy_arbitrage_plan

    cfg = _cfg()
    cap = float(cfg["battery"]["capacity_kwh"])
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 21, 3, 45, tzinfo=tz)
    today = "2026-07-21"

    committed = {
        "plan_date": today,
        "hour": 3,
        "start": "21-07-2026 04:00",
        "timer_schedule": "Chg 03:00-03:30 6.0kW cap50%",
        "action": "Charging from Grid",
        "soc": 50.0,
        "hour_labels_locked": True,
        "production": 0.0,
        "consumption": 0.5,
        "battery": 1.0,
        "bat_charge": 1.0,
        "bat_discharge": 0.0,
        "grid_import": 1.5,
        "grid_export": 0.0,
        "q15": [
            {"quarter": 0, "soc": 20.0, "production": 0.0, "consumption": 0.1,
             "battery": 0.2, "grid_import": 0.3, "grid_export": 0.0, "from_actual": True},
            {"quarter": 1, "soc": 35.0, "production": 0.0, "consumption": 0.1,
             "battery": 0.3, "grid_import": 0.4, "grid_export": 0.0, "from_actual": True},
            {"quarter": 2, "soc": 48.0, "production": 0.0, "consumption": 0.1,
             "battery": 0.3, "grid_import": 0.4, "grid_export": 0.0, "from_actual": False},
            {"quarter": 3, "soc": 50.0, "production": 0.0, "consumption": 0.2,
             "battery": 0.2, "grid_import": 0.4, "grid_export": 0.0, "from_actual": False},
        ],
    }
    stored = {"today_date": today, "plan_from_hour": 3, "rows": [committed], "history_rows": []}

    pv = [0.0] * 24
    load = [0.5] * 24
    forecast = {
        "today": {
            "pv": pv, "load": load, "pv_forecast": pv, "load_forecast": load,
            "pv_total": 0.0, "load_total": 12.0,
        },
        "tomorrow": {
            "pv": [1.0] * 24, "load": [0.5] * 24, "pv_total": 24.0, "load_total": 12.0,
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 21.0,
        "today_hourly": {
            "pv": [0.0] * 24,
            "load": [0.5] * 24,
            "soc": [20.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
        "series_10min": None,
    }

    # Live blend paints current hour near 21%; forward must still start from 50%.
    low_blend = [
        {"quarter": q, "soc": 21.0, "production": 0.0, "consumption": 0.1,
         "battery": 0.0, "grid_import": 0.1, "grid_export": 0.0}
        for q in range(4)
    ]

    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=stored),
        patch(
            "src.plan_orchestrator.build_blended_current_hour_q15",
            return_value=low_blend,
        ),
        patch("src.simulation.quarter_rce_for_dates", return_value={today: [0.1] * 96}),
    ):
        plan = build_energy_arbitrage_plan(forecast, metrics, {}, cfg)

    by_h = {
        int(r["hour"]): r
        for r in plan["rows"]
        if r.get("start") != "TOTAL" and str(r.get("plan_date")) == today
    }
    assert 3 in by_h and 4 in by_h
    # Current hour may show live blend (~21) for UI.
    assert float(by_h[3]["soc"]) < 30.0
    # Next hour must chain from planned EOH 50%, not live ~21.
    h4 = float(by_h[4]["soc"])
    assert h4 >= 40.0, f"H04 soc={h4} should track from planned EOH 50% (cap={cap})"
    assert abs(plan_row_end_soc_kwh(committed, cap) - 0.5 * cap) < 1e-6


def test_valid_locked_chg_stays_committed_not_replanned():
    """Bat Charge ≥ min_hourly keeps the locked Chg hour; do not reopen/split it."""
    from zoneinfo import ZoneInfo

    from src.simulation import build_energy_arbitrage_plan

    cfg = _cfg()
    cfg["timer_schedule"] = {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0}
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 21, 3, 20, tzinfo=tz)
    today = "2026-07-21"
    committed = {
        "plan_date": today,
        "hour": 3,
        "start": "21-07-2026 04:00",
        "timer_schedule": "Chg 03:00-03:30 6.0kW cap50%",
        "action": "Charging from Grid",
        "soc": 50.0,
        "hour_labels_locked": True,
        "production": 0.0,
        "consumption": 0.5,
        "battery": 2.2,
        "bat_charge": 2.5,
        "bat_discharge": 0.3,
        "grid_import": 2.8,
        "grid_export": 0.0,
        "q15": [
            {"quarter": q, "soc": 40.0 + q * 3, "production": 0.0, "consumption": 0.1,
             "battery": 0.6, "grid_import": 0.7, "grid_export": 0.0,
             "from_actual": q < 2}
            for q in range(4)
        ],
    }
    stored = {"today_date": today, "plan_from_hour": 3, "rows": [committed], "history_rows": []}
    pv = [0.0] * 24
    load = [0.5] * 24
    forecast = {
        "today": {
            "pv": pv, "load": load, "pv_forecast": pv, "load_forecast": load,
            "pv_total": 0.0, "load_total": 12.0,
        },
        "tomorrow": {
            "pv": [1.0] * 24, "load": [0.5] * 24, "pv_total": 24.0, "load_total": 12.0,
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 45.0,
        "today_hourly": {
            "pv": [0.0] * 24, "load": [0.5] * 24, "soc": [40.0] * 24,
            "bat_charge": [0.0] * 24, "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24, "grid_sell": [0.0] * 24,
        },
        "series_10min": None,
    }

    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=stored),
        patch("src.simulation.quarter_rce_for_dates", return_value={today: [0.1] * 96}),
    ):
        plan = build_energy_arbitrage_plan(forecast, metrics, {}, cfg)

    h3 = next(
        r for r in plan["rows"]
        if r.get("start") != "TOTAL" and str(r.get("plan_date")) == today and int(r["hour"]) == 3
    )
    assert "Chg" in str(h3.get("timer_schedule") or "")
    assert h3.get("hour_labels_locked") is True


def test_committed_chg_blend_uses_locked_timer_not_sa_rules():
    """Locked EA Chg must drive blend charge even when SA rules timer is empty."""
    from zoneinfo import ZoneInfo

    from src.simulation import build_energy_arbitrage_plan

    cfg = _cfg()
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 27, 1, 5, tzinfo=tz)
    today = "2026-07-27"
    committed = {
        "plan_date": today,
        "hour": 1,
        "start": "27-07-2026 02:00",
        "timer_schedule": "Chg 01:00-01:30 4.0kW cap25%",
        "action": "Charging from Grid",
        "soc": 25.0,
        "hour_labels_locked": True,
        "production": 0.0,
        "consumption": 0.8,
        "battery": 0.9,
        "bat_charge": 1.5,
        "bat_discharge": 0.6,
        "grid_import": 2.0,
        "grid_export": 0.0,
        "q15": [
            {"quarter": q, "soc": 23.0 + q, "production": 0.0, "consumption": 0.2,
             "battery": 0.4 if q < 2 else -0.2, "grid_import": 0.5 if q < 2 else 0.0,
             "grid_export": 0.0}
            for q in range(4)
        ],
    }
    stored = {"today_date": today, "plan_from_hour": 1, "rows": [committed], "history_rows": []}
    pv = [0.0] * 24
    load = [0.8] * 24
    forecast = {
        "today": {
            "pv": pv, "load": load, "pv_forecast": pv, "load_forecast": load,
            "pv_total": 0.0, "load_total": 19.2,
        },
        "tomorrow": {
            "pv": [1.0] * 24, "load": [0.5] * 24, "pv_total": 24.0, "load_total": 12.0,
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 23.5,
        "today_hourly": {
            "pv": [0.0] * 24,
            "load": [0.8] * 24,
            "soc": [23.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
        "series_10min": None,
        "prev_day_hourly": {
            "pv": [0.0] * 24, "load": [0.8] * 24, "soc": [24.0] * 24,
            "bat_charge": [0.0] * 24, "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24, "grid_sell": [0.0] * 24,
        },
    }

    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=stored),
        patch("src.plan_orchestrator.sa_discharge_timer_for_hour", return_value=""),
        patch("src.simulation.quarter_rce_for_dates", return_value={today: [0.1] * 96}),
    ):
        plan = build_energy_arbitrage_plan(forecast, metrics, {}, cfg)

    h1 = next(
        r for r in plan["rows"]
        if r.get("start") != "TOTAL" and str(r.get("plan_date")) == today and int(r["hour"]) == 1
    )
    assert "Chg" in str(h1.get("timer_schedule") or "")
    assert float(h1.get("bat_charge") or 0) > 0.5, (
        f"expected grid charge from locked timer, got bat_charge={h1.get('bat_charge')} "
        f"battery={h1.get('battery')} gi={h1.get('grid_import')}"
    )
    assert float(h1.get("soc") or 0) > 23.5, (
        f"SOC after committed Chg should rise above start, got {h1.get('soc')}"
    )


def test_idle_current_hour_forward_soc_chains_from_display_blend():
    """Without a committed timer, H+1 must continue from the violet EOH.

    If forward seeded from a lower as-if-00:00 planned EOH, a following Chg
    hour can end at the same SOC as the current row and look like a no-op.
    """
    from zoneinfo import ZoneInfo

    from src.simulation import build_energy_arbitrage_plan

    cfg = _cfg()
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 7, 27, 0, 55, tzinfo=tz)
    today = "2026-07-27"

    # No timer on H00 — not a committed current hour.
    stored = {
        "today_date": today,
        "plan_from_hour": 0,
        "rows": [{
            "plan_date": today,
            "hour": 0,
            "start": "27-07-2026 01:00",
            "timer_schedule": "",
            "action": "Discharging to Load",
            "soc": 22.0,
            "hour_labels_locked": False,
            "production": 0.0,
            "consumption": 0.9,
            "battery": -0.9,
            "bat_charge": 0.0,
            "bat_discharge": 0.9,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "q15": [
                {"quarter": q, "soc": 22.0 - q * 0.2, "production": 0.0,
                 "consumption": 0.2, "battery": -0.2, "grid_import": 0.0,
                 "grid_export": 0.0}
                for q in range(4)
            ],
        }],
        "history_rows": [],
    }

    pv = [0.0] * 24
    load = [0.8] * 24
    forecast = {
        "today": {
            "pv": pv, "load": load, "pv_forecast": pv, "load_forecast": load,
            "pv_total": 0.0, "load_total": 19.2,
        },
        "tomorrow": {
            "pv": [1.0] * 24, "load": [0.5] * 24, "pv_total": 24.0, "load_total": 12.0,
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 24.0,
        "today_hourly": {
            "pv": [0.0] * 24,
            "load": [0.8] * 24,
            "soc": [23.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
        "series_10min": None,
        "prev_day_hourly": {
            "pv": [0.0] * 24,
            "load": [0.8] * 24,
            "soc": [24.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
    }

    # Display EOH for H00 — higher than a typical as-if-00:00 planned EOH.
    display_blend = [
        {"quarter": q, "soc": 23.7, "production": 0.0, "consumption": 0.2,
         "battery": -0.2, "grid_import": 0.0, "grid_export": 0.0}
        for q in range(4)
    ]

    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=stored),
        patch(
            "src.plan_orchestrator.build_blended_current_hour_q15",
            return_value=display_blend,
        ),
        patch("src.simulation.quarter_rce_for_dates", return_value={today: [0.1] * 96}),
    ):
        plan = build_energy_arbitrage_plan(forecast, metrics, {}, cfg)

    by_h = {
        int(r["hour"]): r
        for r in plan["rows"]
        if r.get("start") != "TOTAL" and str(r.get("plan_date")) == today
    }
    assert 0 in by_h and 1 in by_h
    assert abs(float(by_h[0]["soc"]) - 23.7) < 0.05
    # H01 must not sit flat at 23.7 when net battery is clearly positive, and
    # must not restart from a ~1pp-lower planned EOH under the display value.
    h1 = by_h[1]
    h1_soc = float(h1["soc"])
    h1_bat = float(h1.get("battery") or 0)
    if h1_bat > 0.3:
        assert h1_soc > float(by_h[0]["soc"]) + 0.3, (
            f"H01 soc={h1_soc} after bat={h1_bat:+.2f} should rise above H00 "
            f"display {by_h[0]['soc']}"
        )
    else:
        # Even without a big charge, forward start is the display EOH — H01 end
        # stays within ~1pp of H00 after ordinary house load.
        assert abs(h1_soc - 23.7) < 2.5, f"H01 soc={h1_soc} drifted from display EOH"


def test_empty_current_hour_never_exports_at_hour_start():
    """Empty current-hour timer is frozen for export at :00, same as the UI."""
    from zoneinfo import ZoneInfo

    from src.simulation import build_energy_arbitrage_plan

    cfg = _cfg()
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 9, 1, 22, 0, tzinfo=tz)
    today = "2026-09-01"
    tomorrow = "2026-09-02"

    idle = {
        "plan_date": today,
        "hour": 22,
        "start": "01-09-2026 23:00",
        "timer_schedule": "",
        "action": "Discharging to Load",
        "soc": 50.0,
        "hour_labels_locked": False,
        "production": 0.0,
        "consumption": 0.8,
        "battery": -0.8,
        "bat_charge": 0.0,
        "bat_discharge": 0.8,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "q15": [
            {"quarter": q, "soc": 50.0 - q, "production": 0.0,
             "consumption": 0.2, "battery": -0.2, "grid_import": 0.0,
             "grid_export": 0.0}
            for q in range(4)
        ],
    }
    stored = {"today_date": today, "plan_from_hour": 22, "rows": [idle], "history_rows": []}

    pv = [0.0] * 24
    load = [0.5] * 24
    forecast = {
        "today": {
            "pv": pv, "load": load, "pv_forecast": pv, "load_forecast": load,
            "pv_total": 0.0, "load_total": 12.0,
        },
        "tomorrow": {
            "pv": [0.0] * 6 + [0.5] * 12 + [0.0] * 6,
            "load": [0.5] * 24,
            "pv_total": 6.0, "load_total": 12.0,
        },
        "meta": {},
    }
    metrics = {
        "battery_soc": 50.0,
        "today_hourly": {
            "pv": [0.0] * 24,
            "load": [0.5] * 24,
            "soc": [50.0] * 24,
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
        "series_10min": None,
    }

    rce_today = [0.40] * 96
    for q in range(4):
        rce_today[22 * 4 + q] = 1.80
    rce_tom = [0.40] * 96
    for q in range(4):
        rce_tom[6 * 4 + q] = 1.00

    with (
        patch("src.simulation._now_warsaw", return_value=now),
        patch("src.sqlite_store.read_plan", return_value=stored),
        patch(
            "src.simulation.quarter_rce_for_dates",
            return_value={today: rce_today, tomorrow: rce_tom},
        ),
    ):
        plan = build_energy_arbitrage_plan(forecast, metrics, {}, cfg)

    by_key = {
        (str(r.get("plan_date")), int(r["hour"])): r
        for r in plan["rows"]
        if r.get("start") != "TOTAL" and r.get("hour") is not None
    }
    h22 = by_key[(today, 22)]
    assert not str(h22.get("timer_schedule") or "").strip()
    assert not h22.get("export_planned")
    today_export = [
        h for (d, h), r in by_key.items()
        if d == today and (r.get("export_planned") or str(r.get("timer_schedule") or "").lower().startswith("dis"))
    ]
    assert 22 not in today_export
