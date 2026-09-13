"""Incremental Energy arbitrage plan merge — SQLite is the source of truth."""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta
from typing import Any

from .influxdb import now_warsaw
from .plan_cost import compute_plan_totals
from .plan_hourly_actuals import (
    Q15_PER_HOUR,
    _actual_q15_battery_grid,
    actual_q15_slice_kwh,
    _bound_soc_pct,
    _clamp_soc_pct,
    _soc_kwh_after_battery_delta,
    apply_open_pull_quarter_to_row,
    apply_q15_physics_to_row,
    hour_start_soc_kwh,
    meter_soc_pct_for_q15,
    overlay_meter_soc_on_rows,
    refresh_row_grid_cash,
)
from .simulation_config import plan_min_soc_kwh, plan_min_soc_pct
from .timer_plan import (
    ACTION_DISCHARGE_LOAD,
    parse_timer_schedule_segments,
)

log = logging.getLogger(__name__)


def _timer_has_chg(timer_txt: str) -> bool:
    return str(timer_txt or "").strip().startswith("Chg")


def _timer_chg_ends_after(timer_txt: str, minute_of_day: int) -> bool:
    """True when any Chg segment ends after *minute_of_day* (window not finished)."""
    for seg in parse_timer_schedule_segments(timer_txt):
        if seg.get("kind") != "chg":
            continue
        try:
            hh, mm = str(seg["to"]).split(":")
            end_min = int(hh) * 60 + int(mm)
        except (TypeError, ValueError):
            continue
        if end_min > minute_of_day:
            return True
    return False


def _should_preserve_imminent_chg(
    existing_row: dict[str, Any],
    fresh_row: dict[str, Any],
    *,
    plan_date: str,
    hour: int,
    today_str: str,
    current_hour: int,
) -> bool:
    """Keep SQLite next-hour Chg when the fresh sim clears that timer before :00.

    Front-load often slips charge to hour+1; keep the committed Chg so the :00
    SA sync still sees the planned timer. Thin Chg is cleared elsewhere via
    min_hourly / economics before it is written.
    """
    if plan_date != today_str or hour != current_hour + 1:
        return False
    if existing_row.get("timer_schedule_manual"):
        return False
    existing_timer = str(existing_row.get("timer_schedule") or "").strip()
    fresh_timer = str(fresh_row.get("timer_schedule") or "").strip()
    if not _timer_has_chg(existing_timer):
        return False
    return not fresh_timer


def _strip_slipped_next_hour_chg(
    row: dict[str, Any],
    existing_row: dict[str, Any] | None,
    *,
    current_timer: str,
    now: datetime,
) -> bool:
    """Clear next-hour Chg while the current hour Chg window is still open.

    Prevents a second front-load Chg (e.g. 03:00-03:30) while 02:00-02:30 is
    still the active commitment — recovery belongs after the window ends.
    """
    if not _timer_has_chg(current_timer):
        return False
    now_min = now.hour * 60 + now.minute
    if not _timer_chg_ends_after(current_timer, now_min):
        return False
    if not _timer_has_chg(str(row.get("timer_schedule") or "")):
        return False
    prev_timer = (
        str(existing_row.get("timer_schedule") or "").strip()
        if existing_row is not None
        else ""
    )
    prev_action = (
        str(existing_row.get("action") or "").strip()
        if existing_row is not None
        else ""
    )
    if prev_timer and not _timer_has_chg(prev_timer):
        row["timer_schedule"] = prev_timer
        row["action"] = prev_action or ACTION_DISCHARGE_LOAD
    else:
        row["timer_schedule"] = ""
        row["action"] = ACTION_DISCHARGE_LOAD
    return True


def last_completed_quarter_tick(now: datetime) -> tuple[int, int]:
    """Return (hour_offset, quarter) for the q15 slot that just ended.

    hour_offset 0 = current clock hour; -1 = previous hour (only at :00).
    Clock boundary only — Influx 10-min data for this slot is usually incomplete.
    Use `freeze_ready_quarter_tick` to decide what may be frozen into SQLite.
    """
    minute = now.minute
    if minute == 0:
        return -1, 3
    if minute < 15:
        return 0, -1
    if minute < 30:
        return 0, 0
    if minute < 45:
        return 0, 1
    return 0, 2


def freeze_ready_quarter_tick(now: datetime) -> tuple[int, int]:
    """Return (hour_offset, quarter) ready to freeze from Influx.

    The :30 plan job starts at :31 so the :20-:30 10-min bucket is present;
    freeze through current q1. Other ticks still wait one q15 for GROUP BY.

      :30/:31 → (0, 1) current q1 (:15-:30)
      :45 → (0, 1)  current q1 (:15-:30)
      :00 → (-1, 2) previous q2 (:30-:45)
      :15 → (-1, 3) previous q3 (:45-:00)
      :01-:14 → (0, -1) no new freeze this window
    """
    hour_offset, tick_q = last_completed_quarter_tick(now)
    if hour_offset == 0 and tick_q < 0:
        return 0, -1
    if hour_offset == -1 and tick_q == 3:
        return -1, 2
    if hour_offset == 0 and tick_q == 0:
        return -1, 3
    if hour_offset == 0 and tick_q == 1:
        return 0, 1
    if hour_offset == 0 and tick_q >= 1:
        return 0, tick_q - 1
    return 0, -1


def max_freeze_quarter_for_hour(
    now: datetime,
    *,
    hour: int,
    current_hour: int,
) -> int:
    """Highest q15 index that may be frozen for *hour* at *now* (-1 = none)."""
    if hour < current_hour - 1:
        return Q15_PER_HOUR - 1
    if hour == current_hour - 1:
        fo, fq = freeze_ready_quarter_tick(now)
        if fo == -1 and fq >= 0:
            return fq
        return Q15_PER_HOUR - 1
    if hour == current_hour:
        fo, fq = freeze_ready_quarter_tick(now)
        if fo == 0 and fq >= 0:
            return fq
        return -1
    return -1


def quarter_tick_now(now: datetime) -> datetime:
    """Floor *now* to the :00/:15/:30/:45 tick that the job belongs to.

    Long Open-Meteo / sim work can push wall-clock past the minute; merge must
    still treat the scheduled quarter boundary as the tick (so :00 still
    finalizes previous-hour q3 after a delayed start). The :31 cron floors to
    :30.
    """
    minute = (int(now.minute) // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0)


def _row_key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row.get("plan_date") or ""), int(row.get("hour", -1))


