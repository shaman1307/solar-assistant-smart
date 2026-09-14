"""Ranked battery→grid export after DP (RCE windows, claims, chrono fill)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .grid_config import grid_export_threshold_pln_kwh
from .plan_physics import (
    HourControl,
    pv_load_energy_split,
    simulate_hour,
    slots_per_hour_from_scale,
)
from .plan_reserve import (
    HOURS_PER_DAY,
    _ALL_OFFPEAK_COVER_HOUR_END,
    morning_cover_bound_from_hour_buys,
)


@dataclass(frozen=True)
class G12Tariff:
    offpeak_full: float
    offpeak_energy: float
    peak_full: float
    peak_energy: float


def g12_tariff_from_cfg(cfg: dict) -> G12Tariff:
    g12 = cfg["grid"]["g12"]
    return G12Tariff(
        offpeak_full=float(g12["offpeak_price_pln_kwh"]),
        offpeak_energy=float(g12["offpeak_energy_only_pln_kwh"]),
        peak_full=float(g12["peak_price_pln_kwh"]),
        peak_energy=float(g12["peak_energy_only_pln_kwh"]),
    )


def battery_export_break_even_rce(tariff: G12Tariff, cfg: dict | None = None) -> float:
    """Minimum RCE (PLN/kWh) for battery export vs self-use at offpeak buy."""
    if cfg is not None:
        return grid_export_threshold_pln_kwh(cfg)
    return tariff.offpeak_full


def _rce_at_or_above(
    rce_series: list[float | None],
    step: int,
    floor: float,
    *,
    epsilon: float = 0.0,
) -> bool:
    if step < 0 or step >= len(rce_series):
        return False
    rce = rce_series[step]
    return rce is not None and float(rce) >= floor - epsilon


def battery_export_step_allowed(
    step: int,
    rce_series: list[float | None],
    floor: float,
    *,
    step_scale: float = 0.25,
    epsilon: float = 0.0,
) -> bool:
    """Q15 neighbour RCE gate used by unit tests.

    Production export assignment uses ranked hourly average RCE
    (``plan_battery_grid_export``).
    """
    if not _rce_at_or_above(rce_series, step, floor, epsilon=epsilon):
        return False
    slots_per_hour = slots_per_hour_from_scale(step_scale)
    q_in_hour = step % slots_per_hour
    if q_in_hour >= 1 and _rce_at_or_above(rce_series, step - 1, floor, epsilon=epsilon):
        return True
    if q_in_hour < slots_per_hour - 1 and _rce_at_or_above(
        rce_series, step + 1, floor, epsilon=epsilon,
    ):
        return True
    if slots_per_hour == 1 and _rce_at_or_above(rce_series, step - 1, floor, epsilon=epsilon):
        return True
    return False


def hourly_avg_rce(
    rce_series: list[float | None],
    hour: int,
    *,
    slots_per_hour: int = 4,
) -> float | None:
    """Mean RCE over the clock hour (None if no priced quarters)."""
    start = int(hour) * int(slots_per_hour)
    vals: list[float] = []
    for i in range(int(slots_per_hour)):
        idx = start + i
        if 0 <= idx < len(rce_series) and rce_series[idx] is not None:
            vals.append(float(rce_series[idx]))
    if not vals:
        return None
    return sum(vals) / len(vals)


def hour_rce_rating(
    rce_series: list[float | None],
    hour: int,
    *,
    slots_per_hour: int = 4,
) -> float | None:
    """Hour avg RCE rounded to 0.01 — eligibility vs export floor and hold-SOC."""
    avg = hourly_avg_rce(rce_series, hour, slots_per_hour=slots_per_hour)
    if avg is None:
        return None
    return round(float(avg), 2)


def round_rce_5_groszy(value: float) -> float:
    """Round PLN/kWh to 5 groszy, half up."""
    steps = Decimal(str(value)) / Decimal("0.05")
    n = steps.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(n * Decimal("0.05"))


def hour_rce_rating_5_groszy(
    rce_series: list[float | None],
    hour: int,
    *,
    slots_per_hour: int = 4,
) -> float | None:
    """Grow-from-peak ranking: hour avg RCE rounded to 5 groszy."""
    avg = hourly_avg_rce(rce_series, hour, slots_per_hour=slots_per_hour)
    if avg is None:
        return None
    return round_rce_5_groszy(avg)


def _distance_to_hour_run(hour: int, selected: set[int] | list[int]) -> int:
    """Gap from *hour* to the [min, max] selected run (0 if inside)."""
    sel = {int(h) for h in selected}
    if not sel:
        return 0
    lo, hi = min(sel), max(sel)
    h = int(hour)
    if h < lo:
        return lo - h
    if h > hi:
        return h - hi
    return 0


def rank_hours_by_avg_rce(
    hours: list[int],
    rce_series: list[float | None],
    floor: float,
    *,
    slots_per_hour: int = 4,
    epsilon: float = 0.0,
) -> list[int]:
    """Hours with rating ≥ floor, richest first (ties: earlier hour).

    Rating is avg RCE rounded to hundredths. Allocation uses
    ``pick_next_export_hour``: seed the unrounded peak, then grow by
    5-groszy rating.
    """
    scored: list[tuple[float, int]] = []
    for h in hours:
        rating = hour_rce_rating(rce_series, h, slots_per_hour=slots_per_hour)
        if rating is not None and rating + epsilon >= floor:
            scored.append((rating, int(h)))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [h for _, h in scored]


def pick_next_export_hour(
    remaining: list[int],
    ratings: dict[int, float],
    *,
    selected: set[int] | list[int] = (),
    last_hour: int | None = None,
    seed_ratings: dict[int, float] | None = None,
    gap_ratings: dict[int, float] | None = None,
    export_window_start_hour: int = 16,
    cover_bound: int | None = None,
) -> int:
    """Next hour: seed the unrounded peak, then grow by 5-groszy rating.

    After the seed, *ratings* are 5-groszy avgs among hours that do not jump
    a still-eligible gap. Same rating: closer to the selected window, then
    neighbour of *last_hour*, then earlier hour.
    """
    if not remaining:
        raise ValueError("remaining hours empty")
    sel = {int(h) for h in selected}
    start = int(export_window_start_hour)
    if not sel:
        rank_src = seed_ratings if seed_ratings is not None else ratings
        pool = list(remaining)
        best = max(float(rank_src[h]) for h in pool)
        tied = [h for h in pool if abs(float(rank_src[h]) - best) <= 1e-12]
        return min(tied)

    eligible = gap_ratings if gap_ratings is not None else ratings
    no_gap = [
        h for h in remaining
        if not _export_seed_jumps_rated_gap(
            int(h), sel, eligible, export_window_start_hour=start,
            cover_bound=cover_bound,
        )
    ]
    pool = no_gap or list(remaining)
    best = max(float(ratings[h]) for h in pool)
    tied = [h for h in pool if abs(float(ratings[h]) - best) <= 1e-12]
    return min(
        tied,
        key=lambda h: (
            _distance_to_hour_run(h, sel),
            abs(int(h) - int(last_hour)) if last_hour is not None else 0,
            int(h),
        ),
    )


def _export_seed_jumps_rated_gap(
    hour: int,
    selected: set[int],
    ratings: dict[int, float],
    *,
    export_window_start_hour: int = 16,
    cover_bound: int | None = None,
) -> bool:
    """True when *hour* would skip a still-eligible hour next to the run."""
    if not selected:
        return False
    start = int(export_window_start_hour)
    if not _same_sale_window(
        hour, min(selected), export_window_start_hour=start, cover_bound=cover_bound,
    ):
        return False
    lo, hi = min(selected), max(selected)
    h = int(hour)
    if h > hi + 1:
        return any(
            x in ratings and _same_sale_window(
                x, h, export_window_start_hour=start, cover_bound=cover_bound,
            )
            for x in range(hi + 1, h)
        )
    if h < lo - 1:
        return any(
            x in ratings and _same_sale_window(
                x, h, export_window_start_hour=start, cover_bound=cover_bound,
            )
            for x in range(h + 1, lo)
        )
    return False


def trim_remaining_after_failed_export_edge(
    remaining: list[int],
    *,
    selected: set[int],
    failed_hour: int,
    export_window_start_hour: int = 16,
    cover_bound: int | None = None,
) -> list[int]:
    """Drop same-window hours beyond a failed ≥-threshold edge so a weaker island cannot seed."""
    if not selected:
        return remaining
    lo, hi = min(selected), max(selected)
    failed = int(failed_hour)
    start = int(export_window_start_hour)

    def _other_window(h: int) -> bool:
        return not _same_sale_window(
            h, failed, export_window_start_hour=start, cover_bound=cover_bound,
        )

    if failed > hi:
        return [x for x in remaining if x < failed or _other_window(x)]
    if failed < lo:
        return [x for x in remaining if x > failed or _other_window(x)]
    return remaining


def export_window_roles(selected_hours: set[int] | list[int]) -> dict[int, str]:
    """Classify each selected hour as single|first|middle|last in its run."""
    hours = sorted({int(h) for h in selected_hours})
    roles: dict[int, str] = {}
    if not hours:
        return roles
    run = [hours[0]]
    for h in hours[1:]:
        if h == run[-1] + 1:
            run.append(h)
        else:
            _assign_export_run_roles(run, roles)
            run = [h]
    _assign_export_run_roles(run, roles)
    return roles


def _assign_export_run_roles(run: list[int], roles: dict[int, str]) -> None:
    if len(run) == 1:
        roles[run[0]] = "single"
        return
    roles[run[0]] = "first"
    for h in run[1:-1]:
        roles[h] = "middle"
    roles[run[-1]] = "last"


def export_span_candidates(role: str) -> list[tuple[int, int]]:
    """Allowed (start_q, end_q_exclusive) spans for a window role, longest first.

    Quarters: 0=:00-:15 … 3=:45-:00. end_exclusive=4 means end at next :00.
    """
    if role == "middle":
        return [(0, 4)]
    if role == "first":
        # Start :00/:15/:30; must end at next :00.
        return [(0, 4), (1, 4), (2, 4)]
    if role == "last":
        # Start :00 only; end :00 / :45 / :30.
        return [(0, 4), (0, 3), (0, 2)]
    # single-hour window: start :00/:15/:30, end :30/:45/:00
    cands: list[tuple[int, int, int]] = []
    for start in (0, 1, 2):
        for end in (2, 3, 4):
            if end > start:
                cands.append((start, end, end - start))
    cands.sort(key=lambda t: (-t[2], t[0], t[1]))
    return [(s, e) for s, e, _ in cands]


def _hour_steps_in_horizon(
    *,
    hour: int,
    steps: int,
    rce_step_offset: int,
    slots_per_hour: int,
) -> list[int]:
    out: list[int] = []
    for step in range(steps):
        global_step = rce_step_offset + step
        if global_step // slots_per_hour == hour:
            out.append(step)
    return out


def _hour_pv_covers_load(
    hour: int,
    *,
    pv_series: list[float],
    load_series: list[float],
    rce_step_offset: int,
    slots: int,
    steps: int,
    eta_pv_load: float,
    epsilon: float,
    forecast: dict[str, Any] | None = None,
) -> bool:
    """Whether this clock hour's PV covers house load (generation underway)."""
    idxs = _hour_steps_in_horizon(
        hour=hour, steps=steps, rce_step_offset=rce_step_offset,
        slots_per_hour=slots,
    )
    if idxs:
        pv_h = sum(float(pv_series[i]) for i in idxs)
        load_h = sum(float(load_series[i]) for i in idxs)
    else:
        day = int(hour) // HOURS_PER_DAY
        clock = int(hour) % HOURS_PER_DAY
        if day == 0:
            day_fc = (forecast or {}).get("today") or {}
        elif day == 1:
            day_fc = (forecast or {}).get("tomorrow") or {}
        else:
            return False
        pv_list = day_fc.get("pv") or []
        load_list = day_fc.get("load") or []
        if clock >= len(pv_list) or clock >= len(load_list):
            return False
        pv_h = float(pv_list[clock] or 0.0)
        load_h = float(load_list[clock] or 0.0)
    if eta_pv_load <= 0:
        return False
    if pv_h <= float(epsilon):
        return False
    return pv_h * float(eta_pv_load) >= load_h - float(epsilon)


