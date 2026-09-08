"""Manual Timer Schedule overrides on Energy arbitrage rolling plan."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .plan_optimizer import (
    HourControl,
    eps_step_kwh,
    optimize_horizon,
    reserve_soc_per_step,
    simulate_hour,
)
from .simulation_config import (
    get_simulation_params,
    plan_min_soc_kwh,
    plan_reserve_min_soc_kwh,
    plan_timer_discharge_power_kw,
)
from .timer_plan import (
    _hhmm_to_minute_of_day,
    classify_action as timer_classify_action,
    derive_timer_schedule_q15,
    parse_timer_schedule_segments,
)

Q15_PER_HOUR = 4
STEP_SCALE = 1.0 / Q15_PER_HOUR


def get_timer_overrides_for_date(cfg: dict, date_str: str) -> dict[int, str]:
    """Hour -> timer cell text; key present means manual (empty str = no timer)."""
    raw = (cfg.get("plan_overrides") or {}).get("timer_schedule") or {}
    day = raw.get(date_str) or {}
    return {int(h): str(v) for h, v in day.items()}


def is_timer_schedule_hour_editable(
    plan_date: str,
    hour: int,
    *,
    today_date: str,
    plan_from_hour: int,
) -> bool:
    """True for strictly future plan rows (tomorrow+, or today hour > plan_from_hour)."""
    hour = int(hour)
    if plan_date < today_date:
        return False
    if plan_date > today_date:
        return True
    return hour > int(plan_from_hour)


def set_timer_schedule_override(
    cfg: dict,
    date_str: str,
    hour: int,
    timer_schedule: str | None,
    *,
    clear_later_hours: bool = True,
) -> dict[int, str]:
    """Persist override and drop later hours on the same date (stale after replay)."""
    hour = max(0, min(23, int(hour)))
    po = cfg.setdefault("plan_overrides", {})
    by_date = po.setdefault("timer_schedule", {})
    day = by_date.setdefault(date_str, {})

    if clear_later_hours:
        for h in list(day.keys()):
            if int(h) > hour:
                del day[h]

    if timer_schedule is None:
        day.pop(str(hour), None)
    else:
        day[str(hour)] = str(timer_schedule)

    if not day:
        by_date.pop(date_str, None)
    if not by_date:
        po.pop("timer_schedule", None)
    if not po:
        cfg.pop("plan_overrides", None)

    return get_timer_overrides_for_date(cfg, date_str)


def timer_hour_has_grid_charge(timer_txt: str, hour: int) -> bool:
    """True when any grid-charge segment overlaps this clock hour."""
    if not str(timer_txt or "").strip():
        return False
    hour_start = int(hour) * 60
    hour_end = hour_start + 60
    for seg in parse_timer_schedule_segments(timer_txt):
        if seg.get("kind") != "chg":
            continue
        from_min = _hhmm_to_minute_of_day(seg["from"])
        to_min = _hhmm_to_minute_of_day(seg["to"])
        if from_min is None or to_min is None:
            continue
        if from_min < hour_end and to_min > hour_start:
            return True
    return False


def hour_control_from_timer_override(
    hour: int,
    quarter: int,
    timer_txt: str,
    *,
    step_scale: float = STEP_SCALE,
) -> HourControl:
    """Map one Timer Schedule cell to a 15-min optimizer control."""
    if not str(timer_txt or "").strip():
        return HourControl(0.0, 0.0)

    slot_start = hour * 60 + quarter * 15
    slot_end = slot_start + 15
    grid_charge_kw = 0.0
    battery_export_kwh = 0.0
    # House load stays on battery (load priority); grid import is charge only.
    load_from_grid = False

    for seg in parse_timer_schedule_segments(timer_txt):
        from_min = _hhmm_to_minute_of_day(seg["from"])
        to_min = _hhmm_to_minute_of_day(seg["to"])
        if from_min is None or to_min is None:
            continue
        if slot_start >= to_min or slot_end <= from_min:
            continue
        power = float(seg["power_kw"])
        if seg["kind"] == "chg":
            grid_charge_kw = max(grid_charge_kw, power * step_scale)
        elif seg["kind"] == "dis":
            battery_export_kwh = max(battery_export_kwh, power * step_scale)

    return HourControl(grid_charge_kw, battery_export_kwh, load_from_grid)


def _soc_at_hour_start(plan: dict[str, Any], hour: int) -> float | None:
    q15 = plan.get("q15_by_hour") or {}
    if hour > 0:
        prev = q15.get(hour - 1) or []
        if prev:
            return float(prev[-1].get("soc_end") or 0.0)
    for h in range(hour - 1, -1, -1):
        slots = q15.get(h) or []
        if slots:
            return float(slots[-1].get("soc_end") or 0.0)
    return None


def _append_q15_slot(
    *,
    q15_by_hour: dict[int, list[dict[str, Any]]],
    q15_plan_rows: list[dict[str, Any]],
    start_dt: datetime,
    h: int,
    quarter: int,
    slot: dict[str, Any],
    battery_cap: float | None = None,
) -> None:
    q15_by_hour.setdefault(h, []).append(slot)
    reserve_kwh = slot.get("reserve_kwh")
    reserve_soc_pct = slot.get("reserve_soc_pct")
    if reserve_soc_pct is None and reserve_kwh is not None and battery_cap:
        reserve_soc_pct = (float(reserve_kwh) / float(battery_cap)) * 100.0
    row: dict[str, Any] = {
        "start": (start_dt + timedelta(hours=h, minutes=quarter * 15)).strftime("%d-%m-%Y %H:%M"),
        "hour": h,
        "action": slot.get("action"),
        "soc": slot.get("soc_pct"),
        "grid_import": round(float(slot.get("grid_import") or 0), 4),
        "grid_export": round(float(slot.get("grid_export") or 0), 4),
        "battery": round(float(slot.get("battery_delta") or 0), 4),
    }
    if reserve_kwh is not None:
        row["reserve_kwh"] = round(float(reserve_kwh), 4)
    if reserve_soc_pct is not None:
        row["reserve_soc_pct"] = round(float(reserve_soc_pct), 2)
    q15_plan_rows.append(row)


def replay_day_plan_with_timer_overrides(
    plan: dict[str, Any],
    overrides: dict[int, str],
    *,
    date_str: str,
    pv_q: list[float],
    load_q: list[float],
    buy_q: list[float],
    rce_q: list[float | None],
    cfg: dict,
    from_hour: int,
    initial_soc_kwh: float | None = None,
    tomorrow_pv: list[float] | None = None,
    tomorrow_load: list[float] | None = None,
    forecast: dict[str, Any] | None = None,
    gap_mode: str = "optimize",
) -> dict[str, Any]:
    """Replay *from_hour* onward applying manual timer cells.

    *gap_mode*:
      - ``optimize`` — re-run DP on hours without a manual cell (EA rolling plan)
      - ``idle`` — HourControl(0,0): PV→battery / load from battery only on hours
        without a manual cell (solid SOC curve after a past-hour Chg patch)
    """
    if not overrides:
        return plan

    replay_from = max(from_hour, min(overrides.keys()))
    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    discharge_dc_kw = plan_timer_discharge_power_kw(cfg)
    inverter_ac_kw = float(cfg["inverter"]["ac_capacity_kw"])
    epsilon = float(params["epsilon_kwh"])
    eps_q = eps_step_kwh(epsilon, STEP_SCALE)
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])
    idle_gaps = str(gap_mode or "optimize").strip().lower() == "idle"

    start_dt = datetime.strptime(date_str, "%Y-%m-%d")
    today_date = start_dt.date()
    end_dt = start_dt.replace(hour=23, minute=45)

    soc = initial_soc_kwh
    if soc is None:
        soc = _soc_at_hour_start(plan, replay_from)
    if soc is None:
        soc = min_kwh

    q15_by_hour: dict[int, list[dict[str, Any]]] = {
        h: list((plan.get("q15_by_hour") or {}).get(h) or [])
        for h in range(replay_from)
    }
    q15_plan_rows: list[dict[str, Any]] = [
        r for r in (plan.get("q15_plan_rows") or [])
        if int(r.get("hour", 0)) < replay_from
    ]

    start_step = replay_from * Q15_PER_HOUR
    reserves = reserve_soc_per_step(
        len(pv_q) - start_step,
        pv_q[start_step:],
        load_q[start_step:],
        reserve_floor_kwh=plan_reserve_min_soc_kwh(cfg),
        eta_out=eta_out,
        eta_pv_load=eta_pv_load,
        epsilon=epsilon,
        step_scale=STEP_SCALE,
        end_dt=end_dt,
        today_date=today_date,
        forecast=forecast,
        global_step_offset=start_step,
        buy_prices=buy_q[start_step:],
        cfg=cfg,
    )

    h = replay_from
    global_step = start_step
    reserve_idx = 0

    while h < 24 and global_step < len(pv_q):
        if h in overrides:
            timer_txt = overrides[h]
            for q in range(Q15_PER_HOUR):
                if global_step >= len(pv_q):
                    break
                reserve = reserves[reserve_idx] if reserve_idx < len(reserves) else min_kwh
                ctrl = hour_control_from_timer_override(h, q, timer_txt)
                phys = simulate_hour(
                    soc, pv_q[global_step], load_q[global_step], ctrl,
                    battery_cap=battery_cap,
                    min_kwh=min_kwh,
                    ac_cap_kw=inverter_ac_kw * STEP_SCALE,
                    discharge_dc_cap_kwh=discharge_dc_kw * STEP_SCALE,
                    eta_grid=eta_grid,
                    eta_out=eta_out,
                    eta_pv_load=eta_pv_load,
                    eta_pv_grid=eta_pv_grid,
                    eta_pv_battery=eta_pv_battery,
                    epsilon=eps_q,
                    reserve_soc_kwh=reserve,
                )
                batt_exp = min(ctrl.battery_export_kwh, phys.grid_export)
                soc_pct = (phys.soc_end / battery_cap) * 100.0 if battery_cap else 0.0
                action = timer_classify_action(
                    bat_charge=max(0.0, phys.battery_delta),
                    bat_discharge=max(0.0, -phys.battery_delta),
                    grid_import=phys.grid_import,
                    grid_export=phys.grid_export,
                    production=pv_q[global_step],
                    epsilon=eps_q,
                )
                slot = {
                    "hour": h,
                    "quarter": q,
                    "action": action,
                    "pv": pv_q[global_step],
                    "load": load_q[global_step],
                    "grid_import": phys.grid_import,
                    "grid_export": phys.grid_export,
                    "battery_delta": phys.battery_delta,
                    "battery_export_kwh": batt_exp,
                    "grid_charge_kw": ctrl.grid_charge_kw,
                    "load_from_grid": ctrl.load_from_grid,
                    "ctrl_battery_export_kwh": ctrl.battery_export_kwh,
                    "soc_pct": soc_pct,
                    "soc_end": phys.soc_end,
                    "reserve_kwh": reserve,
                    "reserve_soc_pct": (
                        (float(reserve) / battery_cap) * 100.0 if battery_cap else None
                    ),
                    "rce": rce_q[global_step] if global_step < len(rce_q) else None,
                }
                _append_q15_slot(
                    q15_by_hour=q15_by_hour,
                    q15_plan_rows=q15_plan_rows,
                    start_dt=start_dt,
                    h=h,
                    quarter=q,
                    slot=slot,
                    battery_cap=battery_cap,
                )
                soc = phys.soc_end
                global_step += 1
                reserve_idx += 1
            h += 1
            continue

        next_override = min((oh for oh in overrides if oh > h), default=24)
        block_hours = next_override - h
        block_steps = block_hours * Q15_PER_HOUR
        slice_end = min(len(pv_q), global_step - start_step + block_steps)
        slice_len = slice_end - (global_step - start_step)
        if slice_len <= 0:
            break

        off = global_step - start_step
        if idle_gaps:
            block_controls = [HourControl(0.0, 0.0, False) for _ in range(slice_len)]
        else:
            block_controls = optimize_horizon(
                steps=slice_len,
                pv_series=pv_q[global_step:global_step + slice_len],
                load_series=load_q[global_step:global_step + slice_len],
                buy_prices=buy_q[global_step:global_step + slice_len],
                rce_series=rce_q,
                initial_soc_kwh=soc,
                cfg=cfg,
                params=params,
                end_dt=end_dt,
                today_date=today_date,
                rce_map={},
                forecast=forecast,
                step_scale=STEP_SCALE,
                rce_step_offset=global_step,
            )

        for ctrl in block_controls:
            if global_step >= len(pv_q):
                break
            hh = global_step // Q15_PER_HOUR
            qq = global_step % Q15_PER_HOUR
            reserve = reserves[reserve_idx] if reserve_idx < len(reserves) else min_kwh
            phys = simulate_hour(
                soc, pv_q[global_step], load_q[global_step], ctrl,
                battery_cap=battery_cap,
                min_kwh=min_kwh,
                ac_cap_kw=inverter_ac_kw * STEP_SCALE,
                discharge_dc_cap_kwh=discharge_dc_kw * STEP_SCALE,
                eta_grid=eta_grid,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                eta_pv_grid=eta_pv_grid,
                eta_pv_battery=eta_pv_battery,
                epsilon=eps_q,
                reserve_soc_kwh=reserve,
            )
            batt_exp = min(ctrl.battery_export_kwh, phys.grid_export)
            soc_pct = (phys.soc_end / battery_cap) * 100.0 if battery_cap else 0.0
            action = timer_classify_action(
                bat_charge=max(0.0, phys.battery_delta),
                bat_discharge=max(0.0, -phys.battery_delta),
                grid_import=phys.grid_import,
                grid_export=phys.grid_export,
                production=pv_q[global_step],
                epsilon=eps_q,
            )
            slot = {
                "hour": hh,
                "quarter": qq,
                "action": action,
                "pv": pv_q[global_step],
                "load": load_q[global_step],
                "grid_import": phys.grid_import,
                "grid_export": phys.grid_export,
                "battery_delta": phys.battery_delta,
                "battery_export_kwh": batt_exp,
                "grid_charge_kw": ctrl.grid_charge_kw,
                "load_from_grid": ctrl.load_from_grid,
                "ctrl_battery_export_kwh": ctrl.battery_export_kwh,
                "soc_pct": soc_pct,
                "soc_end": phys.soc_end,
                "reserve_kwh": reserve,
                "reserve_soc_pct": (
                    (float(reserve) / battery_cap) * 100.0 if battery_cap else None
                ),
                "rce": rce_q[global_step] if global_step < len(rce_q) else None,
            }
            _append_q15_slot(
                q15_by_hour=q15_by_hour,
                q15_plan_rows=q15_plan_rows,
                start_dt=start_dt,
                h=hh,
                quarter=qq,
                slot=slot,
                battery_cap=battery_cap,
            )
            soc = phys.soc_end
            global_step += 1
            reserve_idx += 1

        h = next_override

    return {
        **plan,
        "q15_by_hour": q15_by_hour,
        "q15_plan_rows": q15_plan_rows,
        "end_soc_kwh": round(soc, 3),
        "timer_schedule": derive_timer_schedule_q15(q15_plan_rows, cfg),
    }


def apply_plan_timer_overrides_if_any(
    plan: dict[str, Any] | None,
    *,
    date_str: str,
    pv_hourly: list[float],
    load_hourly: list[float],
    tomorrow_pv: list[float],
    tomorrow_load: list[float],
    cfg: dict,
    from_hour: int,
    rce_quarters: list[float | None] | None = None,
    gap_mode: str = "optimize",
) -> dict[str, Any] | None:
    """Apply saved manual Timer Schedule cells and replay from the earliest override hour."""
    overrides = get_timer_overrides_for_date(cfg, date_str)
    if not plan or not overrides:
        return plan

    # Skip past-only timer overrides on a mid-day rolling plan (from_hour > 0).
    # Those cells belong on the solid as-if-00:00 SOC curve (from_hour=0).
    if max(overrides.keys()) < int(from_hour):
        return plan

    from .plan_q15 import resolve_rce_quarters, split_energy_hourly_to_q15
    from .g12_pricing import get_buy_price

    pv_q = split_energy_hourly_to_q15(pv_hourly)
    load_q = split_energy_hourly_to_q15(load_hourly)
    base = datetime.strptime(date_str, "%Y-%m-%d")
    buy_q: list[float] = []
    for h in range(24):
        price = get_buy_price(base.replace(hour=h), cfg)[0]
        buy_q.extend([price] * Q15_PER_HOUR)
    rce_q = resolve_rce_quarters(date_str, rce_quarters)
    forecast = {
        "today": {"pv": pv_hourly, "load": load_hourly},
        "tomorrow": {"pv": tomorrow_pv, "load": tomorrow_load},
    }
    replay_from = max(from_hour, min(overrides.keys()))
    initial_soc = _soc_at_hour_start(plan, replay_from) if replay_from > 0 else None

    return replay_day_plan_with_timer_overrides(
        plan,
        overrides,
        date_str=date_str,
        pv_q=pv_q,
        load_q=load_q,
        buy_q=buy_q,
        rce_q=rce_q,
        cfg=cfg,
        from_hour=from_hour,
        initial_soc_kwh=initial_soc,
        tomorrow_pv=tomorrow_pv,
        tomorrow_load=tomorrow_load,
        forecast=forecast,
        gap_mode=gap_mode,
    )
