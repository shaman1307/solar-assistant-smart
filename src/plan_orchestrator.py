"""Horizon optimizer dispatch, EA row assembly, and chart SOC series.

``build_energy_arbitrage_plan`` in simulation.py loads inputs and returns the
JSON payload; this module runs the committed-hour optimizer, builds hour rows,
and produces the as-if-midnight SOC chart.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .plan_hourly_actuals import (
    build_blended_current_hour_q15,
    build_h0_carryover_row,
    replay_forward_soc_on_rows,
    sync_blended_current_hour_row,
)
from .plan_q15 import (
    build_smart_plan_hour_row,
    run_day_smart_q15_plan,
    run_rolling_smart_q15_plan,
    run_today_smart_q15_plan,
)
from .plan_timer_override import apply_plan_timer_overrides_if_any
from .timer_plan import quarter_start_minute, sa_discharge_timer_for_hour

Q15_PER_HOUR = 4


def hourly_forecast_kwh(
    dt: datetime,
    today_date,
    pv_today: list[float],
    pv_tomorrow: list[float],
    load_today: list[float],
    load_tomorrow: list[float],
) -> tuple[float, float]:
    hour = dt.hour
    if dt.date() == today_date:
        pv_h = float(pv_today[hour]) if hour < len(pv_today) else 0.0
        load_h = float(load_today[hour]) if hour < len(load_today) else 0.0
    else:
        pv_h = float(pv_tomorrow[hour]) if hour < len(pv_tomorrow) else 0.0
        load_h = float(load_tomorrow[hour]) if hour < len(load_tomorrow) else 0.0
    return pv_h, load_h


def opt_slots_from_committed_q15(slots_now: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Optimizer-shaped slots from a locked EA q15 row (blend physics)."""
    opt_slots: list[dict[str, Any]] = []
    for s in slots_now:
        opt_slots.append({
            "quarter": int(s.get("quarter", 0)),
            "pv": float(s.get("production") or 0),
            "load": float(s.get("consumption") or 0),
            "grid_import": float(s.get("grid_import") or 0),
            "grid_export": float(s.get("grid_export") or 0),
            "battery_delta": float(s.get("battery") or 0),
            "soc_pct": float(s.get("soc") or 0),
            "grid_charge_kw": 0.0,
            "battery_export_kwh": float(s.get("grid_export") or 0),
        })
    return opt_slots


