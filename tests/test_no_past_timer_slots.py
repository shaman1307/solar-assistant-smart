"""No retroactive timer slots; reserve blocks free evening export."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.simulation import apply_locked_hour_labels_from_plan
from src.timer_plan import (
    ACTION_DISCHARGE_GRID,
    build_hour_timer_schedule,
    clip_timer_schedule_not_before,
)


def _cfg(**timer_kw) -> dict:
    return {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 2.0,
            **timer_kw,
        },
        "grid": {"g12": {"offpeak_price_pln_kwh": 0.5}},
    }


def _dis_slots(exports: list[float]) -> list[dict]:
    """Four q15 slots; exports are battery→grid kWh per quarter."""
    out = []
    for qi, exp in enumerate(exports):
        out.append({
            "quarter": qi,
            "pv": 0.0,
            "load": 0.2,
            "battery_delta": -float(exp) - 0.2 if exp > 0 else -0.2,
            "grid_import": 0.0,
            "grid_export": float(exp),
            "battery_export_kwh": float(exp),
            "soc_pct": 50.0,
            "action": ACTION_DISCHARGE_GRID if exp > 0 else "Discharging to Load",
        })
    return out


def test_clip_timer_drops_fully_past_segment():
    cfg = _cfg()
    txt = "Dis 22:00-22:30 8.0kW cap16%"
    # 22:30 → earliest 22:30; segment ends at 22:30 → dropped
    assert clip_timer_schedule_not_before(txt, 22 * 60 + 30, cfg=cfg) == ""
    # 22:15 → truncate to 22:15-22:30 (remainder may be < min_block)
    assert clip_timer_schedule_not_before(txt, 22 * 60 + 15, cfg=cfg) == (
        "Dis 22:15-22:30 8kW cap16%"
    )


def test_build_hour_timer_no_past_extend_after_half_hour():
    """After 22:30, q0–q1 export must not recreate Dis 22:00–22:30."""
    cfg = _cfg()
    slots = _dis_slots([1.0, 1.0, 0.0, 0.0])
    # Without floor: natural 22:00-22:30
    full = build_hour_timer_schedule(
        22, slots, cfg, action=ACTION_DISCHARGE_GRID, grid_export=2.0,
    )
    assert "22:00-22:30" in full
    # Mid-hour floor at 22:30: past quarters ignored → empty (can't make 30 min forward)
    mid = build_hour_timer_schedule(
        22, slots, cfg, action=ACTION_DISCHARGE_GRID, grid_export=2.0,
        not_before_min=22 * 60 + 30,
    )
    assert mid == ""


def test_build_hour_timer_future_quarters_ok():
    cfg = _cfg()
    slots = _dis_slots([0.0, 0.0, 1.0, 1.0])
    txt = build_hour_timer_schedule(
        22, slots, cfg, action=ACTION_DISCHARGE_GRID, grid_export=2.0,
        not_before_min=22 * 60 + 30,
    )
    assert "22:30-23:00" in txt


def test_apply_locked_mid_hour_keeps_full_past_dis():
    """Started Dis window stays in SQLite after mid-hour rebuild (no clip erase)."""
    cfg = _cfg()
    now = datetime(2026, 7, 18, 22, 35, tzinfo=ZoneInfo("Europe/Warsaw"))
    existing = {
        "today_date": "2026-07-18",
        "rows": [{
            "plan_date": "2026-07-18",
            "hour": 22,
            "start": "18-07-2026 23:00",
            "timer_schedule": "Dis 22:00-22:30 8.0kW cap16%",
            "action": ACTION_DISCHARGE_GRID,
            "hour_labels_locked": True,
            "bat_charge": 0.0,
            "bat_discharge": 1.4,
            "grid_import": 0.0,
            "grid_export": 0.005,
            "production": 0.0,
        }],
    }
    fresh = {
        "today_date": "2026-07-18",
        "rows": [{
            "plan_date": "2026-07-18",
            "hour": 22,
            "start": "18-07-2026 23:00",
            "timer_schedule": "",
            "action": "Idle",
            "bat_charge": 0.0,
            "bat_discharge": 1.4,
            "grid_import": 0.0,
            "grid_export": 0.005,
            "production": 0.0,
        }],
    }
    apply_locked_hour_labels_from_plan(fresh, existing, now, cfg=cfg)
    row = fresh["rows"][0]
    assert row["timer_schedule"] == "Dis 22:00-22:30 8.0kW cap16%"
    assert row["action"] == "Idle"
    assert row["hour_labels_locked"] is True
