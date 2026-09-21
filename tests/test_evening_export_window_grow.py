"""Evening export window grows from the RCE peak into both edges."""

from __future__ import annotations

from src.plan_optimizer import HourControl, plan_battery_grid_export


def _export_plan(
    *,
    rce_by_hour: dict[int, float],
    reserves_by_hour: dict[int, float],
    initial_soc_kwh: float,
    start_hour: int,
    n_hours: int,
    min_kwh: float = 8.0,
    min_hourly_kwh: float = 2.0,
    export_floor: float = 0.62,
    eta: float = 0.925,
    discharge_dc_step: float = 2.0,
    load_per_step: float = 0.2,
) -> tuple[dict[int, float], dict[int, str]]:
    slots = 4
    offset = start_hour * slots
    hours = list(range(start_hour, start_hour + n_hours))
    steps = n_hours * slots
    rce: list[float | None] = [None] * (offset + steps)
    for h, price in rce_by_hour.items():
        for q in range(slots):
            rce[h * slots + q] = float(price)
    reserves: list[float] = []
    for h in hours:
        reserves.extend([float(reserves_by_hour[h])] * slots)
    controls = plan_battery_grid_export(
        [HourControl(0.0, 0.0) for _ in range(steps)],
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[load_per_step] * steps,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=initial_soc_kwh,
        battery_cap=48.0,
        min_kwh=min_kwh,
        discharge_dc_step=discharge_dc_step,
        inverter_ac_step=discharge_dc_step,
        eta_grid=eta,
        eta_out=eta,
        eta_pv_load=eta,
        eta_pv_grid=eta,
        eta_pv_battery=eta,
        eps_step=0.05,
        reserves=reserves,
        export_floor=export_floor,
        min_hourly_kwh=min_hourly_kwh,
    )

    export_kwh: dict[int, float] = {}
    qmask: dict[int, str] = {}
    for h in hours:
        idxs = [i for i in range(steps) if (offset + i) // slots == h]
        export_kwh[h] = sum(controls[i].battery_export_kwh for i in idxs)
        qmask[h] = "".join(
            "1" if controls[i].battery_export_kwh > 0.05 else "0" for i in idxs
        )
    return export_kwh, qmask


def test_leftover_after_peak_opens_right_edge_h21():
    """28.08 shape: H21 ≥ RCE floor; leftover above survive-after-H21 must Dis H21."""
    export, _ = _export_plan(
        rce_by_hour={19: 1.05, 20: 1.10, 21: 0.88, 22: 0.55, 23: 0.40},
        reserves_by_hour={19: 20.0, 20: 18.0, 21: 16.0, 22: 10.0, 23: 9.0},
        initial_soc_kwh=32.0,
        start_hour=19,
        n_hours=5,
    )
    assert export[20] >= 2.0
    assert export[21] >= 2.0, f"H21 must open on leftover, got {export}"
    assert export[22] < 0.5


def test_h21_in_window_before_weaker_h22():
    """29.08: H21 ≥ floor and richer than H22; do not leave H21 empty for an H22 island."""
    export, _ = _export_plan(
        rce_by_hour={19: 1.14, 20: 1.19, 21: 0.99, 22: 0.91, 23: 0.40},
        reserves_by_hour={19: 20.0, 20: 18.0, 21: 16.0, 22: 14.0, 23: 12.0},
        initial_soc_kwh=32.0,
        start_hour=19,
        n_hours=5,
    )
    assert export[20] >= 2.0
    assert export[21] >= 2.0, f"H21 must be in the window, got {export}"
    if export[22] >= 2.0:
        assert export[21] >= 2.0


def test_skip_below_threshold_may_take_hour_beyond():
    """H21 below RCE floor: H22 ≥ floor may still get Dis (skip the cheap hole)."""
    export, _ = _export_plan(
        rce_by_hour={19: 1.05, 20: 1.10, 21: 0.50, 22: 0.90, 23: 0.40},
        reserves_by_hour={19: 22.0, 20: 20.0, 21: 18.0, 22: 16.0, 23: 14.0},
        initial_soc_kwh=32.0,
        start_hour=19,
        n_hours=5,
        min_hourly_kwh=0.5,
    )
    assert export[20] >= 2.0
    assert export[21] < 0.5
    assert export[22] >= 1.5


def test_soc_closed_right_edge_does_not_seed_weaker_island():
    """H21 ≥ floor but leftover < 30 min: do not seed a later weaker H22 island."""
    export, _ = _export_plan(
        rce_by_hour={20: 1.19, 21: 0.99, 22: 0.91, 23: 0.40},
        # After H20, SOC sits on H21's survive floor; H22's floor is much lower.
        reserves_by_hour={20: 16.0, 21: 15.8, 22: 9.0, 23: 8.5},
        initial_soc_kwh=24.0,
        start_hour=20,
        n_hours=4,
        min_hourly_kwh=2.0,
    )
    assert export[20] >= 2.0
    assert export[21] >= 2.0, f"H21 must take leftover before H22, got {export}"
    assert export[22] < 0.5, f"H22 must not island past H21, got {export}"


def test_second_rated_tonight_exports_despite_richer_morning_seed():
    """H07 is richest; 2nd-rated H19 still Dis, then H20 glues. Overnight trough stays weaker."""
    rce = {18: 1.09, 19: 1.31, 20: 1.24, 21: 0.90}
    for h in range(22, 30):
        rce[h] = 0.82
    rce[30] = 1.01
    rce[31] = 1.50
    load_per_hour = 0.8
    reserves = {
        h: 8.0 + load_per_hour * max(0, 31 - h) for h in range(18, 32)
    }
    export, _ = _export_plan(
        rce_by_hour=rce,
        reserves_by_hour=reserves,
        initial_soc_kwh=36.0,
        start_hour=18,
        n_hours=14,
        min_kwh=8.0,
        export_floor=0.68,
        load_per_step=0.2,
    )
    assert export[31] >= 2.0, f"morning H07 must export, got {export}"
    assert export[19] >= 2.0, f"tonight H19 must export as 2nd-rated, got {export}"
    assert export[20] >= 2.0, f"H20 must glue to H19, got {export}"


def test_second_rated_distant_peak_opens_before_adjacent_trough():
    """H19 seed, 2nd-rated H22 across H20/H21 trough still gets a Dis island."""
    export, _ = _export_plan(
        rce_by_hour={18: 0.80, 19: 1.00, 20: 0.70, 21: 0.60, 22: 0.90},
        reserves_by_hour={18: 12.0, 19: 12.0, 20: 12.0, 21: 12.0, 22: 12.0},
        initial_soc_kwh=40.0,
        start_hour=18,
        n_hours=5,
        min_kwh=8.0,
        export_floor=0.50,
        min_hourly_kwh=0.5,
    )
    assert export[19] >= 2.0, f"H19 seed must export, got {export}"
    assert export[22] >= 2.0, f"2nd-rated H22 must island, got {export}"


def test_partial_right_edge_starts_at_hour_start():
    """Right-edge 30/45 min Dis starts at :00, not a tail."""
    export, qmask = _export_plan(
        rce_by_hour={20: 1.19, 21: 0.90, 22: 0.40, 23: 0.40},
        reserves_by_hour={20: 16.0, 21: 14.0, 22: 12.0, 23: 11.0},
        initial_soc_kwh=20.0,
        start_hour=20,
        n_hours=4,
        min_kwh=8.16,
    )
    assert export[20] >= 2.0
    if export[21] >= 2.0:
        assert qmask[21].startswith("1"), f"right edge must start :00, got {qmask[21]}"
        assert qmask[21] in ("1100", "1110", "1111"), qmask[21]


def test_partial_left_edge_flush_to_block():
    """Left-edge 30/45 min Dis ends at the next hour, not :00–:30."""
    export, qmask = _export_plan(
        rce_by_hour={19: 1.05, 20: 1.20, 21: 0.70, 22: 0.40},
        reserves_by_hour={19: 18.0, 20: 16.0, 21: 14.0, 22: 12.0},
        initial_soc_kwh=22.0,
        start_hour=19,
        n_hours=4,
    )
    assert export[20] >= 2.0
    if 2.0 <= export[19] < 6.0:
        assert qmask[19].endswith("1"), f"left edge must flush to :00, got {qmask[19]}"
        assert qmask[19] in ("0011", "0111", "1111"), qmask[19]