def evening_export_window_hours(
    hours: list[int],
    *,
    pv_series: list[float],
    load_series: list[float],
    rce_step_offset: int,
    slots: int,
    steps: int,
    eta_pv_load: float,
    epsilon: float,
    export_window_start_hour: int = 16,
    forecast: dict[str, Any] | None = None,
    cover_bound: int | None = None,
    cfg: dict[str, Any] | None = None,
    today_date=None,
) -> set[int]:
    """Hours from *export_window_start_hour* through overnight until PV covers load.

    Midnight does not split the window. Leftover stays open until the first hour
    where PV covers the house, looking only as far as the next G12 morning-peak
    end. Clock hours from that bound up to (not including) the start hour stay
    out. Cover uses the full available PV/load (forecast hourly when the
    optimizer slice omits an hour).
    """
    start = max(0, min(HOURS_PER_DAY - 1, int(export_window_start_hour)))
    bound = cover_bound
    if bound is None and cfg is not None and today_date is not None:
        tariff = g12_tariff_from_cfg(cfg)
        bound = morning_cover_bound_from_hour_buys(
            offpeak_buy=tariff.offpeak_full, epsilon=epsilon,
            cfg=cfg, today_date=today_date,
        )
    if bound is None:
        bound = _ALL_OFFPEAK_COVER_HOUR_END
    bound = max(0, min(HOURS_PER_DAY, int(bound)))
    hours_set = {int(h) for h in hours}
    if not hours_set:
        return set()
    hi = max(hours_set)
    in_window: set[int] = set()
    leftover_open = True
    for h in range(0, hi + 1):
        clock = h % HOURS_PER_DAY
        if clock >= start:
            leftover_open = True
            if h in hours_set:
                in_window.add(h)
            continue
        if clock < bound:
            covered = _hour_pv_covers_load(
                h, pv_series=pv_series, load_series=load_series,
                rce_step_offset=rce_step_offset, slots=slots, steps=steps,
                eta_pv_load=eta_pv_load, epsilon=epsilon, forecast=forecast,
            )
            if covered:
                leftover_open = False
            elif leftover_open and h in hours_set:
                in_window.add(h)
            continue
        leftover_open = False
    return in_window


