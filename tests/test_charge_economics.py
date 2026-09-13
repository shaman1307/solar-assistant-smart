"""Offpeak Chg economics (price) and min_hourly_transfer threshold."""

from __future__ import annotations

import pytest

from src.plan_optimizer import (
    HourControl,
    enforce_min_hourly_battery_grid_limits,
    plan_battery_grid_charge,
    offpeak_min_block_charge_is_worth,
)


OFF = 0.62
PEAK = 1.24
ETA = 0.925
MIN_HOURLY = 2.0
EPS = 0.01


def test_economics_rejects_thin_need_forced_to_min_block():
    """Need 0.3 kWh → forced 2 kWh block costs ~1.24 zł vs ~0.28 zł peak buy."""
    assert not offpeak_min_block_charge_is_worth(
        need_ac_kwh=0.3,
        min_hourly_kwh=MIN_HOURLY,
        offpeak_buy=OFF,
        peak_buy=PEAK,
        eta_grid=ETA,
        eta_out=ETA,
        epsilon=EPS,
    )


def test_economics_accepts_need_at_or_above_min_block():
    """Full min block (and larger) stays cheaper than buying the same energy at peak."""
    assert offpeak_min_block_charge_is_worth(
        need_ac_kwh=MIN_HOURLY,
        min_hourly_kwh=MIN_HOURLY,
        offpeak_buy=OFF,
        peak_buy=PEAK,
        eta_grid=ETA,
        eta_out=ETA,
        epsilon=EPS,
    )
    assert offpeak_min_block_charge_is_worth(
        need_ac_kwh=4.0,
        min_hourly_kwh=MIN_HOURLY,
        offpeak_buy=OFF,
        peak_buy=PEAK,
        eta_grid=ETA,
        eta_out=ETA,
        epsilon=EPS,
    )


def test_economics_rejects_when_offpeak_near_peak():
    """If offpeak ≈ peak, round-trip losses make grid→battery not worth it."""
    assert not offpeak_min_block_charge_is_worth(
        need_ac_kwh=3.0,
        min_hourly_kwh=MIN_HOURLY,
        offpeak_buy=1.10,
        peak_buy=1.20,
        eta_grid=ETA,
        eta_out=ETA,
        epsilon=EPS,
    )


def test_economics_zero_need_is_never_worth():
    assert not offpeak_min_block_charge_is_worth(
        need_ac_kwh=0.0,
        min_hourly_kwh=MIN_HOURLY,
        offpeak_buy=OFF,
        peak_buy=PEAK,
        eta_grid=ETA,
        eta_out=ETA,
        epsilon=EPS,
    )


def test_threshold_clears_sub_min_hourly_charge():
    """0 < charge_h < min_hourly → wipe (do not inflate to the floor)."""
    controls = [
        HourControl(0.5, 0.0),
        HourControl(0.5, 0.0),
        HourControl(0.0, 0.0),
        HourControl(0.0, 0.0),
    ]
    out = enforce_min_hourly_battery_grid_limits(
        controls,
        rce_step_offset=0,
        step_scale=0.25,
        min_hourly_kwh=MIN_HOURLY,
        epsilon=EPS,
    )
    assert sum(c.grid_charge_kw for c in out) == 0.0


def test_threshold_keeps_charge_at_or_above_min_hourly():
    controls = [
        HourControl(1.0, 0.0),
        HourControl(1.0, 0.0),
        HourControl(0.0, 0.0),
        HourControl(0.0, 0.0),
    ]
    out = enforce_min_hourly_battery_grid_limits(
        controls,
        rce_step_offset=0,
        step_scale=0.25,
        min_hourly_kwh=MIN_HOURLY,
        epsilon=EPS,
    )
    assert sum(c.grid_charge_kw for c in out) == pytest.approx(2.0)


def _front_load(
    budget_slots: list[tuple[int, float]],
    *,
    n_steps: int = 12,
    skip: int = 4,
    min_hourly: float = MIN_HOURLY,
    buy: list[float] | None = None,
) -> list[HourControl]:
    min_kwh = 7.68
    controls = [HourControl(0.0, 0.0, False) for _ in range(n_steps)]
    for i, ac in budget_slots:
        controls[i] = HourControl(ac, 0.0, False)
    prices = buy or ([OFF] * 8 + [PEAK] * (n_steps - 8))
    return plan_battery_grid_charge(
        controls,
        pv_series=[0.0] * n_steps,
        load_series=[0.05] * n_steps,
        buy_prices=prices,
        offpeak_buy=OFF,
        charge_targets=[0.0] * n_steps,
        initial_soc_kwh=min_kwh + 2.0,
        battery_cap=48.0,
        min_kwh=min_kwh,
        charge_ac_step=1.5,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=ETA,
        eta_out=ETA,
        eta_pv_load=ETA,
        eta_pv_grid=ETA,
        eta_pv_battery=ETA,
        eps_step=EPS,
        reserves=[min_kwh] * n_steps,
        step_scale=0.25,
        skip_leading_slots=skip,
        min_block_minutes=30,
        min_hourly_kwh=min_hourly,
    )


def test_front_load_drops_uneconomic_thin_budget():
    """DP budget 0.8 kWh (< 2) is not worth a min block → no Chg slots at all."""
    out = _front_load([(4, 0.5), (5, 0.3)])
    assert sum(c.grid_charge_kw for c in out) == 0.0


def test_front_load_keeps_economic_budget_dense_in_one_hour():
    """4 kWh packs into one clock hour (H01 = steps 4..7), not split 30+30 across hours."""
    # Seed DP budget in late offpeak slots; pack dense in the last hour before peak.
    out = _front_load([(6, 2.0), (7, 2.0)])
    hour1 = [out[i].grid_charge_kw for i in range(4, 8)]
    hour2 = [out[i].grid_charge_kw for i in range(8, 12)]
    assert sum(hour1) == pytest.approx(4.0)
    assert all(x == 0.0 for x in hour2)
    # Dense: first slots at max step, no idle gap inside the fill.
    assert hour1[0] == pytest.approx(1.5)
    assert hour1[1] == pytest.approx(1.5)
    assert sum(1 for x in hour1 if x > EPS) >= 3


def test_front_load_never_starts_chg_in_skipped_current_hour():
    """skip_leading_slots keeps the in-progress hour free of relocated Chg."""
    out = _front_load([(0, 2.0), (1, 2.0)], skip=4)
    assert all(out[i].grid_charge_kw == 0.0 for i in range(4))
    assert sum(out[i].grid_charge_kw for i in range(4, 8)) == pytest.approx(4.0)