def _find_row(rows: list[dict], plan_date: str, hour: int) -> dict[str, Any] | None:
    for row in rows:
        if row.get("start") == "TOTAL":
            continue
        if str(row.get("plan_date") or "") == plan_date and int(row.get("hour", -1)) == hour:
            return row
    return None


def _q15_slot_actual(slot: dict[str, Any] | None) -> bool:
    return bool(slot and slot.get("from_actual"))


def _ensure_q15_length(q15: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(q15)
    while len(out) < Q15_PER_HOUR:
        prev_soc = out[-1].get("soc", 0.0) if out else 0.0
        out.append({
            "quarter": len(out),
            "production": 0.0,
            "consumption": 0.0,
            "soc": prev_soc,
            "battery": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "from_actual": False,
        })
    return out[:Q15_PER_HOUR]


def _build_actual_q15_slot(
    hour: int,
    quarter: int,
    *,
    series_10min: dict[str, list[float | None]] | None,
    soc_start_kwh: float,
    cfg: dict,
    rce: float | None = None,
) -> dict[str, Any]:
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc_pct = plan_min_soc_pct(cfg)
    min_kwh = plan_min_soc_kwh(cfg)
    bat_delta, grid_import, grid_export = _actual_q15_battery_grid(
        series_10min, hour, quarter,
    )
    pv = actual_q15_slice_kwh((series_10min or {}).get("pv"), hour, quarter)
    load = actual_q15_slice_kwh((series_10min or {}).get("load"), hour, quarter)
    soc_kwh = _soc_kwh_after_battery_delta(
        soc_start_kwh,
        bat_delta,
        min_kwh=min_kwh,
        battery_cap=battery_cap,
    )
    meter_pct = meter_soc_pct_for_q15(series_10min, hour, quarter)
    soc_pct = meter_pct if meter_pct is not None else _bound_soc_pct(
        (soc_kwh / battery_cap) * 100.0
    )
    slot = {
        "quarter": quarter,
        "production": round(pv, 4),
        "consumption": round(load, 4),
        "soc": soc_pct,
        "battery": bat_delta,
        "grid_import": grid_import,
        "grid_export": grid_export,
        "from_actual": True,
    }
    if rce is not None:
        slot["rce"] = rce
    return slot


def _apply_actual_quarter_if_needed(
    row: dict[str, Any],
    hour: int,
    quarter: int,
    *,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
    live_soc_kwh: float | None = None,
) -> bool:
    """Write one actual q15 slot from Influx when not yet frozen.

    Freeze-once: if `from_actual` is already set, leave the slot untouched.
    There is no rewrite path for frozen quarters on the live tick.
    """
    q15 = _ensure_q15_length(list(row.get("q15") or []))
    if _q15_slot_actual(q15[quarter]):
        return False

    min_soc_pct = plan_min_soc_pct(cfg)
    soc_start = hour_start_soc_kwh(today_hourly, hour, battery_cap, min_soc_pct)
    if soc_start is None:
        # Prior completed actual end-SOC inside this hour.
        for q in range(quarter - 1, -1, -1):
            if _q15_slot_actual(q15[q]):
                soc_start = (float(q15[q].get("soc") or 0) / 100.0) * battery_cap
                break
    if soc_start is None and q15:
        # Implied :00 start from the planned/first slot (end − battery Δ).
        # Prefer this over live — live is mid-hour, not hour-start.
        end0 = (float(q15[0].get("soc") or 0) / 100.0) * battery_cap
        bat0 = float(q15[0].get("battery") or 0.0)
        soc_start = end0 - bat0
    if soc_start is None and live_soc_kwh is not None:
        soc_start = float(live_soc_kwh)
    if soc_start is None:
        return False

    soc_kwh = soc_start
    for q in range(quarter):
        if _q15_slot_actual(q15[q]):
            soc_kwh = (float(q15[q].get("soc") or 0) / 100.0) * battery_cap
        else:
            bat = float(q15[q].get("battery") or 0)
            soc_kwh = _soc_kwh_after_battery_delta(
                soc_kwh,
                bat,
                min_kwh=plan_min_soc_kwh(cfg),
                battery_cap=battery_cap,
            )

    planned_rce = q15[quarter].get("rce") if isinstance(q15[quarter], dict) else None
    hour_rce = list(row.get("rce_q15") or [])
    if planned_rce is None and quarter < len(hour_rce):
        planned_rce = hour_rce[quarter]
    q15[quarter] = _build_actual_q15_slot(
        hour,
        quarter,
        series_10min=series_10min,
        soc_start_kwh=soc_kwh,
        cfg=cfg,
        rce=planned_rce,
    )
    row["q15"] = q15
    apply_q15_physics_to_row(row, q15)
    refresh_row_grid_cash(row, cfg)
    return True



def _finalize_hour_actual_quarters(
    row: dict[str, Any],
    hour: int,
    *,
    through_quarter: int,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
    live_soc_kwh: float | None = None,
) -> bool:
    """Update unfrozen q15 slots 0..through_quarter from Influx, then freeze each.

    Already-frozen slots are left untouched. Call after the Influx series for
    this tick is available — freeze is the write that sets `from_actual`.
    """
    if through_quarter < 0:
        return False
    changed = False
    for q in range(min(through_quarter, Q15_PER_HOUR - 1) + 1):
        if _apply_actual_quarter_if_needed(
            row,
            hour,
            q,
            series_10min=series_10min,
            today_hourly=today_hourly,
            cfg=cfg,
            battery_cap=battery_cap,
            live_soc_kwh=live_soc_kwh,
        ):
            changed = True
    return changed


def _finalize_history_actual_quarters(
    history: list[dict[str, Any]],
    *,
    now: datetime,
    today_str: str,
    current_hour: int,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
) -> int:
    """Fill Influx-ready unfrozen q15 on today's past history hours.

    Previous-hour q3 freezes from :15. Does not rewrite already-frozen slots.
    """
    fixed = 0
    for row in history:
        if str(row.get("plan_date") or "") != today_str:
            continue
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if hour < 0 or hour >= current_hour:
            continue
        through_q = max_freeze_quarter_for_hour(
            now, hour=hour, current_hour=current_hour,
        )
        if through_q < 0:
            continue
        q15 = _ensure_q15_length(list(row.get("q15") or []))
        # Only recover gaps after a partial freeze (e.g. missed :15 q3).
        # Untouched plan/meter stubs stay as-is.
        if not any(_q15_slot_actual(s) for s in q15):
            continue
        ready = q15[: through_q + 1]
        if ready and all(_q15_slot_actual(s) for s in ready):
            continue
        if _finalize_hour_actual_quarters(
            row,
            hour,
            through_quarter=through_q,
            series_10min=series_10min,
            today_hourly=today_hourly,
            cfg=cfg,
            battery_cap=battery_cap,
        ):
            fixed += 1
    return fixed


def datafix_completed_quarters_from_live(
    row: dict[str, Any],
    *,
    hour: int,
    now: datetime,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
    live_soc_kwh: float,
) -> bool:
    """Update Influx-ready completed quarters; rechain later slots.

    At :30 freeze q0–q1 (:00-:30). Earlier frozen slots stay.
    Before :30 in the current hour, nothing is freeze-ready here.
    """
    del live_soc_kwh  # mid-hour meter; never treat as hour-start for EOH display
    min_soc_pct = plan_min_soc_pct(cfg)
    min_kwh = plan_min_soc_kwh(cfg)
    # Freeze quarters whose Influx 10-min windows are ready at this tick.
    fo, fq = freeze_ready_quarter_tick(now)
    freeze_through = fq if (fo == 0 and fq >= 0) else -1
    if freeze_through < 0:
        return False

    # Influx first, then freeze — only unfrozen slots through freeze-ready.
    changed = _finalize_hour_actual_quarters(
        row,
        hour,
        through_quarter=freeze_through,
        series_10min=series_10min,
        today_hourly=today_hourly,
        cfg=cfg,
        battery_cap=battery_cap,
        live_soc_kwh=None,
    )

    q15 = _ensure_q15_length(list(row.get("q15") or []))
    soc_kwh = (float(q15[freeze_through].get("soc") or 0) / 100.0) * battery_cap
    for q in range(freeze_through + 1, Q15_PER_HOUR):
        bat = float(q15[q].get("battery") or 0.0)
        soc_kwh = _soc_kwh_after_battery_delta(
            soc_kwh, bat, min_kwh=min_kwh, battery_cap=battery_cap,
        )
        new_pct = _clamp_soc_pct((soc_kwh / battery_cap) * 100.0, min_soc_pct)
        if abs(float(q15[q].get("soc") or 0) - new_pct) > 0.05:
            changed = True
        q15[q]["soc"] = new_pct

    row["q15"] = q15
    apply_q15_physics_to_row(row, q15)
    refresh_row_grid_cash(row, cfg)
    return changed


def _live_soc_kwh_from_metrics(
    metrics: dict[str, Any] | None,
    *,
    battery_cap: float,
    live_soc_pct: float | None = None,
) -> float | None:
    """Return live inverter SOC in kWh when SA is online; otherwise None."""
    if not metrics or not metrics.get("sa_online"):
        return None
    raw = live_soc_pct if live_soc_pct is not None else metrics.get("battery_soc")
    if raw is None:
        return None
    pct = max(0.0, min(100.0, float(raw)))
    return (pct / 100.0) * battery_cap


def _q15_bat_charge_kwh(row: dict[str, Any] | None) -> float:
    if not row:
        return 0.0
    return sum(max(0.0, float(s.get("battery") or 0)) for s in (row.get("q15") or []))


def _q15_bat_discharge_kwh(row: dict[str, Any] | None) -> float:
    if not row:
        return 0.0
    return sum(max(0.0, -float(s.get("battery") or 0)) for s in (row.get("q15") or []))


def _locked_timer_q15_mismatch(
    row: dict[str, Any],
    fresh_row: dict[str, Any] | None,
) -> bool:
    """True when locked Chg/Dis label has no matching energy but *fresh_row* does."""
    timer = str(row.get("timer_schedule") or "").strip().lower()
    if timer.startswith("chg") or " chg " in f" {timer} ":
        return _q15_bat_charge_kwh(row) < 0.05 and _q15_bat_charge_kwh(fresh_row) > 0.05
    if timer.startswith("dis") or " dis " in f" {timer} ":
        # Export Dis should show positive grid export or battery discharge above house.
        row_exp = sum(float(s.get("grid_export") or 0) for s in (row.get("q15") or []))
        fresh_exp = sum(
            float(s.get("grid_export") or 0) for s in ((fresh_row or {}).get("q15") or [])
        )
        return row_exp < 0.05 and fresh_exp > 0.05
    return False


def _merge_current_hour_q15(
    row: dict[str, Any],
    *,
    now: datetime,
    hour: int,
    series_10min: dict[str, list[float | None]] | None,
    today_hourly: dict[str, list[float | None]] | None,
    cfg: dict,
    battery_cap: float,
    live_soc_kwh: float | None = None,
    fresh_row: dict[str, Any] | None = None,
) -> None:
    """Apply freeze-ready actuals; rebuild open pull + tail q15 from *fresh_row*.

    Timer Schedule / Action are left untouched (caller keeps them locked).
    Freeze-ready quarters get Influx once (`from_actual=True`). The open pull
    quarter and future quarters take Production, battery, grid, SOC from the
    fresh optimizer blend (Influx partial + forecast fill on the pull tick).

    Before the first quarter ends, keep the :00 end-of-hour SOC chain on the
    locked row — do not replace it with a mid-hour live-seeded fresh curve —
    unless the locked timer implies Chg/Dis that the stored q15 never applied.
    """
    hour_offset, tick_q = last_completed_quarter_tick(now)
    fo, fq = freeze_ready_quarter_tick(now)
    if hour_offset == 0 and tick_q < 0 and row.get("q15"):
        if not _locked_timer_q15_mismatch(row, fresh_row):
            # :00–:14 — preserve locked hour-end SOC from the :00 plan.
            q15 = _ensure_q15_length(list(row.get("q15") or []))
            apply_q15_physics_to_row(row, q15)
            refresh_row_grid_cash(row, cfg)
            return
        # Locked Chg/Dis label with idle q15 — take fresh physics for the hour.
        if fresh_row is not None:
            row["q15"] = copy.deepcopy(_ensure_q15_length(list(fresh_row.get("q15") or [])))
            for slot in row["q15"]:
                slot["from_actual"] = False
            apply_q15_physics_to_row(row, row["q15"])
            refresh_row_grid_cash(row, cfg)
            return

    # Freeze only the lag-ready quarter in this hour (Influx 10-min settled).
    if fo == 0 and fq >= 0:
        _apply_actual_quarter_if_needed(
            row,
            hour,
            fq,
            series_10min=series_10min,
            today_hourly=today_hourly,
            cfg=cfg,
            battery_cap=battery_cap,
            live_soc_kwh=live_soc_kwh,
        )

    # Open pull + tail from fresh (pull index = freeze_ready + 1 when same hour).
    from_q = (fq + 1) if (fo == 0 and fq >= 0) else 0
    if fresh_row is not None:
        eq = _ensure_q15_length(list(row.get("q15") or []))
        fq_slots = _ensure_q15_length(list(fresh_row.get("q15") or []))
        for q in range(from_q, Q15_PER_HOUR):
            slot = copy.deepcopy(fq_slots[q])
            slot["quarter"] = q
            slot["from_actual"] = False
            eq[q] = slot
        row["q15"] = eq

    q15 = _ensure_q15_length(list(row.get("q15") or []))
    apply_q15_physics_to_row(row, q15)
    refresh_row_grid_cash(row, cfg)


def _lock_hour_labels(row: dict[str, Any], fresh_row: dict[str, Any]) -> None:
    """Lock the current-hour Timer Schedule as stored (empty, Chg, or Dis)."""
    del fresh_row
    row["hour_labels_locked"] = True


def _copy_future_row(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Future plan hour — full row from optimizer (including timer/action)."""
    for key, val in src.items():
        dst[key] = copy.deepcopy(val)
    dst["hour_labels_locked"] = False


def _history_has(history: list[dict], plan_date: str, hour: int) -> bool:
    return _find_row(history, plan_date, hour) is not None


def _as_history_row(row: dict[str, Any]) -> dict[str, Any]:
    """Copy a plan row for history: blended-live SOC marker applies only to the
    in-progress hour, so it must not survive promotion."""
    hist = copy.deepcopy(row)
    hist["history_hour"] = True
    hist.pop("soc_blended", None)
    # Freeze Timer/Action once the hour leaves the live plan.
    if str(hist.get("timer_schedule") or "").strip() or hist.get("timer_schedule_manual"):
        hist["hour_labels_locked"] = True
    return hist


def _effective_plan_boundary_hour(plan: dict[str, Any] | None, now: datetime) -> int:
    """Wall hour, or plan_from_hour when the sim already crossed into the next hour.

    write_plan guard must use the same boundary as merge_incremental_plan /
    attach_immutable_history — otherwise a straddle refresh that finishes after
    :00 with now still HH:59 drops the completed hour from history (meters
    backfill later without Timer Schedule).
    """
    current_hour = int(now.hour)
    if not isinstance(plan, dict):
        return current_hour
    try:
        sim_from = int(plan.get("plan_from_hour"))
    except (TypeError, ValueError):
        return current_hour
    if sim_from > current_hour:
        return sim_from
    return current_hour


def _strip_blended_flags(history: list[dict]) -> None:
    """Clear ``soc_blended`` on history rows."""
    for row in history:
        row.pop("soc_blended", None)


def _sort_history(history: list[dict]) -> None:
    history.sort(
        key=lambda r: (str(r.get("plan_date") or ""), int(r.get("hour", -1))),
    )


def _backfill_history_from_meters(
    history: list[dict],
    meter_rows: list[dict] | None,
    *,
    today_str: str,
    current_hour: int,
) -> int:
    """Append meters-based rows for past hours absent from history (append-only).

    Existing history entries stay unchanged. Fill missing past hours after a
    missed :00 promote (restart/race at the hour boundary).
    """
    added = 0
    for row in meter_rows or []:
        plan_date = str(row.get("plan_date") or "")
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if plan_date != today_str or hour < 0 or hour >= current_hour:
            continue
        if _history_has(history, plan_date, hour):
            continue
        history.append(_as_history_row(row))
        added += 1
    if added:
        log.warning(
            "History backfill — %d missing past hour(s) restored from meters", added,
        )
    return added


def merge_incremental_plan(
    existing: dict[str, Any],
    fresh: dict[str, Any],
    *,
    now: datetime | None = None,
    metrics: dict[str, Any] | None = None,
    cfg: dict,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply quarter tick merge: immutable past, incremental current, live future."""
    del rules
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    current_hour = now.hour
    # Fresh sim may start one hour later than `now` if the hour flipped while
    # it was computed — treat that later hour as current so nothing is dropped.
    try:
        fresh_from_hour = int(fresh.get("plan_from_hour"))
    except (TypeError, ValueError):
        fresh_from_hour = current_hour
    if fresh_from_hour > current_hour:
        log.warning(
            "Fresh sim crossed hour boundary during merge (%02d -> %02d)",
            current_hour,
            fresh_from_hour,
        )
        current_hour = fresh_from_hour
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    today_hourly = (metrics or {}).get("today_hourly")
    series_10min = (metrics or {}).get("series_10min")
    live_soc_kwh = _live_soc_kwh_from_metrics(
        metrics,
        battery_cap=battery_cap,
        live_soc_pct=(fresh.get("live_soc_pct") if isinstance(fresh, dict) else None),
    )

    merged = copy.deepcopy(fresh)
    merged["history_rows"] = copy.deepcopy(existing.get("history_rows") or [])
    history = merged["history_rows"]
    _strip_blended_flags(history)
    history_hours_in = sorted(
        int(r.get("hour", -1)) for r in history
        if str(r.get("plan_date") or "") == today_str
    )
    fresh_by_key = {
        _row_key(r): r for r in (fresh.get("rows") or []) if r.get("start") != "TOTAL"
    }
    existing_by_key = {
        _row_key(r): r for r in (existing.get("rows") or []) if r.get("start") != "TOTAL"
    }

    hour_offset, tick_q = last_completed_quarter_tick(now)
    fo, fq = freeze_ready_quarter_tick(now)

    # :00 — freeze previous-hour q2 (Influx-ready), move hour to history; q3 at :15.
    if fo == -1 and fq == 2 and current_hour > 0:
        prev_hour = current_hour - 1
        prev_key = (today_str, prev_hour)
        prev_row = existing_by_key.get(prev_key) or _find_row(history, today_str, prev_hour)
        if prev_row is not None:
            through_q = max_freeze_quarter_for_hour(
                now, hour=prev_hour, current_hour=current_hour,
            )
            if not _history_has(history, today_str, prev_hour):
                prev_copy = _as_history_row(prev_row)
                _finalize_hour_actual_quarters(
                    prev_copy,
                    prev_hour,
                    through_quarter=through_q,
                    series_10min=series_10min,
                    today_hourly=today_hourly,
                    cfg=cfg,
                    battery_cap=battery_cap,
                )
                # Open-pull q3 (Influx + forecast fill); freeze at :15.
                apply_open_pull_quarter_to_row(
                    prev_copy,
                    prev_hour,
                    3,
                    series_10min=series_10min,
                    cfg=cfg,
                )
                history.append(prev_copy)
            else:
                # Hour already in history — freeze missing ready quarters only; never
                # rewrite timer_schedule / action (I4).
                for hrow in history:
                    if _row_key(hrow) == prev_key:
                        _finalize_hour_actual_quarters(
                            hrow,
                            prev_hour,
                            through_quarter=through_q,
                            series_10min=series_10min,
                            today_hourly=today_hourly,
                            cfg=cfg,
                            battery_cap=battery_cap,
                        )
                        apply_open_pull_quarter_to_row(
                            hrow,
                            prev_hour,
                            3,
                            series_10min=series_10min,
                            cfg=cfg,
                        )
                        break

    # Promote any completed hour still sitting in existing.rows (missed :00 tick
    # after a restart/delayed refresh).
    for key, row in existing_by_key.items():
        plan_date, hour = key
        if plan_date != today_str or hour >= current_hour:
            continue
        if _history_has(history, plan_date, hour):
            continue
        promoted = _as_history_row(row)
        through_q = max_freeze_quarter_for_hour(
            now, hour=hour, current_hour=current_hour,
        )
        if through_q >= 0:
            _finalize_hour_actual_quarters(
                promoted,
                hour,
                through_quarter=through_q,
                series_10min=series_10min,
                today_hourly=today_hourly,
                cfg=cfg,
                battery_cap=battery_cap,
            )
        history.append(promoted)
        log.warning(
            "History promote (late) — hour %02d was not moved at :00, recovered from rows",
            hour,
        )

    # Fill missing past-hour history from the fresh sim meter rows.
    _backfill_history_from_meters(
        history,
        fresh.get("history_rows"),
        today_str=today_str,
        current_hour=current_hour,
    )
    _sort_history(history)
    # Recover any still-unfrozen q15 on past hours (missed :00 / delayed tick).
    fixed = _finalize_history_actual_quarters(
        history,
        now=now,
        today_str=today_str,
        current_hour=current_hour,
        series_10min=series_10min,
        today_hourly=today_hourly,
        cfg=cfg,
        battery_cap=battery_cap,
    )
    if fixed:
        log.info("History q15 finalized from Influx for %d past hour(s)", fixed)

    fresh_hist_by = {
        _row_key(r): r for r in (fresh.get("history_rows") or [])
        if r.get("start") != "TOTAL"
    }
    for hrow in history:
        src = fresh_hist_by.get(_row_key(hrow))
        if src is not None:
            _absorb_incoming_rce(hrow, src, cfg=cfg)

    out_rows: list[dict[str, Any]] = []
    for key, fresh_row in sorted(fresh_by_key.items(), key=lambda x: (x[0][0], x[0][1])):
        plan_date, hour = key
        if plan_date < today_str:
            continue
        if plan_date == today_str and hour < current_hour:
            continue  # past hours already promoted/backfilled above

        existing_row = existing_by_key.get(key)
        if existing_row is None:
            new_row = copy.deepcopy(fresh_row)
            if plan_date == today_str and hour == current_hour:
                new_row["soc_blended"] = True
                if now.minute == 0:
                    _lock_hour_labels(new_row, fresh_row)
            else:
                new_row.pop("soc_blended", None)
            out_rows.append(new_row)
            continue

        row = copy.deepcopy(existing_row)

        if plan_date == today_str and hour == current_hour:
            if not row.get("hour_labels_locked"):
                if now.minute == 0:
                    _lock_hour_labels(row, fresh_row)
                else:
                    # Missed :00 — lock the timer/action already in SQLite for this hour.
                    row["hour_labels_locked"] = True
            # Remaining q15 from fresh (weather/plan), then completed → fact and
            # SOC rechain from live so mid-hour stays continuous.
            _merge_current_hour_q15(
                row,
                now=now,
                hour=hour,
                series_10min=series_10min,
                today_hourly=today_hourly,
                cfg=cfg,
                battery_cap=battery_cap,
                live_soc_kwh=live_soc_kwh,
                fresh_row=fresh_row,
            )
            # Influx update then freeze the just-completed tick (live SOC unused).
            datafix_completed_quarters_from_live(
                row,
                hour=hour,
                now=now,
                series_10min=series_10min,
                today_hourly=today_hourly,
                cfg=cfg,
                battery_cap=battery_cap,
                live_soc_kwh=live_soc_kwh if live_soc_kwh is not None else 0.0,
            )
            _absorb_incoming_rce(row, fresh_row, cfg=cfg)
            # Violet live-SOC highlight: always the in-progress hour.
            row["soc_blended"] = True
            out_rows.append(row)
            continue

        # Future hours (today after current, and tomorrow): live optimizer row.
        _copy_future_row(row, fresh_row)
        _keep_rce_if_incoming_empty(row, existing_row)
        row.pop("soc_blended", None)
        if _should_preserve_imminent_chg(
            existing_row,
            fresh_row,
            plan_date=plan_date,
            hour=hour,
            today_str=today_str,
            current_hour=current_hour,
        ):
            row["timer_schedule"] = existing_row.get("timer_schedule", "")
            row["action"] = existing_row.get("action", "")
            log.info(
                "Plan merge — preserved imminent Chg on H%02d (fresh wiped timer)",
                hour,
            )
        out_rows.append(row)

    # While current-hour Chg is still open, drop slipped Chg on the next hour.
    current_row = next(
        (
            r for r in out_rows
            if str(r.get("plan_date") or "") == today_str
            and int(r.get("hour", -1)) == current_hour
        ),
        None,
    )
    current_timer = (
        str(current_row.get("timer_schedule") or "").strip()
        if current_row is not None
        else ""
    )
    if current_timer:
        for row in out_rows:
            if str(row.get("plan_date") or "") != today_str:
                continue
            if int(row.get("hour", -1)) != current_hour + 1:
                continue
            existing_next = existing_by_key.get((today_str, current_hour + 1))
            if _strip_slipped_next_hour_chg(
                row,
                existing_next,
                current_timer=current_timer,
                now=now,
            ):
                log.info(
                    "Plan merge — cleared slipped Chg on H%02d while H%02d Chg still open",
                    current_hour + 1,
                    current_hour,
                )
            break

    merged["history_rows"] = history
    merged["rows"] = out_rows
    overlay_meter_soc_on_rows(
        merged["history_rows"],
        today_str=today_str,
        today_hourly=today_hourly,
        series_10min=series_10min,
        current_hour=current_hour,
    )
    overlay_meter_soc_on_rows(
        merged["rows"],
        today_str=today_str,
        today_hourly=today_hourly,
        series_10min=series_10min,
        current_hour=current_hour,
    )
    merged["has_history_rows"] = bool(history)
    merged["computed_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    merged["today_date"] = today_str
    merged["plan_from_hour"] = current_hour

    history_hours_out = sorted(
        int(r.get("hour", -1)) for r in history
        if str(r.get("plan_date") or "") == today_str
    )
    if history_hours_out != history_hours_in:
        log.info("Plan history hours %s -> %s", history_hours_in, history_hours_out)
    return merged


def plan_needs_full_rebuild(cached: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """True when no plan exists or calendar day changed (midnight full rebuild)."""
    if not cached:
        return True
    now = now or now_warsaw()
    return cached.get("today_date") != now.strftime("%Y-%m-%d")


def next_replan_boundary(now: datetime | None = None) -> tuple[str, int, int]:
    """First q15 slot a config/override replan may rewrite: (plan_date, hour, quarter).

    Ceil to the next quarter start; if *now* is exactly on :00/:15/:30/:45, that
    quarter is included.

    Examples (Warsaw):
      22:44 → (today, 22, 3)  # from 22:45
      22:48 → (today, 23, 0)  # from 23:00
      22:45:00 → (today, 22, 3)
    """
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    on_boundary = (
        now.minute % 15 == 0
        and now.second == 0
        and now.microsecond == 0
    )
    if on_boundary:
        return today_str, now.hour, now.minute // 15

    total_min = now.hour * 60 + now.minute
    next_min = ((total_min // 15) + 1) * 15
    if next_min >= 24 * 60:
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        return tomorrow, 0, 0
    return today_str, next_min // 60, (next_min % 60) // 15


def _first_fresh_quarter_in_hour(
    now: datetime,
    hour: int,
    *,
    boundary: tuple[str, int, int] | None = None,
) -> int:
    """Quarter index (0..4) from which *hour* may take fresh q15; 4 = keep all."""
    today_str = now.strftime("%Y-%m-%d")
    from_date, from_hour, from_q = boundary or next_replan_boundary(now)
    if from_date != today_str:
        return Q15_PER_HOUR if from_date > today_str else 0
    if from_hour > hour:
        return Q15_PER_HOUR
    if from_hour < hour:
        return 0
    return from_q


def splice_replan_from_quarter(
    existing: dict[str, Any],
    fresh: dict[str, Any],
    *,
    now: datetime | None = None,
    boundary: tuple[str, int, int] | None = None,
) -> dict[str, Any]:
    """Config/override splice: keep slots before *boundary*, take the rest from fresh.

    Past hours stay verbatim. The boundary hour keeps completed + in-progress
    quarters from SQLite and only rewrites from the boundary quarter onward.
    Later hours / tomorrow come entirely from *fresh*.
    """
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    from_date, from_hour, from_q = boundary or next_replan_boundary(now)
    result = copy.deepcopy(fresh)

    result["history_rows"] = copy.deepcopy(existing.get("history_rows") or [])
    _strip_blended_flags(result["history_rows"])

    existing_by_key = {
        _row_key(r): r for r in (existing.get("rows") or []) if r.get("start") != "TOTAL"
    }
    fresh_by_key = {
        _row_key(r): r for r in (fresh.get("rows") or []) if r.get("start") != "TOTAL"
    }

    kept: list[dict[str, Any]] = []

    # Today hours fully before the boundary hour — keep from SQLite.
    for key, row in sorted(existing_by_key.items(), key=lambda x: (x[0][0], x[0][1])):
        plan_date, hour = key
        if plan_date != today_str:
            continue
        if from_date > today_str or hour < from_hour:
            kept.append(copy.deepcopy(row))

    # Boundary hour on today — merge q15 at from_q.
    if from_date == today_str:
        bkey = (today_str, from_hour)
        existing_row = existing_by_key.get(bkey)
        fresh_row = fresh_by_key.get(bkey)
        if existing_row is not None and fresh_row is not None:
            kept.append(
                _merge_hour_from_quarter(existing_row, fresh_row, from_q=from_q),
            )
        elif existing_row is not None:
            kept.append(copy.deepcopy(existing_row))
        elif fresh_row is not None:
            kept.append(copy.deepcopy(fresh_row))

        # Later today hours — fresh only.
        for key, row in sorted(fresh_by_key.items(), key=lambda x: (x[0][0], x[0][1])):
            plan_date, hour = key
            if plan_date == today_str and hour > from_hour:
                kept.append(copy.deepcopy(row))
            elif plan_date > today_str:
                kept.append(copy.deepcopy(row))
    else:
        # Boundary is tomorrow (or later) — keep all today from SQLite, fresh after.
        for key, row in sorted(fresh_by_key.items(), key=lambda x: (x[0][0], x[0][1])):
            if key[0] >= from_date:
                if key[0] == from_date and key[1] < from_hour:
                    continue
                if key[0] == from_date and key[1] == from_hour:
                    existing_row = existing_by_key.get(key)
                    if existing_row is not None:
                        kept.append(
                            _merge_hour_from_quarter(existing_row, row, from_q=from_q),
                        )
                    else:
                        kept.append(copy.deepcopy(row))
                else:
                    kept.append(copy.deepcopy(row))

    # Deduplicate by key (stable last-write) then sort.
    by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for row in kept:
        by_key[_row_key(row)] = row
    kept = [by_key[k] for k in sorted(by_key.keys(), key=lambda x: (x[0], x[1]))]

    result["rows"] = kept
    result["has_history_rows"] = bool(result["history_rows"])
    result["today_date"] = today_str
    result["plan_from_hour"] = now.hour

    today_plan = [r for r in kept if str(r.get("plan_date") or "") == today_str]
    result["totals"] = compute_plan_totals(result["history_rows"] + today_plan)
    log.info(
        "Config replan splice — fresh from %s %02d:q%d onward",
        from_date,
        from_hour,
        from_q,
    )
    return result


def _merge_hour_from_quarter(
    existing_row: dict[str, Any],
    incoming_row: dict[str, Any],
    *,
    from_q: int,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """Keep completed/from_actual (and optionally q < from_q); rest from incoming.

    Meter actuals: prefer *incoming* when it also marks ``from_actual`` (Influx
    refresh / datafix). Otherwise keep existing actuals (I7). Locked
    timer/action stay as stored (empty, Chg, or Dis).
    """
    merged = copy.deepcopy(incoming_row)
    existing_timer = str(existing_row.get("timer_schedule") or "")
    if existing_row.get("timer_schedule_manual"):
        merged["timer_schedule"] = existing_timer
        merged["action"] = existing_row.get("action", "")
        merged["hour_labels_locked"] = True
        merged["timer_schedule_manual"] = True
    elif existing_row.get("hour_labels_locked"):
        merged["timer_schedule"] = existing_timer
        merged["action"] = existing_row.get("action", "")
        merged["hour_labels_locked"] = True

    eq = _ensure_q15_length(list(existing_row.get("q15") or []))
    iq = _ensure_q15_length(list(incoming_row.get("q15") or []))
    out: list[dict[str, Any]] = []
    for q in range(Q15_PER_HOUR):
        if _q15_slot_actual(iq[q]):
            # Fresh Influx / datafix actual wins over a stale frozen actual.
            out.append(copy.deepcopy(iq[q]))
        elif _q15_slot_actual(eq[q]) or q < from_q:
            slot = copy.deepcopy(eq[q])
            if q < from_q and not _q15_slot_actual(eq[q]) and iq[q].get("soc") is not None:
                slot["soc"] = copy.deepcopy(iq[q]["soc"])
            out.append(slot)
        else:
            out.append(copy.deepcopy(iq[q]))
    apply_q15_physics_to_row(merged, out)
    return merged


def _merge_current_hour_future_quarters(
    existing_row: dict[str, Any],
    incoming_row: dict[str, Any],
    *,
    now: datetime,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """Keep Influx-frozen quarters; open / lagging quarters from incoming.

    At :30 q0–q1 are freeze-ready (:00-:30); q2+ take the incoming plan until
    later ticks when their Influx 10-min windows have settled.
    """
    fo, fq = freeze_ready_quarter_tick(now)
    from_q = (fq + 1) if (fo == 0 and fq >= 0) else 0
    return _merge_hour_from_quarter(
        existing_row, incoming_row, from_q=from_q, cfg=cfg,
    )


def _in_progress_quarter(now: datetime) -> int:
    """Return 0..3 index of the quarter currently in progress."""
    return min(Q15_PER_HOUR - 1, max(0, now.minute // 15))



def _absorb_incoming_q15_actuals(dst: dict[str, Any], src: dict[str, Any]) -> bool:
    """Copy Influx-frozen q15 from *src* onto still-unfrozen slots in *dst*.

    Timer Schedule / Action stay on *dst*. Used by write_plan guard so a
    just-finalized q3 is not discarded when past hours are otherwise immutable.
    """
    dq = _ensure_q15_length(list(dst.get("q15") or []))
    # Only fill gaps after a partial freeze; do not replace untouched history.
    if not any(_q15_slot_actual(s) for s in dq):
        return False
    sq = _ensure_q15_length(list(src.get("q15") or []))
    changed = False
    for q in range(Q15_PER_HOUR):
        if _q15_slot_actual(dq[q]):
            continue
        if not _q15_slot_actual(sq[q]):
            continue
        dq[q] = copy.deepcopy(sq[q])
        changed = True
    if changed:
        dst["q15"] = dq
        apply_q15_physics_to_row(dst, dq)
    return changed


def _rce_q15_incomplete(row: dict[str, Any] | None) -> bool:
    qs = list((row or {}).get("rce_q15") or [])
    return len(qs) < Q15_PER_HOUR or any(v is None for v in qs[:Q15_PER_HOUR])


def _absorb_incoming_rce(
    dst: dict[str, Any],
    src: dict[str, Any],
    cfg: dict | None = None,
) -> bool:
    """Fill missing RCE on a frozen hour from this tick's plan row."""
    src_q = list(src.get("rce_q15") or [])
    while len(src_q) < Q15_PER_HOUR:
        src_q.append(None)
    dst_q = list(dst.get("rce_q15") or [])
    while len(dst_q) < Q15_PER_HOUR:
        dst_q.append(None)
    new_q: list[float | None] = []
    changed = False
    for i in range(Q15_PER_HOUR):
        if dst_q[i] is not None:
            new_q.append(dst_q[i])
            continue
        if src_q[i] is None:
            new_q.append(None)
            continue
        new_q.append(round(float(src_q[i]), 4))
        changed = True
    if not changed:
        return False
    dst["rce_q15"] = new_q
    vals = [float(v) for v in new_q if v is not None]
    dst["rce_price"] = round(sum(vals) / len(vals), 4) if vals else None
    slots = dst.get("q15")
    if isinstance(slots, list):
        for i, slot in enumerate(slots[:Q15_PER_HOUR]):
            if isinstance(slot, dict) and slot.get("rce") is None and new_q[i] is not None:
                slot["rce"] = new_q[i]
    from .grid_config import merge_grid_defaults
    refresh_row_grid_cash(dst, cfg if cfg is not None else merge_grid_defaults({}))
    return True


def _keep_rce_if_incoming_empty(dst: dict[str, Any], existing: dict[str, Any]) -> None:
    """Keep stored RCE when the fresh plan row has no prices this tick."""
    if not _rce_q15_incomplete(dst):
        return
    src_q = list(existing.get("rce_q15") or [])
    if not any(v is not None for v in src_q):
        return
    _absorb_incoming_rce(dst, existing)


def guard_future_quarters_on_write(
    incoming: dict[str, Any],
    existing: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Hard gate for plan_latest: frozen past stays; only open future may change.

    Same Warsaw day with an existing plan:
      - past hours in history_rows stay as frozen (timer/action/energy)
      - exception: absorb newly frozen Influx q15 into still-unfrozen slots
        (just-completed tick / recovered q3) — never rewrite from_actual
      - current hour: completed/from_actual q15 kept; open q15 from incoming
      - future hours / tomorrow: taken from incoming
    Empty SQLite or new calendar day: deep-copy of *incoming* (first seed only).

    Boundary hour is max(wall clock, incoming.plan_from_hour) so a straddle
    refresh that finishes after :00 cannot drop the completed hour (and its
    Timer Schedule) before it is written to SQLite.
    """
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    current_hour = _effective_plan_boundary_hour(incoming, now)
    if current_hour > int(now.hour):
        log.warning(
            "write_plan guard — sim crossed hour boundary (%02d -> %02d); "
            "promoting gap into history",
            now.hour,
            current_hour,
        )
    result = copy.deepcopy(incoming)

    if existing is None or str(existing.get("today_date") or "") != today_str:
        return result

    history = copy.deepcopy(existing.get("history_rows") or [])
    _strip_blended_flags(history)

    for row in existing.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        plan_date = str(row.get("plan_date") or "")
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if plan_date != today_str or hour < 0 or hour >= current_hour:
            continue
        if _history_has(history, today_str, hour):
            continue
        history.append(_as_history_row(row))

    blocked = 0
    for src in (result.get("history_rows"), result.get("rows")):
        for row in src or []:
            plan_date = str(row.get("plan_date") or "")
            try:
                hour = int(row.get("hour", -1))
            except (TypeError, ValueError):
                continue
            if plan_date != today_str or hour < 0 or hour >= current_hour:
                continue
            if _history_has(history, today_str, hour):
                blocked += 1
                # Keep past-hour immutability, but absorb newly frozen Influx q15
                # (e.g. recovered q3 after a delayed :00 tick).
                for hrow in history:
                    if _row_key(hrow) == (plan_date, hour):
                        _absorb_incoming_q15_actuals(hrow, row)
                        _absorb_incoming_rce(hrow, row)
                        break
                continue
            history.append(_as_history_row(row))

    if blocked:
        log.warning(
            "write_plan guard — blocked overwrite of %d past hour(s) before %02d:00",
            blocked,
            current_hour,
        )

    _sort_history(history)

    existing_current = _find_row(existing.get("rows") or [], today_str, current_hour)

    live_rows: list[dict[str, Any]] = []
    stripped = 0
    for row in result.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        plan_date = str(row.get("plan_date") or "")
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if plan_date == today_str and hour < current_hour:
            stripped += 1
            continue
        if plan_date == today_str and hour == current_hour and existing_current is not None:
            live_rows.append(
                _merge_current_hour_future_quarters(
                    existing_current, row, now=now, cfg=None,
                ),
            )
        else:
            live_rows.append(row)

    if stripped:
        log.warning(
            "write_plan guard — stripped %d past hour(s) from rows before %02d:00",
            stripped,
            current_hour,
        )

    result["history_rows"] = history
    result["rows"] = live_rows
    result["has_history_rows"] = bool(history)
    result["today_date"] = today_str
    # Keep plan_from aligned with the boundary used for promote/strip.
    try:
        result["plan_from_hour"] = max(int(result.get("plan_from_hour") or 0), current_hour)
    except (TypeError, ValueError):
        result["plan_from_hour"] = current_hour

    today_plan = [
        r for r in live_rows
        if str(r.get("plan_date") or "") == today_str
    ]
    result["totals"] = compute_plan_totals(history + today_plan)
    return result


# Alias for guard_future_quarters_on_write.
guard_past_hours_on_write = guard_future_quarters_on_write


def attach_immutable_history(
    result: dict[str, Any],
    existing: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> None:
    """Keep past hours from SQLite; full rebuild may only refresh current+future.

    Same calendar day: *history_rows* are copied from *existing* and never replaced
    by a fresh Influx rebuild. Hours still sitting in *existing.rows* that are now
    before the current clock hour are promoted into history (append-only).

    New day / empty SQLite: keep *result.history_rows* as a one-shot seed (meters
    with empty timers from the fresh sim). No preserve/fill of Timer Schedule.
    """
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    current_hour = now.hour
    # The fresh sim may have crossed an hour boundary while it was computed
    # (rows already start at now.hour+1). Use the later boundary so the hour
    # in between is promoted into history.
    try:
        sim_from_hour = int(result.get("plan_from_hour"))
    except (TypeError, ValueError):
        sim_from_hour = current_hour
    if sim_from_hour > current_hour:
        log.warning(
            "Sim crossed hour boundary during rebuild (%02d -> %02d) — promoting the gap",
            current_hour,
            sim_from_hour,
        )
        current_hour = sim_from_hour

    if existing is not None and str(existing.get("today_date") or "") == today_str:
        history = copy.deepcopy(existing.get("history_rows") or [])
        _strip_blended_flags(history)
        for row in existing.get("rows") or []:
            if row.get("start") == "TOTAL":
                continue
            plan_date = str(row.get("plan_date") or "")
            try:
                hour = int(row.get("hour", -1))
            except (TypeError, ValueError):
                continue
            if plan_date != today_str or hour < 0 or hour >= current_hour:
                continue
            if _history_has(history, today_str, hour):
                continue
            history.append(_as_history_row(row))
        # Heal holes (hours lost to earlier restarts/races) from fresh meter rows.
        _backfill_history_from_meters(
            history,
            result.get("history_rows"),
            today_str=today_str,
            current_hour=current_hour,
        )
        log.info(
            "Full rebuild — history attached from SQLite (%d rows)", len(history),
        )
    else:
        history = copy.deepcopy(result.get("history_rows") or [])
        _strip_blended_flags(history)
        log.info(
            "Full rebuild — history seeded from meters (%d rows, existing=%s)",
            len(history),
            "none" if existing is None else str(existing.get("today_date")),
        )

    _sort_history(history)

    live_rows: list[dict[str, Any]] = []
    for row in result.get("rows") or []:
        if row.get("start") == "TOTAL":
            continue
        plan_date = str(row.get("plan_date") or "")
        try:
            hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if plan_date == today_str and hour < current_hour:
            continue
        live_rows.append(row)

    result["history_rows"] = history
    result["rows"] = live_rows
    result["has_history_rows"] = bool(history)
    result["today_date"] = today_str
    result["plan_from_hour"] = current_hour

    today_plan = [
        r for r in live_rows
        if str(r.get("plan_date") or "") == today_str
    ]
    result["totals"] = compute_plan_totals(history + today_plan)