@dataclass(frozen=True)
class BatteryGridExportHourClaim:
    """Export assignment for one clock hour (rank-order greedy)."""

    hour: int
    span: tuple[int, int]
    export_q: tuple[float, float, float, float]
    bat_discharge_kwh: float

    @property
    def export_ac_kwh(self) -> float:
        return float(sum(self.export_q))


def _same_sale_window(
    hour_a: int,
    hour_b: int,
    *,
    export_window_start_hour: int = 16,
    cover_bound: int | None = None,
) -> bool:
    """Whether two absolute hours sit in one start-hour→morning sale window.

    Hours from the G12 morning-peak end until *export_window_start_hour* are a
    gap, so tonight and tomorrow evening are distinct. Midnight is not a gap.
    """
    start = max(0, min(HOURS_PER_DAY - 1, int(export_window_start_hour)))
    bound = _ALL_OFFPEAK_COVER_HOUR_END if cover_bound is None else max(
        0, min(HOURS_PER_DAY, int(cover_bound)),
    )
    lo, hi = (int(hour_a), int(hour_b)) if int(hour_a) <= int(hour_b) else (int(hour_b), int(hour_a))
    for h in range(lo, hi + 1):
        clock = h % HOURS_PER_DAY
        if bound <= clock < start:
            return False
    return True


