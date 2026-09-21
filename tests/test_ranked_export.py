"""Ranked hourly-RCE export assignment and multi-hour window spans."""

from __future__ import annotations

from datetime import datetime

from src.grid_config import merge_grid_defaults
from src.plan_optimizer import (
    HourControl,
    plan_battery_grid_export,
    export_span_candidates,
    export_window_roles,
    hour_rce_rating,
    hour_rce_rating_5_groszy,
    hourly_avg_rce,
    optimize_horizon,
    pick_next_export_hour,
    rank_hours_by_avg_rce,
    round_rce_5_groszy,
    evening_export_window_hours,
)
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
)


def _cfg(**timer_kw) -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 40.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "epsilon_kwh": 0.01,
            "losses_pct": {
                "grid_to_battery": 0.0,
                "battery_to_load_or_grid": 0.0,
                "pv_to_grid": 0.0,
                "pv_to_load": 0.0,
                "pv_to_battery": 0.0,
            },
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.5,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.0,
                "offpeak_price_pln_kwh": 0.5,
                "peak_energy_only_pln_kwh": 0.6,
                "offpeak_energy_only_pln_kwh": 0.3,
            },
        },
        "timer_schedule": {
            "min_block_minutes": 15,
            "min_hourly_transfer_kwh": 0.0,
            **timer_kw,
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def test_hourly_avg_rce_and_rank():
    rce = [0.4, 0.4, 0.4, 0.4, 0.9, 0.9, 0.9, 0.9, 0.7, 0.7, 0.7, 0.7]
    assert hourly_avg_rce(rce, 0) == 0.4
    assert hourly_avg_rce(rce, 1) == 0.9
    assert rank_hours_by_avg_rce([0, 1, 2], rce, 0.5) == [1, 2]


def test_hour_rce_rating_rounds_to_hundredths():
    # 0.901 / 0.904 → same 0.90 rating; 0.906 → 0.91
    rce = (
        [0.901] * 4
        + [0.904] * 4
        + [0.906] * 4
    )
    assert hour_rce_rating(rce, 0) == 0.90
    assert hour_rce_rating(rce, 1) == 0.90
    assert hour_rce_rating(rce, 2) == 0.91
    assert rank_hours_by_avg_rce([0, 1, 2], rce, 0.5) == [2, 0, 1]


def test_round_rce_5_groszy_half_up():
    assert round_rce_5_groszy(1.12) == 1.10
    assert round_rce_5_groszy(1.14) == 1.15
    assert round_rce_5_groszy(1.125) == 1.15
    assert round_rce_5_groszy(1.15) == 1.15
    assert round_rce_5_groszy(0.025) == 0.05
    assert round_rce_5_groszy(1.06) == 1.05


def test_hour_rce_rating_5_groszy_from_avg():
    rce = [1.14] * 4 + [1.12] * 4 + [1.16] * 4
    assert hour_rce_rating_5_groszy(rce, 0) == 1.15
    assert hour_rce_rating_5_groszy(rce, 1) == 1.10
    assert hour_rce_rating_5_groszy(rce, 2) == 1.15


def test_pick_next_export_hour_seeds_peak_then_grows_edges():
    ratings = {18: 1.28, 19: 1.47, 20: 1.40, 21: 1.17}
    remaining = [18, 19, 20, 21]
    assert pick_next_export_hour(remaining, ratings, selected=set()) == 19
    assert pick_next_export_hour([18, 20, 21], ratings, selected={19}) == 20
    assert pick_next_export_hour([18, 21], ratings, selected={19, 20}) == 18
    assert pick_next_export_hour([21], ratings, selected={18, 19, 20}) == 21
    # Equal-rating edges: prefer the neighbour of the last assigned hour.
    ratings2 = {21: 1.0, 18: 0.9, 20: 0.9}
    assert pick_next_export_hour(
        [18, 20], ratings2, selected={21}, last_hour=21,
    ) == 20
    # Same 5-groszy rating: closer to the selected window, then earlier edge.
    grow = {18: 1.15, 19: 1.15, 20: 1.20, 21: 1.15}
    assert pick_next_export_hour(
        [19, 21], grow, selected={20}, last_hour=20,
    ) == 19
    assert pick_next_export_hour(
        [19, 22], grow | {22: 1.15}, selected={20}, last_hour=20,
        gap_ratings={19: 1.06, 20: 1.14, 22: 1.14},
    ) == 19


def test_pick_next_second_rated_skips_weaker_trough():
    """After morning H07 seed, 2nd-rated tonight H19 wins over adjacent H06."""
    grow = {0: 0.85, 4: 0.80, 6: 1.00, 7: 1.50, 19: 1.30, 20: 1.25, 21: 0.90}
    gap = {0: 0.83, 4: 0.82, 6: 1.01, 7: 1.50, 19: 1.31, 20: 1.24, 21: 0.90}
    remaining = [0, 4, 6, 7, 19, 20, 21]
    assert pick_next_export_hour(remaining, grow, selected=set(), seed_ratings=gap) == 7
    assert pick_next_export_hour(
        [0, 4, 6, 19, 20, 21], grow, selected={7}, last_hour=7, gap_ratings=gap,
    ) == 19
    assert pick_next_export_hour(
        [0, 4, 6, 20, 21], grow, selected={7, 19}, last_hour=19, gap_ratings=gap,
    ) == 20


def test_equal_rating_after_rich_hour_prefers_neighbor():
    """Allocator grows the nearer equal-rating edge before a distant hour."""
    import src.plan_export as pe

    orig = pe.pick_next_export_hour
    tried: list[int] = []

    def _wrap(remaining, ratings, **kwargs):
        h = orig(remaining, ratings, **kwargs)
        tried.append(h)
        return h

    pe.pick_next_export_hour = _wrap
    try:
        offset = 16 * 4
        steps = 20  # 16..20
        rce = (
            [None] * offset
            + [0.90] * 4  # 16
            + [0.40] * 4  # 17 below floor
            + [0.40] * 4  # 18 below floor
            + [1.00] * 4  # 19
            + [0.90] * 4  # 20
        )
        assert hour_rce_rating(rce, 16) == hour_rce_rating(rce, 20) == 0.90
        assert hour_rce_rating(rce, 19) == 1.0
        base = [HourControl(0.0, 0.0) for _ in range(steps)]
        plan_battery_grid_export(
            base,
            steps=steps,
            pv_series=[0.0] * steps,
            load_series=[0.05] * steps,
            rce_series=rce,
            rce_step_offset=offset,
            step_scale=0.25,
            initial_soc_kwh=20.0,
            battery_cap=40.0,
            min_kwh=6.4,
            discharge_dc_step=8.0,
            inverter_ac_step=8.0,
            eta_grid=1.0,
            eta_out=1.0,
            eta_pv_load=1.0,
            eta_pv_grid=1.0,
            eta_pv_battery=1.0,
            eps_step=0.01,
            reserves=[6.4] * steps,
            export_floor=0.5,
            min_hourly_kwh=0.5,
        )
    finally:
        pe.pick_next_export_hour = orig

    assert tried[0] == 19
    assert tried.index(20) < tried.index(16)


def test_seed_uses_unrounded_avg_then_5_groszy_grows_closer_edge():
    """Seed max raw avg; next hours round to 5 groszy; ties go to the nearer edge."""
    import src.plan_export as pe

    orig = pe.pick_next_export_hour
    tried: list[int] = []

    def _wrap(remaining, ratings, **kwargs):
        h = orig(remaining, ratings, **kwargs)
        tried.append(h)
        return h

    pe.pick_next_export_hour = _wrap
    try:
        offset = 16 * 4
        steps = 24
        # H19=1.139 and H20=1.141 both 1.14 at 0.01; seed must be H20.
        # H19=1.139 and H21=1.14 both 1.15 at 5 groszy; grow prefers H19.
        rce = (
            [None] * offset
            + [0.70] * 4  # 16
            + [0.70] * 4  # 17
            + [0.80] * 4  # 18
            + [1.139] * 4  # 19
            + [1.141] * 4  # 20 peak unrounded
            + [1.14] * 4  # 21
        )
        base = [HourControl(0.0, 0.0) for _ in range(steps)]
        plan_battery_grid_export(
            base,
            steps=steps,
            pv_series=[0.0] * steps,
            load_series=[0.05] * steps,
            rce_series=rce,
            rce_step_offset=offset,
            step_scale=0.25,
            initial_soc_kwh=28.0,
            battery_cap=40.0,
            min_kwh=6.4,
            discharge_dc_step=8.0,
            inverter_ac_step=8.0,
            eta_grid=1.0,
            eta_out=1.0,
            eta_pv_load=1.0,
            eta_pv_grid=1.0,
            eta_pv_battery=1.0,
            eps_step=0.01,
            reserves=[6.4] * steps,
            export_floor=0.5,
            min_hourly_kwh=0.5,
        )
    finally:
        pe.pick_next_export_hour = orig

    assert tried[0] == 20
    assert tried[1] == 19
    assert tried[2] == 21


def test_export_window_starts_at_16():
    """Sale window is clock 16–23; 14–15 stay out even with no PV."""
    offset = 14 * 4
    steps = 32  # H14..H21
    hours = list(range(14, 22))
    window = evening_export_window_hours(
        hours,
        pv_series=[0.0] * steps,
        load_series=[0.5 / 4] * steps,
        rce_step_offset=offset,
        slots=4,
        steps=steps,
        eta_pv_load=1.0,
        epsilon=0.01,
    )
    assert window == {16, 17, 18, 19, 20, 21}


def test_export_window_respects_configured_start_hour():
    """Start hour 18 keeps 16–17 out of the sale window."""
    offset = 16 * 4
    steps = 24
    hours = list(range(16, 22))
    window = evening_export_window_hours(
        hours,
        pv_series=[0.0] * steps,
        load_series=[0.5 / 4] * steps,
        rce_step_offset=offset,
        slots=4,
        steps=steps,
        eta_pv_load=1.0,
        epsilon=0.01,
        export_window_start_hour=18,
    )
    assert window == {18, 19, 20, 21}


def test_export_window_includes_16_even_when_pv_covers():
    """H16/H17 stay in the window when PV still covers load."""
    offset = 16 * 4
    steps = 24  # H16..H21
    pv = (
        [3.044 / 4] * 4
        + [1.587 / 4] * 4
        + [0.321 / 4] * 4
        + [0.0] * 12
    )
    load = (
        [0.531 / 4] * 4
        + [0.645 / 4] * 4
        + [0.695 / 4] * 4
        + [0.887 / 4] * 4
        + [1.035 / 4] * 4
        + [1.117 / 4] * 4
    )
    hours = list(range(16, 22))
    window = evening_export_window_hours(
        hours,
        pv_series=pv,
        load_series=load,
        rce_step_offset=offset,
        slots=4,
        steps=steps,
        eta_pv_load=1.0,
        epsilon=0.01,
    )
    assert window == {16, 17, 18, 19, 20, 21}


def test_export_window_closed_by_forecast_pv_cover_outside_slice():
    """PV cover at H08 (full-day forecast) closes leftover; H10 is not in the window."""
    offset = 10 * 4
    steps = 12 * 4
    hours = list(range(10, 22))
    today_pv = [0.0] * 24
    today_load = [0.5] * 24
    today_pv[8] = 2.0
    window = evening_export_window_hours(
        hours,
        pv_series=[0.0] * steps,
        load_series=[0.5 / 4] * steps,
        rce_step_offset=offset,
        slots=4,
        steps=steps,
        eta_pv_load=1.0,
        epsilon=0.01,
        forecast={
            "today": {"pv": today_pv, "load": today_load},
            "tomorrow": {"pv": [0.0] * 24, "load": [0.5] * 24},
        },
    )
    assert 10 not in window
    assert 11 not in window
    assert window == {16, 17, 18, 19, 20, 21}


def test_export_window_overnight_leftover_not_cut_at_midnight():
    """H23 and tomorrow H00–H06 stay one window until morning PV covers."""
    hours = [23, 24, 25, 30]
    window = evening_export_window_hours(
        hours,
        pv_series=[0.0] * 32,
        load_series=[0.15] * 32,
        rce_step_offset=23 * 4,
        slots=4,
        steps=32,
        eta_pv_load=1.0,
        epsilon=0.01,
    )
    assert window == {23, 24, 25, 30}


def test_peak_seeded_then_grows_back_through_16():
    """Window from 16: seed H20, then H19 / H21 / H18 / H17 / H16 by rating."""
    import src.plan_export as pe

    orig = pe.pick_next_export_hour
    tried: list[int] = []

    def _wrap(remaining, ratings, **kwargs):
        h = orig(remaining, ratings, **kwargs)
        tried.append(h)
        return h

    pe.pick_next_export_hour = _wrap
    try:
        offset = 16 * 4
        steps = 24
        rce = (
            [None] * offset
            + [0.69] * 4  # 16
            + [0.73] * 4  # 17
            + [0.86] * 4  # 18 leftover PV
            + [1.10] * 4  # 19
            + [1.15] * 4  # 20 peak
            + [1.10] * 4  # 21
        )
        pv = (
            [3.044 / 4] * 4
            + [1.587 / 4] * 4
            + [0.321 / 4] * 4
            + [0.0] * 12
        )
        load = [0.7 / 4] * steps
        base = [HourControl(0.0, 0.0) for _ in range(steps)]
        controls = plan_battery_grid_export(
            base,
            steps=steps,
            pv_series=pv,
            load_series=load,
            rce_series=rce,
            rce_step_offset=offset,
            step_scale=0.25,
            initial_soc_kwh=36.0,
            battery_cap=40.0,
            min_kwh=6.4,
            discharge_dc_step=2.0,
            inverter_ac_step=2.0,
            eta_grid=1.0,
            eta_out=1.0,
            eta_pv_load=1.0,
            eta_pv_grid=1.0,
            eta_pv_battery=1.0,
            eps_step=0.01,
            reserves=[6.4] * steps,
            export_floor=0.62,
            min_hourly_kwh=0.5,
        )
    finally:
        pe.pick_next_export_hour = orig

    def hour_export(clock: int) -> float:
        return sum(
            controls[step].battery_export_kwh
            for step in range(steps)
            if (offset + step) // 4 == clock
        )

    assert tried[:6] == [20, 19, 21, 18, 17, 16]
    assert hour_export(20) > 0.5
    assert hour_export(19) > 0.5


def test_hold_keeps_peak_when_h18_has_no_claim():
    """H17 must not dump SOC when H18 has PV cover and H20 is richer."""
    offset = 16 * 4
    steps = 24
    rce = (
        [None] * offset
        + [0.69] * 4
        + [0.73] * 4
        + [0.86] * 4
        + [1.10] * 4
        + [1.15] * 4
        + [1.10] * 4
    )
    pv = (
        [3.044 / 4] * 4
        + [1.587 / 4] * 4
        + [1.104 / 4] * 4
        + [0.0] * 12
    )
    load = [0.7 / 4] * steps
    base = [HourControl(0.0, 0.0) for _ in range(steps)]
    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=pv,
        load_series=load,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=22.0,
        battery_cap=40.0,
        min_kwh=8.0,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        eps_step=0.01,
        reserves=[8.0] * steps,
        export_floor=0.62,
        min_hourly_kwh=2.0,
    )

    def hour_export(clock: int) -> float:
        return sum(
            controls[step].battery_export_kwh
            for step in range(steps)
            if (offset + step) // 4 == clock
        )

    assert hour_export(20) >= 2.0
    assert hour_export(17) <= hour_export(20)