def run_horizon_smart_plans(
    *,
    today_str: str,
    tomorrow_str: str,
    pv_merged: list[float],
    load_merged: list[float],
    pv_tomorrow: list[float],
    load_tomorrow: list[float],
    cfg: dict,
    rce_today_full: list[float | None] | None,
    rce_tomorrow_full: list[float | None] | None,
    committed_hour: dict[str, Any] | None,
    committed_end_soc: float | None,
    plan_from_hour: int,
    hour_steps: int,
    need_tomorrow_hours: int,
    soc_kwh: float,
    day_start_soc: float,
    epsilon: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run the 15-min optimizer for today/tomorrow given the committed current hour."""
    smart_today: dict[str, Any] | None = None
    smart_tomorrow: dict[str, Any] | None = None

    if committed_hour is not None and committed_end_soc is not None and plan_from_hour >= 23:
        # Hour 23 committed — only tomorrow is re-planned from this end SOC.
        smart_today = {
            "q15_by_hour": {h: [] for h in range(24)},
            "q15_plan_rows": [],
            "end_soc_kwh": round(committed_end_soc, 3),
            "timer_schedule": {},
            "epsilon": epsilon,
        }
        if need_tomorrow_hours > 0:
            smart_tomorrow = run_day_smart_q15_plan(
                date_str=tomorrow_str,
                pv_hourly=[float(v) for v in pv_tomorrow],
                load_hourly=[float(v) for v in load_tomorrow],
                tomorrow_pv=[float(v) for v in pv_tomorrow],
                tomorrow_load=[float(v) for v in load_tomorrow],
                cfg=cfg,
                rce_quarters=rce_tomorrow_full,
                initial_soc_kwh=float(committed_end_soc),
                from_hour=0,
                front_load_skip_leading_slots=0,
            )
    elif committed_hour is not None and committed_end_soc is not None:
        opt_from = plan_from_hour + 1
        opt_horizon = max(0, hour_steps - 1)
        if need_tomorrow_hours > 0 and opt_horizon > 0:
            rolling = run_rolling_smart_q15_plan(
                date_str=today_str,
                pv_hourly=pv_merged,
                load_hourly=load_merged,
                tomorrow_pv=[float(v) for v in pv_tomorrow],
                tomorrow_load=[float(v) for v in load_tomorrow],
                cfg=cfg,
                rce_quarters=rce_today_full,
                rce_quarters_tomorrow=rce_tomorrow_full,
                initial_soc_kwh=float(committed_end_soc),
                from_hour=opt_from,
                horizon_hours=opt_horizon,
                front_load_skip_leading_slots=0,
            )
            smart_today = (rolling or {}).get("today")
            smart_tomorrow = (rolling or {}).get("tomorrow")
        elif opt_horizon > 0:
            smart_today = run_day_smart_q15_plan(
                date_str=today_str,
                pv_hourly=pv_merged,
                load_hourly=load_merged,
                tomorrow_pv=pv_tomorrow,
                tomorrow_load=load_tomorrow,
                cfg=cfg,
                rce_quarters=rce_today_full,
                initial_soc_kwh=float(committed_end_soc),
                from_hour=opt_from,
                front_load_skip_leading_slots=0,
            )
    elif need_tomorrow_hours > 0:
        rolling = run_rolling_smart_q15_plan(
            date_str=today_str,
            pv_hourly=pv_merged,
            load_hourly=load_merged,
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            rce_quarters=rce_today_full,
            rce_quarters_tomorrow=rce_tomorrow_full,
            initial_soc_kwh=soc_kwh,
            from_hour=plan_from_hour,
            horizon_hours=hour_steps,
        )
        smart_today = (rolling or {}).get("today")
        smart_tomorrow = (rolling or {}).get("tomorrow")
    else:
        smart_today = run_today_smart_q15_plan(
            date_str=today_str,
            pv_hourly=pv_merged,
            load_hourly=load_merged,
            tomorrow_pv=pv_tomorrow,
            tomorrow_load=load_tomorrow,
            cfg=cfg,
            rce_quarters=rce_today_full,
            plan_from_hour=plan_from_hour,
            day_start_soc_kwh=day_start_soc,
            live_soc_kwh=soc_kwh,
        )
    return smart_today, smart_tomorrow


def assemble_ea_plan_rows(
    *,
    smart_today: dict[str, Any] | None,
    smart_tomorrow: dict[str, Any] | None,
    committed_hour: dict[str, Any] | None,
    committed_end_soc: float | None,
    plan_from_hour: int,
    hour_steps: int,
    today_str: str,
    tomorrow_str: str,
    today_date,
    now: datetime,
    pv_merged: list[float],
    load_merged: list[float],
    pv_tomorrow: list[float],
    load_tomorrow: list[float],
    forecast_pv_q15,
    forecast_load_q15,
    series_10min,
    live_metrics: dict[str, Any],
    quarters_by_date: dict[str, list],
    cfg: dict,
    params: dict,
    rules: dict[str, Any],
    epsilon: float,
    battery_cap: float,
    plan_start_soc_kwh: float,
    initial_soc_pct: float,
    today_timer_ov: dict,
    tomorrow_timer_ov: dict,
    merged_pv_q15_today: list,
    merged_load_q15_today: list,
    merged_pv_q15_tomorrow: list,
    merged_load_q15_tomorrow: list,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Build rolling EA hour rows (today from plan_from_hour, then tomorrow)."""
    export_hours: set[int] = set()
    all_rows: list[dict] = []
    remaining = hour_steps
    blended_anchor_kwh: float | None = None
    blended_row_idx: int | None = None

    if smart_today or committed_hour is not None:
        for h in range(plan_from_hour, 24):
            if remaining <= 0:
                break
            if h == plan_from_hour and committed_hour is not None:
                row = copy.deepcopy(committed_hour)
                row["hour_labels_locked"] = True
                if h == plan_from_hour:
                    slots_now = list(row.get("q15") or [])
                    fpv_h = float(pv_merged[h]) if h < len(pv_merged) else 0.0
                    flo_h = float(load_merged[h]) if h < len(load_merged) else 0.0
                    sa_timer = str(committed_hour.get("timer_schedule") or "").strip()
                    if not sa_timer:
                        sa_timer = sa_discharge_timer_for_hour(rules, h, cfg=cfg) or ""
                    opt_slots = opt_slots_from_committed_q15(slots_now)
                    blended_q15 = build_blended_current_hour_q15(
                        h,
                        now,
                        forecast_pv_q15=forecast_pv_q15,
                        forecast_load_q15=forecast_load_q15,
                        series_10min=series_10min,
                        soc_start_kwh=plan_start_soc_kwh,
                        opt_slots=opt_slots,
                        cfg=cfg,
                        pv_hourly=fpv_h,
                        load_hourly=flo_h,
                        sa_timer_txt=sa_timer or None,
                    )
                    pv_blend = round(sum(float(s.get("production") or 0) for s in blended_q15), 3)
                    load_blend = round(sum(float(s.get("consumption") or 0) for s in blended_q15), 3)
                    soc_blend = float(blended_q15[-1].get("soc") or initial_soc_pct)
                    sync_blended_current_hour_row(
                        row,
                        blended_q15,
                        production=pv_blend,
                        consumption=load_blend,
                        soc=soc_blend,
                        cfg=cfg,
                        epsilon=epsilon,
                        hour=h,
                        opt_slots=opt_slots,
                        sa_timer_txt=sa_timer or None,
                        now=now,
                    )
                    row["timer_schedule"] = committed_hour.get("timer_schedule", "")
                    row["action"] = committed_hour.get("action", "")
                    row["hour_labels_locked"] = True
                    has_timer = bool(str(committed_hour.get("timer_schedule") or "").strip())
                    blended_anchor_kwh = (
                        committed_end_soc
                        if has_timer and committed_end_soc is not None
                        else (soc_blend / 100.0) * battery_cap
                    )
                    blended_row_idx = len(all_rows)
                all_rows.append(row)
                if row.get("export_planned"):
                    export_hours.add(h)
                remaining -= 1
                continue

            slots = (smart_today or {}).get("q15_by_hour", {}).get(h) or []
            if not slots:
                if h == 0 and plan_from_hour == 0:
                    prev_day_hourly = live_metrics.get("prev_day_hourly")
                    if prev_day_hourly:
                        dt0 = datetime.strptime(today_str, "%Y-%m-%d").replace(hour=0)
                        disp_pv, disp_load = hourly_forecast_kwh(
                            dt0, today_date, pv_merged, pv_tomorrow, load_merged, load_tomorrow,
                        )
                        q0 = quarters_by_date.get(today_str) or []
                        rce_vals = [float(v) for v in q0[0:4] if v is not None]
                        rce_h0 = round(sum(rce_vals) / len(rce_vals), 4) if rce_vals else None
                        carry = build_h0_carryover_row(
                            today_str,
                            prev_day_hourly,
                            forecast_pv=disp_pv,
                            forecast_load=disp_load,
                            cfg=cfg,
                            params=params,
                            rce_price=rce_h0,
                        )
                        if carry:
                            all_rows.append(carry)
                            remaining -= 1
                continue
            dt = datetime.strptime(today_str, "%Y-%m-%d").replace(hour=h)
            disp_pv, disp_load = hourly_forecast_kwh(
                dt, today_date, pv_merged, pv_tomorrow, load_merged, load_tomorrow,
            )
            row = build_smart_plan_hour_row(
                dt,
                slots,
                cfg=cfg,
                epsilon=epsilon,
                display_pv=disp_pv,
                display_load=disp_load,
                manual_timer_schedule=(
                    today_timer_ov[h] if h in today_timer_ov else None
                ),
                not_before_min=(
                    quarter_start_minute(now) if h == plan_from_hour else None
                ),
            )
            if h == plan_from_hour:
                slots_now = (smart_today or {}).get("q15_by_hour", {}).get(h) or []
                fpv_h = float(pv_merged[h]) if h < len(pv_merged) else 0.0
                flo_h = float(load_merged[h]) if h < len(load_merged) else 0.0
                sa_timer = sa_discharge_timer_for_hour(rules, h, cfg=cfg)
                blended_q15 = build_blended_current_hour_q15(
                    h,
                    now,
                    forecast_pv_q15=forecast_pv_q15,
                    forecast_load_q15=forecast_load_q15,
                    series_10min=series_10min,
                    soc_start_kwh=plan_start_soc_kwh,
                    opt_slots=slots_now,
                    cfg=cfg,
                    pv_hourly=fpv_h,
                    load_hourly=flo_h,
                    sa_timer_txt=sa_timer or None,
                )
                pv_blend = round(sum(float(s.get("production") or 0) for s in blended_q15), 3)
                load_blend = round(sum(float(s.get("consumption") or 0) for s in blended_q15), 3)
                soc_blend = float(blended_q15[-1].get("soc") or initial_soc_pct)
                sync_blended_current_hour_row(
                    row,
                    blended_q15,
                    production=pv_blend,
                    consumption=load_blend,
                    soc=soc_blend,
                    cfg=cfg,
                    epsilon=epsilon,
                    hour=h,
                    opt_slots=slots_now,
                    sa_timer_txt=sa_timer or None,
                    now=now,
                )
                blended_anchor_kwh = (soc_blend / 100.0) * battery_cap
                blended_row_idx = len(all_rows)
            all_rows.append(row)
            if row.get("export_planned"):
                export_hours.add(h)
            remaining -= 1

    if smart_tomorrow and remaining > 0:
        for h in range(24):
            if remaining <= 0:
                break
            slots = smart_tomorrow["q15_by_hour"].get(h) or []
            if not slots:
                continue
            dt = datetime.strptime(tomorrow_str, "%Y-%m-%d").replace(hour=h)
            disp_pv, disp_load = hourly_forecast_kwh(
                dt, today_date, pv_merged, pv_tomorrow, load_merged, load_tomorrow,
            )
            row = build_smart_plan_hour_row(
                dt,
                slots,
                cfg=cfg,
                epsilon=epsilon,
                display_pv=disp_pv,
                display_load=disp_load,
                manual_timer_schedule=(
                    tomorrow_timer_ov[h] if h in tomorrow_timer_ov else None
                ),
            )
            all_rows.append(row)
            if row.get("export_planned"):
                export_hours.add(h)
            remaining -= 1

    if blended_anchor_kwh is not None and blended_row_idx is not None:
        replay_forward_soc_on_rows(
            all_rows[blended_row_idx + 1:],
            anchor_soc_kwh=blended_anchor_kwh,
            q15_plan_by_date={
                today_str: (smart_today or {}).get("q15_by_hour") or {},
                tomorrow_str: (smart_tomorrow or {}).get("q15_by_hour") or {},
            },
            pv_q15_by_date={
                today_str: merged_pv_q15_today,
                tomorrow_str: merged_pv_q15_tomorrow,
            },
            load_q15_by_date={
                today_str: merged_load_q15_today,
                tomorrow_str: merged_load_q15_tomorrow,
            },
            cfg=cfg,
        )
    return all_rows, export_hours


def soc_q15_from_q15_by_hour(plan: dict[str, Any] | None) -> list[float | None]:
    """Full-day q15 SOC from the optimizer (today chart plan line)."""
    out: list[float | None] = [None] * 96
    if not plan:
        return out
    q15_by_hour = plan.get("q15_by_hour") or {}
    for h in range(24):
        slots = q15_by_hour.get(h) or []
        for slot in slots:
            q = int(slot.get("quarter", 0))
            if not (0 <= q < 4):
                continue
            idx = h * 4 + q
            v = slot.get("soc_pct")
            out[idx] = round(float(v), 1) if v is not None else None
    return out


def tomorrow_chart_soc_seed_kwh(
    all_rows: list[dict[str, Any]],
    today_str: str,
    smart_today: dict[str, Any] | None,
    battery_cap: float,
    day_start_soc: float,
) -> float:
    """EA end-of-today SOC (kWh) to seed tomorrow's as-if-midnight chart."""
    tom_seed_kwh: float | None = None
    for r in reversed(all_rows):
        if str(r.get("plan_date") or "") != today_str:
            continue
        soc_pct = None
        q15 = r.get("q15") or []
        if q15 and q15[-1].get("soc") is not None:
            soc_pct = float(q15[-1]["soc"])
        elif r.get("soc") is not None:
            soc_pct = float(r["soc"])
        if soc_pct is None:
            continue
        pct = max(0.0, min(100.0, soc_pct))
        tom_seed_kwh = (pct / 100.0) * battery_cap
        break
    if tom_seed_kwh is None and smart_today and smart_today.get("end_soc_kwh") is not None:
        tom_seed_kwh = float(smart_today["end_soc_kwh"])
    if tom_seed_kwh is None:
        tom_seed_kwh = float(day_start_soc)
    return max(0.0, min(battery_cap, float(tom_seed_kwh)))


def build_chart_plan_soc_q15(
    *,
    today_str: str,
    tomorrow_str: str,
    pv_forecast_today: list[float],
    load_forecast_today: list[float],
    pv_tomorrow: list[float],
    load_tomorrow: list[float],
    cfg: dict,
    rce_today: list,
    rce_tomorrow_full: list | None,
    day_start_soc: float,
    today_timer_ov: dict,
    tomorrow_timer_ov: dict,
    all_rows: list[dict[str, Any]],
    smart_today: dict[str, Any] | None,
    battery_cap: float,
) -> tuple[list[float | None], list[float | None]]:
    """Chart SOC: today as-if 00:00; tomorrow seeded from EA end-of-today."""
    day_plan_for_soc = run_day_smart_q15_plan(
        date_str=today_str,
        pv_hourly=pv_forecast_today,
        load_hourly=load_forecast_today,
        tomorrow_pv=pv_tomorrow,
        tomorrow_load=load_tomorrow,
        cfg=cfg,
        rce_quarters=rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None,
        initial_soc_kwh=day_start_soc,
        from_hour=0,
    )
    if today_timer_ov:
        day_plan_for_soc = apply_plan_timer_overrides_if_any(
            day_plan_for_soc,
            date_str=today_str,
            pv_hourly=pv_forecast_today,
            load_hourly=load_forecast_today,
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            from_hour=0,
            rce_quarters=rce_today if len(rce_today) >= Q15_PER_HOUR * 24 else None,
            gap_mode="idle",
        )
    today_plan_soc = soc_q15_from_q15_by_hour(day_plan_for_soc)

    tom_seed_kwh = tomorrow_chart_soc_seed_kwh(
        all_rows, today_str, smart_today, battery_cap, day_start_soc,
    )
    day_plan_tomorrow_for_soc = run_day_smart_q15_plan(
        date_str=tomorrow_str,
        pv_hourly=[float(v) for v in pv_tomorrow],
        load_hourly=[float(v) for v in load_tomorrow],
        tomorrow_pv=[float(v) for v in pv_tomorrow],
        tomorrow_load=[float(v) for v in load_tomorrow],
        cfg=cfg,
        rce_quarters=rce_tomorrow_full,
        initial_soc_kwh=tom_seed_kwh,
        from_hour=0,
    )
    if day_plan_tomorrow_for_soc and tomorrow_timer_ov:
        day_plan_tomorrow_for_soc = apply_plan_timer_overrides_if_any(
            day_plan_tomorrow_for_soc,
            date_str=tomorrow_str,
            pv_hourly=[float(v) for v in pv_tomorrow],
            load_hourly=[float(v) for v in load_tomorrow],
            tomorrow_pv=[float(v) for v in pv_tomorrow],
            tomorrow_load=[float(v) for v in load_tomorrow],
            cfg=cfg,
            from_hour=0,
            rce_quarters=rce_tomorrow_full,
            gap_mode="idle",
        )
    tomorrow_plan_soc = soc_q15_from_q15_by_hour(day_plan_tomorrow_for_soc)
    return today_plan_soc, tomorrow_plan_soc