def _export_hours_same_run(
    hours: set[int] | list[int],
    hour: int,
    *,
    export_window_start_hour: int = 16,
    cover_bound: int | None = None,
) -> set[int]:
    """Hours in the same start-hour→morning sale window as *hour* (not next evening)."""
    target = int(hour)
    out = {target}
    for h in hours:
        if _same_sale_window(
            int(h), target, export_window_start_hour=export_window_start_hour,
            cover_bound=cover_bound,
        ):
            out.add(int(h))
    return out


def sale_windows(
    hours: list[int] | set[int],
    *,
    export_window_start_hour: int = 16,
    cover_bound: int | None = None,
) -> list[list[int]]:
    """Partition hours into chronological start-hour→morning sale windows."""
    ordered = sorted({int(h) for h in hours})
    if not ordered:
        return []
    start = int(export_window_start_hour)
    windows: list[list[int]] = []
    run = [ordered[0]]
    for h in ordered[1:]:
        if _same_sale_window(
            run[-1], h, export_window_start_hour=start, cover_bound=cover_bound,
        ):
            run.append(h)
        else:
            windows.append(run)
            run = [h]
    windows.append(run)
    return windows


def hold_soc_for_later_battery_grid_export_claims(
    claims: dict[int, BatteryGridExportHourClaim],
    *,
    from_hour: int,
    eta_out: float,
    ratings: dict[int, float] | None = None,
    export_window_start_hour: int = 16,
    cover_bound: int | None = None,
) -> float:
    """DC kWh to hold for richer later hours in the same start-hour→morning window.

    Survive-until-morning is already in per-step reserves. Do not hold SOC for a
    later evening across the noon→start-hour gap — that window is re-planned after
    daytime PV. A hole (hour with no Dis) does not split tonight's window.
    Within tonight, only strictly higher-rated later hours may shrink this hour.
    """
    if not claims:
        return 0.0
    same_run = _export_hours_same_run(
        set(claims) | {int(from_hour)}, int(from_hour),
        export_window_start_hour=export_window_start_hour,
        cover_bound=cover_bound,
    )
    from_rating = (
        float(ratings[int(from_hour)])
        if ratings is not None and int(from_hour) in ratings
        else None
    )
    need_ac = 0.0
    for h, claim in claims.items():
        hi = int(h)
        if hi <= int(from_hour) or hi not in same_run:
            continue
        if from_rating is not None:
            later_rating = float(ratings.get(hi, 0.0)) if ratings is not None else 0.0
            if later_rating <= from_rating + 1e-12:
                continue
        need_ac += claim.export_ac_kwh
    if eta_out <= 0:
        return need_ac
    return need_ac / eta_out


def _apply_battery_grid_export_claims_chrono(
    base_controls: list[HourControl],
    claims: dict[int, BatteryGridExportHourClaim],
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    rce_step_offset: int,
    step_scale: float,
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
) -> tuple[list[HourControl], list[float]]:
    """Chrono replay of DP base + claims. Returns controls and soc_at_step_start."""
    slots = slots_per_hour_from_scale(step_scale)
    out: list[HourControl] = []
    soc_starts: list[float] = []
    soc = initial_soc_kwh
    for step in range(steps):
        soc_starts.append(soc)
        base = base_controls[step] if step < len(base_controls) else HourControl(0.0, 0.0)
        global_step = rce_step_offset + step
        hour = global_step // slots
        q = global_step % slots
        export = 0.0
        claim = claims.get(hour)
        if (
            claim is not None
            and claim.span[0] <= q < claim.span[1]
            and base.grid_charge_kw <= eps_step
        ):
            export = float(claim.export_q[q])
        reserve_soc = float(reserves[step])
        if claim is not None and claim.span[0] <= q < claim.span[1] and claim.span[1] >= 4:
            next_idxs = _hour_steps_in_horizon(
                hour=hour + 1,
                steps=steps,
                rce_step_offset=rce_step_offset,
                slots_per_hour=slots,
            )
            if next_idxs:
                reserve_soc = min(reserve_soc, float(reserves[next_idxs[0]]))
        ctrl = HourControl(base.grid_charge_kw, export, base.load_from_grid)
        phys = simulate_hour(
            soc, pv_series[step], load_series[step], ctrl,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac_step,
            discharge_dc_cap_kwh=discharge_dc_step,
            eta_grid=eta_grid, eta_out=eta_out,
            eta_pv_load=eta_pv_load, eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery, epsilon=eps_step,
            reserve_soc_kwh=reserve_soc,
        )
        delivered = min(ctrl.battery_export_kwh, phys.grid_export)
        out.append(HourControl(base.grid_charge_kw, delivered, base.load_from_grid))
        soc = phys.soc_end
    soc_starts.append(soc)
    return out, soc_starts


