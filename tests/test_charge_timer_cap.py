"""Chg timer cap%: end of charge window + mathematical half-up rounding."""

from __future__ import annotations

from src.timer_plan import (
    ACTION_CHARGE_GRID,
    _charge_timer_cap_pct,
    _round_pct_half_up,
    build_hour_timer_schedule,
    parse_timer_schedule_segments,
)


def _cfg(**sim_extra) -> dict:
    cfg = {
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16, **sim_extra},
        "inverter": {"ac_capacity_kw": 8.0},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 0.5},
    }
    return cfg


def _q(
    soc: float,
    *,
    battery_delta: float = 0.0,
    grid_import: float = 0.0,
    action: str = ACTION_CHARGE_GRID,
    reserve_kwh: float | None = None,
) -> dict:
    slot = {
        "action": action,
        "battery_delta": battery_delta,
        "grid_import": grid_import,
        "grid_export": 0.0,
        "pv": 0.0,
        "load": 0.1925,
        "soc_pct": soc,
    }
    if reserve_kwh is not None:
        slot["reserve_kwh"] = reserve_kwh
    return slot


def _cap_from_timer(txt: str) -> int:
    segs = parse_timer_schedule_segments(txt)
    assert segs and segs[0]["kind"] == "chg", txt
    return int(segs[0]["capacity_pct"])


# --- rounding ---


def test_round_pct_half_up_math_not_bankers():
    assert _round_pct_half_up(23.4) == 23
    assert _round_pct_half_up(23.5) == 24
    assert _round_pct_half_up(23.6) == 24
    # Python round(24.5) == 24 (bankers); math half-up → 25
    assert round(24.5) == 24
    assert _round_pct_half_up(24.5) == 25
    assert _round_pct_half_up(32.0) == 32


# --- _charge_timer_cap_pct unit ---


def test_cap_pct_uses_last_charge_quarter_not_hour_end():
    """Live H01: charge to 24.5% by 01:30, then drain to 23.6% — cap is 25%."""
    slots = [
        _q(21.8, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.36),
        _q(24.5, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.0),
        _q(24.1, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.5),
        _q(23.6, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.0),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 25


def test_cap_pct_ignores_falling_overnight_reserve():
    """High early-night reserve must not inflate / invert the stop %."""
    # Reserve ≈ 32% of 48kWh, but charge window ends at 25.0%
    slots = [
        _q(19.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.36),
        _q(22.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.0),
        _q(24.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.5),
        _q(25.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.0),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 25


def test_cap_pct_math_rounds_charge_end_soc():
    slots = [
        _q(20.0, battery_delta=1.0, grid_import=1.2),
        _q(22.4, battery_delta=1.0, grid_import=1.2),  # 22.4 → 22
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 22
    slots[-1]["soc_pct"] = 22.5
    assert _charge_timer_cap_pct(slots, _cfg()) == 23


def test_cap_pct_floored_at_min_soc():
    slots = [
        _q(10.0, battery_delta=0.5, grid_import=0.6),
        _q(12.2, battery_delta=0.5, grid_import=0.6),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 16


def test_cap_pct_empty_slots_returns_min_soc():
    assert _charge_timer_cap_pct([], _cfg()) == 16


def test_cap_pct_fallback_last_slot_when_no_charge_energy():
    """Action says Charging from Grid but deltas are zero — still use last chg SOC."""
    slots = [
        _q(18.0, action=ACTION_CHARGE_GRID),
        _q(21.6, action=ACTION_CHARGE_GRID),
    ]
    # End 21.6→22; start 18 + 5 headroom → 23.
    assert _charge_timer_cap_pct(slots, _cfg()) == 23


def test_cap_pct_at_least_five_above_window_start():
    """SRNE does not start timed charge when stop % is only ~1 point above live SOC."""
    slots = [
        _q(17.4, battery_delta=0.33, grid_import=0.4),
        _q(18.1, battery_delta=0.33, grid_import=0.4),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 21
    assert _charge_timer_cap_pct(slots, _cfg(), live_soc_pct=17.0) == 22


# --- build_hour_timer_schedule integration ---


def test_timer_schedule_cap_matches_charge_window_physics():
    """Regression: cap24% from hour-end SOC would under-charge vs plan (→ ~23.1%)."""
    slots = [
        _q(21.8, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.36),
        _q(24.5, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.0),
        _q(24.1, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.5),
        _q(23.6, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.0),
    ]
    txt = build_hour_timer_schedule(
        1, slots, _cfg(), action=ACTION_CHARGE_GRID, bat_charge=2.585,
    )
    assert "Chg 01:00-01:30" in txt, txt
    assert _cap_from_timer(txt) == 25
    # Hour-end SOC 23.6 must NOT drive the cap
    assert _cap_from_timer(txt) != 24


def test_consecutive_hours_caps_track_each_hour_charge_end():
    """Multi-hour Chg: each hour has its own cap from that hour's charge end."""
    cfg = _cfg()
    h1_slots = [
        _q(19.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.36),
        _q(22.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.0),
        _q(24.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.5),
        _q(25.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.0),
    ]
    h2_slots = [
        _q(27.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.0),
        _q(29.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=13.8),
        _q(31.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=13.5),
        _q(32.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=13.2),
    ]
    # Falling reserve would have yielded ~32% then ~29% — must not happen
    assert _charge_timer_cap_pct(h1_slots, cfg) == 25
    assert _charge_timer_cap_pct(h2_slots, cfg) == 32
    assert _cap_from_timer(
        build_hour_timer_schedule(1, h1_slots, cfg, action=ACTION_CHARGE_GRID, bat_charge=4.8)
    ) == 25
    assert _cap_from_timer(
        build_hour_timer_schedule(2, h2_slots, cfg, action=ACTION_CHARGE_GRID, bat_charge=3.6)
    ) == 32


def test_full_hour_charge_cap_is_end_soc():
    slots = [
        _q(20.1, battery_delta=1.0, grid_import=1.3),
        _q(22.3, battery_delta=1.0, grid_import=1.3),
        _q(24.7, battery_delta=1.0, grid_import=1.3),
        _q(27.2, battery_delta=1.0, grid_import=1.3),
    ]
    txt = build_hour_timer_schedule(
        3, slots, _cfg(), action=ACTION_CHARGE_GRID, bat_charge=4.0,
    )
    assert "Chg 03:00-04:00" in txt, txt
    assert _cap_from_timer(txt) == 27  # 27.2 → 27
