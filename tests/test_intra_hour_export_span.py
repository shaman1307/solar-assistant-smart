"""Intra-hour Dis span: pick richest legal quarters, keep multi-hour roles."""

from __future__ import annotations

from src.plan_optimizer import (
    HourControl,
    plan_hour_battery_grid_export_claim,
    export_span_candidates,
    export_window_roles,
    plan_battery_grid_export,
)


def _claim_kwargs(**extra):
    base = dict(
        battery_cap=48.0,
        min_kwh=8.64,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.95,
        eta_out=0.95,
        eta_pv_load=0.95,
        eta_pv_grid=0.95,
        eta_pv_battery=0.95,
        eps_step=0.01,
        min_hourly_kwh=2.0,
        hold_soc_kwh=0.0,
        hour_end_floor_kwh=8.64,
        reserve_q=[8.64, 8.64, 8.64, 8.64],
        base_charge_q=[0.0, 0.0, 0.0, 0.0],
        load_q=[0.16, 0.16, 0.16, 0.16],
    )
    base.update(extra)
    return base


def test_declining_rce_thin_soc_does_not_charge_then_dump_late():
    """Thin SOC must not yield Dis 09:30-10:00 via PV charge banking."""
    rce_q = [0.78, 0.75, 0.63, 0.54]
    claim = plan_hour_battery_grid_export_claim(
        hour=9,
        role="single",
        soc0=10.5,
        pv_q=[0.86, 0.86, 0.86, 0.86],
        rce_q=rce_q,
        **_claim_kwargs(),
    )
    if claim is not None:
        assert claim.span[0] <= 1, f"late-only Dis after PV bank: {claim.span}"
        assert claim.span != (2, 4)


def test_declining_rce_single_prefers_first_half_not_late_trim():
    """Like tomorrow H09: high RCE early; do not Dis only 09:30-10:00."""
    rce_q = [0.78, 0.75, 0.63, 0.54]
    # PV surplus early — old trim path parked Dis on the cheap tail.
    pv_q = [0.86, 0.86, 0.86, 0.86]
    claim = plan_hour_battery_grid_export_claim(
        hour=9,
        role="single",
        soc0=12.0,
        pv_q=pv_q,
        rce_q=rce_q,
        **_claim_kwargs(),
    )
    assert claim is not None
    assert claim.span[0] <= 1, f"expected early Dis start, got {claim.span}"
    assert claim.span != (2, 4)


def test_rising_rce_single_shortens_from_the_end():
    """A short single-hour window starts at :00 and ends early."""
    rce_q = [1.27, 1.50, 1.77, 2.37]
    claim = plan_hour_battery_grid_export_claim(
        hour=19,
        role="single",
        soc0=11.5,  # thin above min — partial hour only
        pv_q=[0.0, 0.0, 0.0, 0.0],
        rce_q=rce_q,
        **_claim_kwargs(min_hourly_kwh=2.0),
    )
    assert claim is not None
    assert claim.span[0] == 0
    assert claim.span in ((0, 4), (0, 3), (0, 2))


def test_last_role_still_must_start_at_hour_start():
    """Multi-hour continuity: last hour of a run cannot start mid-hour."""
    assert export_span_candidates("last") == [(0, 4), (0, 3), (0, 2)]
    rce_q = [1.27, 1.50, 1.77, 2.37]
    claim = plan_hour_battery_grid_export_claim(
        hour=21,
        role="last",
        soc0=11.5,
        pv_q=[0.0, 0.0, 0.0, 0.0],
        rce_q=rce_q,
        **_claim_kwargs(),
    )
    assert claim is not None
    assert claim.span[0] == 0, f"last role must start :00, got {claim.span}"


def test_middle_role_still_full_hour_only():
    assert export_span_candidates("middle") == [(0, 4)]
    roles = export_window_roles({19, 20, 21})
    assert roles[20] == "middle"
    rce_q = [1.7, 1.8, 1.6, 1.5]
    claim = plan_hour_battery_grid_export_claim(
        hour=20,
        role="middle",
        soc0=30.0,
        pv_q=[0.0, 0.0, 0.0, 0.0],
        rce_q=rce_q,
        **_claim_kwargs(min_hourly_kwh=0.5, reserve_q=[8.64] * 4),
    )
    assert claim is not None
    assert claim.span == (0, 4)


def test_plan_declining_evening_hour_not_late_only_dis():
    """End-to-end: single evening hour with declining RCE."""
    hour = 19
    offset = hour * 4
    steps = 4
    rce = [None] * offset + [0.78, 0.75, 0.63, 0.54]
    base = [HourControl(0.0, 0.0, False) for _ in range(steps)]
    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.16] * steps,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=12.0,
        battery_cap=48.0,
        min_kwh=8.64,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.95,
        eta_out=0.95,
        eta_pv_load=0.95,
        eta_pv_grid=0.95,
        eta_pv_battery=0.95,
        eps_step=0.01,
        reserves=[8.64] * steps,
        export_floor=0.5,
        min_hourly_kwh=2.0,
    )
    early = controls[0].battery_export_kwh + controls[1].battery_export_kwh
    late = controls[2].battery_export_kwh + controls[3].battery_export_kwh
    assert early + late > 1.0
    # Must not be late-only Dis while early quarters idle.
    assert early > 0.5 or controls[0].battery_export_kwh > 0.05
    assert not (early <= 0.05 and late > 1.0)


def _hour18_export(soc: float, prev_end_min: int | None) -> list[int]:
    """Quarters of hour 18 with battery export, horizon starting at 18."""
    hour = 18
    offset = hour * 4
    steps = 4
    rce = [None] * offset + [0.9676, 1.0735, 1.1788, 1.3937]
    base = [HourControl(0.0, 0.0, False) for _ in range(steps)]
    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.16] * steps,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=soc,
        battery_cap=48.0,
        min_kwh=8.64,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.95,
        eta_out=0.95,
        eta_pv_load=0.95,
        eta_pv_grid=0.95,
        eta_pv_battery=0.95,
        eps_step=0.01,
        reserves=[8.64] * steps,
        export_floor=0.5,
        min_hourly_kwh=2.0,
        prev_export_end_min=prev_end_min,
    )
    return [i for i, c in enumerate(controls) if c.battery_export_kwh > 0.05]


def _gap_ok(prev_end_min: int, hour: int, quarter: int) -> bool:
    gap = hour * 60 + quarter * 15 - prev_end_min
    return gap == 0 or gap >= 30


def test_no_15_min_pause_after_committed_export():
    """Dis ended at 18:00. The next hour starts at 18:00, not 18:15."""
    lone = _hour18_export(14.0, None)
    assert lone and lone[0] == 0
    anchored = _hour18_export(14.0, 18 * 60)
    assert anchored and anchored[0] == 0
    assert _gap_ok(18 * 60, 18, anchored[0])


def test_thin_soc_still_starts_at_hour_start():
    """A short hour starts at :00 and ends early."""
    quarters = _hour18_export(11.5, 18 * 60)
    assert quarters
    assert quarters[0] == 0
    assert _gap_ok(18 * 60, 18, quarters[0])


def test_end_at_45_does_not_resume_on_the_hour():
    """Dis ended at 17:45. The next hour does not open 15 minutes later."""
    quarters = _hour18_export(16.0, 17 * 60 + 45)
    assert quarters == []