def test_hold_keeps_peak_when_h18_has_no_claim():
    """H17 must not dump SOC when H18 has PV cover and H20 is richer."""
    offset = 16 * 4
    steps = 24
    rce = (
        [None] * offset
        + [0.69] * 4
        + [0.73] * 4
        + [0.86] * 4
        + [1.10] * 4
        + [1.15] * 4
        + [1.10] * 4
    )
    pv = (
        [3.044 / 4] * 4
        + [1.587 / 4] * 4
        + [1.104 / 4] * 4
        + [0.0] * 12
    )
    load = [0.7 / 4] * steps
    base = [HourControl(0.0, 0.0) for _ in range(steps)]
    controls = plan_battery_grid_export(
        base,
        steps=steps,
        pv_series=pv,
        load_series=load,
        rce_series=rce,
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=22.0,
        battery_cap=40.0,
        min_kwh=8.0,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        eps_step=0.01,
        reserves=[8.0] * steps,
        export_floor=0.62,
        min_hourly_kwh=2.0,
    )

    def hour_export(clock: int) -> float:
        return sum(
            controls[step].battery_export_kwh
            for step in range(steps)
            if (offset + step) // 4 == clock
        )

    assert hour_export(20) >= 2.0
    assert hour_export(17) <= hour_export(20)