def _sim_hour_battery_grid_export_at_cap(
    *,
    soc0: float,
    pv_q: list[float],
    load_q: list[float],
    reserve_q: list[float],
    base_charge_q: list[float],
    span: tuple[int, int],
    dc_cap_per_q: float,
    hold_soc_kwh: float,
    hour_end_floor_kwh: float | None,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
) -> tuple[list[float], float]:
    """One clock hour at a constant DC discharge budget per active quarter.

    Returns (export_q[4], bat_discharge_kwh in the export *span* only).

    Floor checks must not credit load-only quarters outside the Dis window —
    otherwise a 0.5 kWh orphan export passes min_hourly via overnight house load.
    Outside the Dis span, PV must not bank into the battery for a later cheaper
    dump in the same hour during claim scoring.
    """
    exports = [0.0, 0.0, 0.0, 0.0]
    soc = soc0
    bat_dis = 0.0
    for q in range(4):
        charge = float(base_charge_q[q]) if q < len(base_charge_q) else 0.0
        export = 0.0
        in_span = span[0] <= q < span[1]
        step_dc = dc_cap_per_q if in_span else discharge_dc_step
        reserve_floor = float(reserve_q[q])
        if in_span and charge <= eps_step:
            if span[1] >= 4 and hour_end_floor_kwh is not None:
                reserve_floor = min(reserve_floor, float(hour_end_floor_kwh))
            effective_reserve = max(reserve_floor, min_kwh) + hold_soc_kwh
            export = max_battery_export_kwh(
                soc, pv_q[q], load_q[q],
                min_kwh=min_kwh,
                ac_cap_kw=inverter_ac_step,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                reserve_soc_kwh=effective_reserve,
                epsilon=eps_step,
                discharge_dc_cap_kwh=step_dc,
            )
        ctrl = HourControl(charge, export, False)
        soc_before = soc
        phys = simulate_hour(
            soc, pv_q[q], load_q[q], ctrl,
            battery_cap=battery_cap, min_kwh=min_kwh, ac_cap_kw=inverter_ac_step,
            discharge_dc_cap_kwh=step_dc,
            eta_grid=eta_grid, eta_out=eta_out,
            eta_pv_load=eta_pv_load, eta_pv_grid=eta_pv_grid,
            eta_pv_battery=eta_pv_battery, epsilon=eps_step,
            reserve_soc_kwh=max(reserve_floor, min_kwh),
        )
        delivered = min(export, phys.grid_export)
        exports[q] = delivered
        if in_span:
            bat_dis += max(0.0, -phys.battery_delta)
            soc = phys.soc_end
        elif phys.battery_delta > eps_step:
            # Outside Dis: do not bank PV into the battery to dump on later,
            # cheaper quarters of the same hour during claim scoring.
            soc = soc_before
        else:
            soc = phys.soc_end
    return exports, bat_dis


def _trim_span_to_active_battery_grid_exports(
    role: str,
    span: tuple[int, int],
    exports: list[float],
    *,
    eps: float,
) -> tuple[int, int] | None:
    """Shrink *span* to contiguous active export quarters still legal for *role*."""
    active = [q for q in range(span[0], span[1]) if exports[q] > eps]
    if not active:
        return None
    lo, hi = active[0], active[-1] + 1
    if active != list(range(lo, hi)):
        return None
    trimmed = (lo, hi)
    if trimmed not in export_span_candidates(role):
        return None
    return trimmed


def _export_span_quarter_revenue(
    exports: list[float],
    rce_q: list[float | None],
    *,
    eps: float,
) -> float:
    """Sum export_kwh * RCE over quarters (None RCE → 0 revenue contribution)."""
    rev = 0.0
    for q in range(4):
        exp = float(exports[q]) if q < len(exports) else 0.0
        if exp <= eps:
            continue
        price = rce_q[q] if q < len(rce_q) else None
        if price is None:
            continue
        rev += exp * float(price)
    return rev