def test_export_window_roles_and_spans():
    assert export_window_roles({5}) == {5: "single"}
    assert export_window_roles({5, 6, 7}) == {5: "first", 6: "middle", 7: "last"}
    assert export_span_candidates("middle") == [(0, 4)]
    assert export_span_candidates("first")[0] == (0, 4)
    assert export_span_candidates("last") == [(0, 4), (0, 3), (0, 2)]
    # Single: longest first includes full hour
    assert export_span_candidates("single")[0] == (0, 4)


def test_richer_hour_gets_export_before_cheaper():
    """With limited SOC, rank-1 hour exports more than the cheaper following hour."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    # Two hours: 18 avg 1.0, 19 avg 0.6; threshold 0.5.
    offset = 18 * 4
    steps = 8
    rce = [None] * offset + [1.0] * 4 + [0.6] * 4
    pv = [0.0] * steps
    load = [0.2] * steps
    buy = [0.5] * steps
    end = datetime(2026, 7, 18, 20, 0)
    controls = optimize_horizon(
        steps=steps,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=rce,
        initial_soc_kwh=14.0,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.2] * 24, "pv_total": 0.0, "load_total": 4.8},
            "tomorrow": {"pv": [3.0] * 24, "load": [0.2] * 24, "pv_total": 72.0, "load_total": 4.8},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    exp18 = sum(c.battery_export_kwh for c in controls[:4])
    exp19 = sum(c.battery_export_kwh for c in controls[4:])
    assert exp18 > 1.0
    assert exp18 >= exp19


def test_later_richer_hour_keeps_soc_over_earlier_cheaper():
    """Cheaper hour 18 must not drain SOC needed for richer hour 19."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    offset = 18 * 4
    steps = 8
    # Hour 18 cheap, 19 expensive — limited SOC should prefer 19.
    rce = [None] * offset + [0.55] * 4 + [1.2] * 4
    end = datetime(2026, 7, 18, 20, 0)
    controls = optimize_horizon(
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.15] * steps,
        buy_prices=[0.5] * steps,
        rce_series=rce,
        initial_soc_kwh=12.0,
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.15] * 24, "pv_total": 0.0, "load_total": 3.6},
            "tomorrow": {"pv": [3.0] * 24, "load": [0.15] * 24, "pv_total": 72.0, "load_total": 3.6},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    exp18 = sum(c.battery_export_kwh for c in controls[:4])
    exp19 = sum(c.battery_export_kwh for c in controls[4:])
    assert exp19 > 1.0
    assert exp19 >= exp18