def plan_hour_battery_grid_export_claim(
    *,
    hour: int,
    role: str,
    soc0: float,
    hold_soc_kwh: float,
    pv_q: list[float],
    load_q: list[float],
    reserve_q: list[float],
    base_charge_q: list[float],
    rce_q: list[float | None] | None = None,
    hour_end_floor_kwh: float | None = None,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    min_hourly_kwh: float,
) -> BatteryGridExportHourClaim | None:
    """Pick span + per-quarter export for one hour from remaining SOC budget.

    Role still limits legal spans (first/middle/last/single) so multi-hour
    windows stay contiguous. Among legal spans, prefer max quarter-RCE
    revenue, then volume, then earlier start. Tries max DC power first; if
    SOC cannot fill a span, tries lower uniform power so Bat Discharge still
    meets *min_hourly_kwh*. Claim sim does not bank PV outside the Dis span.
    """
    prices = list(rce_q) if rce_q is not None else [None, None, None, None]
    while len(prices) < 4:
        prices.append(None)
    legal = set(export_span_candidates(role))
    best: BatteryGridExportHourClaim | None = None
    # revenue, volume_kwh, -start_q (earlier start wins ties)
    best_key: tuple[float, float, int] = (-1.0, -1.0, 1)
    common = dict(
        soc0=soc0, pv_q=pv_q, load_q=load_q, reserve_q=reserve_q,
        base_charge_q=base_charge_q, hold_soc_kwh=hold_soc_kwh,
        hour_end_floor_kwh=hour_end_floor_kwh,
        battery_cap=battery_cap, min_kwh=min_kwh,
        discharge_dc_step=discharge_dc_step, inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery, eps_step=eps_step,
    )

    def _consider(
        span: tuple[int, int],
        exports: list[float],
        bat_dis: float,
    ) -> None:
        nonlocal best, best_key
        vol = sum(exports)
        if vol <= eps_step:
            return
        if min_hourly_kwh > eps_step and bat_dis + eps_step < min_hourly_kwh:
            return
        rev = _export_span_quarter_revenue(exports, prices, eps=eps_step)
        key = (rev, vol, -int(span[0]))
        if key > best_key:
            best_key = key
            best = BatteryGridExportHourClaim(
                hour=hour, span=span,
                export_q=(exports[0], exports[1], exports[2], exports[3]),
                bat_discharge_kwh=bat_dis,
            )

    for span in export_span_candidates(role):
        # 1) Max power, then trim empty quarters to a legal sub-span.
        exports, bat_dis = _sim_hour_battery_grid_export_at_cap(
            span=span, dc_cap_per_q=discharge_dc_step, **common,
        )
        trimmed = _trim_span_to_active_battery_grid_exports(role, span, exports, eps=eps_step)
        if trimmed is not None and trimmed in legal:
            if trimmed != span:
                exports, bat_dis = _sim_hour_battery_grid_export_at_cap(
                    span=trimmed, dc_cap_per_q=discharge_dc_step, **common,
                )
            _consider(trimmed, exports, bat_dis)

        # 2) Uniform reduced DC power across the full candidate span.
        if span[1] - span[0] < 1:
            continue
        for level in range(19, 0, -1):
            cap = discharge_dc_step * level / 20.0
            if cap <= eps_step:
                break
            exports, bat_dis = _sim_hour_battery_grid_export_at_cap(
                span=span, dc_cap_per_q=cap, **common,
            )
            if any(exports[q] <= eps_step for q in range(span[0], span[1])):
                continue
            _consider(span, exports, bat_dis)
            break

    return best


def plan_battery_grid_export(
    base_controls: list[HourControl],
    *,
    steps: int,
    pv_series: list[float],
    load_series: list[float],
    rce_series: list[float | None],
    rce_step_offset: int,
    step_scale: float,
    initial_soc_kwh: float,
    battery_cap: float,
    min_kwh: float,
    discharge_dc_step: float,
    inverter_ac_step: float,
    eta_grid: float,
    eta_out: float,
    eta_pv_load: float,
    eta_pv_grid: float,
    eta_pv_battery: float,
    eps_step: float,
    reserves: list[float],
    export_floor: float,
    min_hourly_kwh: float,
    export_window_start_hour: int = 16,
    skip_export_hours: set[int] | None = None,
    forecast: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
    today_date=None,
) -> list[HourControl]:
    """Plan battery→grid export from the config start hour until morning PV cover.

    Eligible hours are clock start–23 plus overnight until PV covers again, with
    hourly avg-RCE (0.01) ≥ *export_floor*. Cover and leftover use the full
    available PV/load and G12 peak/offpeak, not the optimizer hour slice.
    Each start-hour→morning window is filled in clock order so a richer next
    evening cannot skip tonight. Seed the richest unrounded avg, then grow by
    5-groszy rating (ties: closer to the run). A failed ≥-threshold edge closes
    that side (do not seed a weaker island past it). Chrono fill sells leftover
    down to survive-after-that-hour, so the right edge opens when the window
    end moves.
    """
    if steps <= 0:
        return list(base_controls)
    slots = slots_per_hour_from_scale(step_scale)
    hours = sorted({
        (rce_step_offset + i) // slots for i in range(steps)
    })
    cover_bound = None
    if cfg is not None and today_date is not None:
        tariff = g12_tariff_from_cfg(cfg)
        cover_bound = morning_cover_bound_from_hour_buys(
            offpeak_buy=tariff.offpeak_full, epsilon=eps_step,
            cfg=cfg, today_date=today_date,
        )
    window = evening_export_window_hours(
        list(hours),
        pv_series=pv_series,
        load_series=load_series,
        rce_step_offset=rce_step_offset,
        slots=slots,
        steps=steps,
        eta_pv_load=eta_pv_load,
        epsilon=eps_step,
        export_window_start_hour=export_window_start_hour,
        forecast=forecast,
        cover_bound=cover_bound,
        cfg=cfg,
        today_date=today_date,
    )
    ratings: dict[int, float] = {}
    raw_avgs: dict[int, float] = {}
    grow_ratings: dict[int, float] = {}
    skip_export = {int(h) for h in (skip_export_hours or ())}
    for h in hours:
        if int(h) not in window:
            continue
        if int(h) in skip_export:
            continue
        avg = hourly_avg_rce(rce_series, h, slots_per_hour=slots)
        if avg is None:
            continue
        rating = hour_rce_rating(rce_series, h, slots_per_hour=slots)
        if rating is None or rating + eps_step < export_floor:
            continue
        raw_avgs[int(h)] = float(avg)
        ratings[int(h)] = float(rating)
        grow_ratings[int(h)] = round_rce_5_groszy(avg)
    if not ratings:
        return [
            HourControl(c.grid_charge_kw, 0.0, c.load_from_grid) for c in base_controls
        ]

    def _hour_inputs(hour: int, soc_starts: list[float]) -> tuple[
        float, list[float], list[float], list[float], list[float],
        list[float | None], float | None,
    ] | None:
        idxs = _hour_steps_in_horizon(
            hour=hour, steps=steps, rce_step_offset=rce_step_offset,
            slots_per_hour=slots,
        )
        if not idxs:
            return None
        soc0 = float(soc_starts[idxs[0]])
        pv_q = [0.0, 0.0, 0.0, 0.0]
        load_q = [0.0, 0.0, 0.0, 0.0]
        reserve_q = [min_kwh, min_kwh, min_kwh, min_kwh]
        charge_q = [0.0, 0.0, 0.0, 0.0]
        rce_q: list[float | None] = [None, None, None, None]
        for step in idxs:
            global_step = rce_step_offset + step
            q = global_step % slots
            if 0 <= q < 4:
                pv_q[q] = float(pv_series[step])
                load_q[q] = float(load_series[step])
                reserve_q[q] = float(reserves[step])
                base = base_controls[step] if step < len(base_controls) else HourControl(0.0, 0.0)
                charge_q[q] = float(base.grid_charge_kw)
                if 0 <= global_step < len(rce_series):
                    raw = rce_series[global_step]
                    rce_q[q] = float(raw) if raw is not None else None
        next_idxs = _hour_steps_in_horizon(
            hour=hour + 1, steps=steps, rce_step_offset=rce_step_offset,
            slots_per_hour=slots,
        )
        hour_end_floor = float(reserves[next_idxs[0]]) if next_idxs else None
        if hour_end_floor is not None:
            reserve_q = [min(r, hour_end_floor) for r in reserve_q]
        return soc0, pv_q, load_q, reserve_q, charge_q, rce_q, hour_end_floor

    common = dict(
        steps=steps,
        pv_series=pv_series,
        load_series=load_series,
        rce_step_offset=rce_step_offset,
        step_scale=step_scale,
        initial_soc_kwh=initial_soc_kwh,
        battery_cap=battery_cap,
        min_kwh=min_kwh,
        discharge_dc_step=discharge_dc_step,
        inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid,
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid,
        eta_pv_battery=eta_pv_battery,
        eps_step=eps_step,
        reserves=reserves,
    )
    claim_kw = dict(
        battery_cap=battery_cap, min_kwh=min_kwh,
        discharge_dc_step=discharge_dc_step, inverter_ac_step=inverter_ac_step,
        eta_grid=eta_grid, eta_out=eta_out, eta_pv_load=eta_pv_load,
        eta_pv_grid=eta_pv_grid, eta_pv_battery=eta_pv_battery,
        eps_step=eps_step, min_hourly_kwh=min_hourly_kwh,
    )

    # Fill each start-hour→morning window in clock order. Seed the richest
    # unrounded avg, then grow by 5-groszy rating.
    selected: set[int] = set()
    draft: dict[int, BatteryGridExportHourClaim] = {}
    for window_hours in sale_windows(
        list(ratings.keys()),
        export_window_start_hour=export_window_start_hour,
        cover_bound=cover_bound,
    ):
        remaining = list(window_hours)
        last_assigned: int | None = None
        window_selected: set[int] = set()
        while remaining:
            h = pick_next_export_hour(
                remaining,
                grow_ratings,
                selected=window_selected,
                last_hour=last_assigned,
                seed_ratings=raw_avgs,
                gap_ratings=ratings,
                export_window_start_hour=export_window_start_hour,
                cover_bound=cover_bound,
            )
            if _export_seed_jumps_rated_gap(
                h, window_selected, ratings,
                export_window_start_hour=export_window_start_hour,
                cover_bound=cover_bound,
            ):
                remaining = [x for x in remaining if x != h]
                continue
            remaining = [x for x in remaining if x != h]
            trial = window_selected | {h}
            roles = export_window_roles(trial)
            _, soc_starts = _apply_battery_grid_export_claims_chrono(
                base_controls, draft, **common,
            )
            inputs = _hour_inputs(h, soc_starts)
            if inputs is None:
                remaining = trim_remaining_after_failed_export_edge(
                    remaining, selected=window_selected, failed_hour=h,
                    export_window_start_hour=export_window_start_hour,
                    cover_bound=cover_bound,
                )
                continue
            soc0, pv_q, load_q, reserve_q, charge_q, rce_q, hour_end_floor = inputs
            hold = hold_soc_for_later_battery_grid_export_claims(
                draft, from_hour=h, eta_out=eta_out, ratings=ratings,
                export_window_start_hour=export_window_start_hour,
                cover_bound=cover_bound,
            )
            claim = plan_hour_battery_grid_export_claim(
                hour=h, role=roles[h], soc0=soc0, hold_soc_kwh=hold,
                pv_q=pv_q, load_q=load_q, reserve_q=reserve_q, base_charge_q=charge_q,
                rce_q=rce_q, hour_end_floor_kwh=hour_end_floor,
                **claim_kw,
            )
            if claim is None or claim.export_ac_kwh <= eps_step:
                remaining = trim_remaining_after_failed_export_edge(
                    remaining, selected=window_selected, failed_hour=h,
                    export_window_start_hour=export_window_start_hour,
                    cover_bound=cover_bound,
                )
                continue
            draft[h] = claim
            window_selected = trial
            selected = selected | trial
            last_assigned = h

    if not selected:
        return [
            HourControl(c.grid_charge_kw, 0.0, c.load_from_grid) for c in base_controls
        ]

    def _chrono_fill(
        hours: set[int],
        hold_from: dict[int, BatteryGridExportHourClaim],
    ) -> dict[int, BatteryGridExportHourClaim]:
        roles = export_window_roles(hours)
        filled: dict[int, BatteryGridExportHourClaim] = {}
        for hour in sorted(hours):
            _, soc_starts = _apply_battery_grid_export_claims_chrono(
                base_controls, filled, **common,
            )
            inputs = _hour_inputs(hour, soc_starts)
            if inputs is None:
                continue
            soc0, pv_q, load_q, reserve_q, charge_q, rce_q, hour_end_floor = inputs
            claim = plan_hour_battery_grid_export_claim(
                hour=hour, role=roles.get(hour, "single"), soc0=soc0,
                hold_soc_kwh=hold_soc_for_later_battery_grid_export_claims(
                    hold_from, from_hour=hour, eta_out=eta_out, ratings=ratings,
                    export_window_start_hour=export_window_start_hour,
                    cover_bound=cover_bound,
                ),
                pv_q=pv_q, load_q=load_q, reserve_q=reserve_q, base_charge_q=charge_q,
                rce_q=rce_q, hour_end_floor_kwh=hour_end_floor,
                **claim_kw,
            )
            if claim is not None and claim.export_ac_kwh > eps_step:
                filled[hour] = claim
        return filled

    # Chrono fill: each hour dumps down to survive-after-that-hour.
    claims = _chrono_fill(selected, draft)
    if claims:
        claims = _chrono_fill(set(claims), claims)

    controls, _ = _apply_battery_grid_export_claims_chrono(base_controls, claims, **common)
    return controls