def test_avg_below_floor_no_export():
    cfg = _cfg()
    cfg["grid"]["grid_export_threshold_pln_kwh"] = 0.62
    params = get_simulation_params(cfg)
    offset = 18 * 4
    # avg = 0.60 < 0.62
    rce = [None] * offset + [0.50, 0.70, 0.70, 0.50]
    controls = optimize_horizon(
        steps=4,
        pv_series=[0.0] * 4,
        load_series=[0.2] * 4,
        buy_prices=[0.5] * 4,
        rce_series=rce,
        initial_soc_kwh=18.0,
        cfg=cfg,
        params=params,
        end_dt=datetime(2026, 7, 18, 19, 0),
        today_date=datetime(2026, 7, 18).date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.0] * 24, "pv_total": 0.0, "load_total": 0.0},
            "tomorrow": {"pv": [0.0] * 24, "load": [0.0] * 24, "pv_total": 0.0, "load_total": 0.0},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    assert all(c.battery_export_kwh == 0.0 for c in controls)


def test_chrono_fill_opens_second_hour_to_reach_post_dis_floor():
    """After H20 time-limits above post_dis(20), H21 sells leftover to post_dis(21)."""
    cfg = _cfg(min_hourly_transfer_kwh=2.0)
    params = get_simulation_params(cfg)
    load_today = [0.2] * 24
    load_today[20] = 0.8
    load_today[21] = 1.2
    load_today[22] = 1.3
    load_today[23] = 1.0
    load_tom = [1.0, 0.9, 0.7, 0.6, 0.5, 0.5, 0.5, 0.5] + [0.4] * 16
    pv_tom = [0.0] * 7 + [2.0] + [3.0] * 16
    offset = 19 * 4
    steps = 20  # 19..23
    rce = [None] * offset + [0.5] * 4 + [0.93] * 4 + [0.96] * 4 + [0.5] * 8
    pv_s, load_s, buy_s = [], [], []
    for h in range(19, 24):
        for _ in range(4):
            pv_s.append(0.0)
            load_s.append(load_today[h] / 4.0)
            buy_s.append(0.5)
    # ~55% of 40 kWh — H20 alone cannot reach post_dis(20); leftover needs H21.
    controls = optimize_horizon(
        steps=steps,
        pv_series=pv_s,
        load_series=load_s,
        buy_prices=buy_s,
        rce_series=rce + [0.5] * 8,
        initial_soc_kwh=22.0,
        cfg=cfg,
        params=params,
        end_dt=datetime(2026, 7, 27, 0, 0),
        today_date=datetime(2026, 7, 26).date(),
        rce_map={},
        forecast={
            "today": {
                "pv": [0.0] * 24,
                "load": load_today,
                "pv_total": 0.0,
                "load_total": sum(load_today),
            },
            "tomorrow": {
                "pv": pv_tom,
                "load": load_tom,
                "pv_total": sum(pv_tom),
                "load_total": sum(load_tom),
            },
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    exp20 = sum(c.battery_export_kwh for c in controls[4:8])
    exp21 = sum(c.battery_export_kwh for c in controls[8:12])
    assert exp20 > 1.0
    assert exp21 > 1.0


def test_chrono_fill_uses_later_hour_when_surplus_is_large():
    """With a very full battery, later rich hours still get a Dis block."""
    cfg = _cfg(min_hourly_transfer_kwh=0.5)
    params = get_simulation_params(cfg)
    offset = 19 * 4
    steps = 12  # hours 19,20,21
    rce = [None] * offset + [0.70] * 4 + [0.60] * 4 + [1.0] * 4
    end = datetime(2026, 7, 18, 22, 0)
    controls = optimize_horizon(
        steps=steps,
        pv_series=[0.0] * steps,
        load_series=[0.1] * steps,
        buy_prices=[0.5] * steps,
        rce_series=rce,
        initial_soc_kwh=36.0,  # nearly full 40 kWh pack
        cfg=cfg,
        params=params,
        end_dt=end,
        today_date=end.date(),
        rce_map={},
        forecast={
            "today": {"pv": [0.0] * 24, "load": [0.1] * 24, "pv_total": 0.0, "load_total": 2.4},
            "tomorrow": {"pv": [4.0] * 24, "load": [0.1] * 24, "pv_total": 96.0, "load_total": 2.4},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )
    exp19 = sum(c.battery_export_kwh for c in controls[0:4])
    exp20 = sum(c.battery_export_kwh for c in controls[4:8])
    exp21 = sum(c.battery_export_kwh for c in controls[8:12])
    assert exp19 + exp20 + exp21 > 5.0
    # Chrono fills earlier hours first; with large surplus a later hour still opens.
    assert exp19 > 0.5
    assert exp20 > 0.5 or exp21 > 0.5


def test_floor_ignores_load_only_quarters_outside_export_span():
    """Bat Discharge floor is measured only inside the Dis span.

    Repro of hour 23: ~0.5 kWh export in q0 plus ~1.1 kWh load-only discharge
    in q1–q3 must NOT count as meeting min_hourly_transfer — otherwise the plan
    shows Feed-in/+PLN without a Dis timer.
    """
    from src.plan_optimizer import plan_hour_battery_grid_export_claim

    claim = plan_hour_battery_grid_export_claim(
        hour=23,
        role="last",
        soc0=8.0,  # ~18.6% of 43 — only a thin slice above min+reserve
        hold_soc_kwh=0.0,
        pv_q=[0.0, 0.0, 0.0, 0.0],
        load_q=[0.25, 0.25, 0.25, 0.25],
        reserve_q=[6.9, 6.9, 6.9, 6.9],  # overnight reserve ≈ 16%
        base_charge_q=[0.0, 0.0, 0.0, 0.0],
        battery_cap=43.0,
        min_kwh=6.88,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=0.925,
        eta_out=0.925,
        eta_pv_load=0.925,
        eta_pv_grid=0.925,
        eta_pv_battery=0.925,
        eps_step=0.01,
        min_hourly_kwh=2.0,
    )
    assert claim is None


def test_plan_rows_never_show_orphan_export_without_dis_timer():
    """Battery feed-in above the hourly floor requires a Dis timer_schedule."""
    from src.plan_q15 import run_day_smart_q15_plan, timer_schedule_by_hour
    from src.grid_config import merge_grid_defaults
    from src.simulation_config import merge_simulation_defaults
    from tests.test_discharge_power_invariants import (
        DATE, LOAD_TODAY, PV_TODAY, RCE_Q,
    )

    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
        "grid": {},
    }
    merge_grid_defaults(cfg)
    cfg = merge_simulation_defaults(cfg)
    res = run_day_smart_q15_plan(
        date_str=DATE,
        pv_hourly=PV_TODAY,
        load_hourly=LOAD_TODAY,
        tomorrow_pv=PV_TODAY,
        tomorrow_load=LOAD_TODAY,
        cfg=cfg,
        rce_quarters=list(RCE_Q),
        initial_soc_kwh=0.85 * 43.0,
        from_hour=0,
    )
    assert res is not None
    timers = timer_schedule_by_hour(res["q15_by_hour"], cfg, res["epsilon"])
    eps = float(res["epsilon"])
    for h, slots in res["q15_by_hour"].items():
        exp = sum(float(s.get("battery_export_kwh") or 0) for s in slots)
        if exp <= eps:
            continue
        txt = timers.get(h) or ""
        assert txt.startswith("Dis"), (
            f"hour {h}: battery export {exp:.3f} kWh but timer={txt!r} "
            f"(orphan feed-in without Dis — the 23:00 +0.38 PLN case)"
        )


def test_middle_hour_is_full_four_quarters():
    base = [HourControl(0.0, 0.0) for _ in range(12)]
    selected = {19, 20, 21}
    roles = export_window_roles(selected)
    assert roles[20] == "middle"
    assert export_span_candidates(roles[20]) == [(0, 4)]
    offset = 19 * 4
    controls = plan_battery_grid_export(
        base,
        steps=12,
        pv_series=[0.0] * 12,
        load_series=[0.1] * 12,
        rce_series=[0.9] * (22 * 4),
        rce_step_offset=offset,
        step_scale=0.25,
        initial_soc_kwh=35.0,
        battery_cap=40.0,
        min_kwh=6.4,
        discharge_dc_step=2.0,
        inverter_ac_step=2.0,
        eta_grid=1.0,
        eta_out=1.0,
        eta_pv_load=1.0,
        eta_pv_grid=1.0,
        eta_pv_battery=1.0,
        eps_step=0.01,
        reserves=[6.4] * 12,
        export_floor=0.5,
        min_hourly_kwh=0.0,
    )
    # Middle hour steps 4..7 should all export when SOC allows
    mid = controls[4:8]
    assert all(c.battery_export_kwh > 0.05 for c in mid)