assign_ranked_battery_export = plan_battery_grid_export
_plan_hour_export_claim = plan_hour_battery_grid_export_claim


def optimization_battery_export_value(
    rce: float | None,
    tariff: G12Tariff,
    cfg: dict | None = None,
) -> float:
    """Marginal bill benefit of battery export vs self-use at offpeak buy."""
    if rce is None:
        return 0.0
    rce_f = float(rce)
    floor = battery_export_break_even_rce(tariff, cfg)
    if rce_f < floor:
        return 0.0
    return rce_f - floor


def export_credit_price(
    rce: float | None,
    tariff: G12Tariff,
    *,
    from_battery: bool,
    cfg: dict | None = None,
) -> float:
    if rce is None:
        return 0.0
    if from_battery and float(rce) < battery_export_break_even_rce(tariff, cfg):
        return 0.0
    return float(rce)

def max_battery_export_kwh(
    soc_kwh: float,
    pv: float,
    load: float,
    *,
    min_kwh: float,
    ac_cap_kw: float,
    eta_out: float,
    eta_pv_load: float,
    reserve_soc_kwh: float,
    epsilon: float,
    discharge_dc_cap_kwh: float | None = None,
) -> float:
    """Max meter kWh exportable from battery after load, respecting reserve floor."""
    export_headroom = max(0.0, ac_cap_kw - load)
    if export_headroom <= epsilon or eta_out <= 0:
        return 0.0
    soc = soc_kwh
    deficit, _ = pv_load_energy_split(pv, load, eta_pv_load=eta_pv_load)
    dc_used = 0.0
    if deficit > epsilon and eta_out > 0:
        withdraw = min(deficit / eta_out, max(0.0, soc - min_kwh))
        if discharge_dc_cap_kwh is not None:
            withdraw = min(withdraw, max(0.0, float(discharge_dc_cap_kwh) - dc_used))
        soc -= withdraw
        dc_used += withdraw
    exportable_soc = max(0.0, soc - max(min_kwh, reserve_soc_kwh))
    dc_room = (
        max(0.0, float(discharge_dc_cap_kwh) - dc_used)
        if discharge_dc_cap_kwh is not None
        else exportable_soc
    )
    return min(exportable_soc * eta_out, export_headroom, dc_room * eta_out)

