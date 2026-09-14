"""
Hour boundary SA sync — :00, :15/:30/:45 Europe/Warsaw.

Writes to SA from the current hour's Energy arbitrage Timer Schedule cell
(e.g. Dis 19:30-20:00 6.51kW cap16%), not merged multi-hour proposed_schedule.

Mode write order (SRNE):
  Export start: Work mode On-grid → Battery Grid export → Timed discharge on
  Export end:   Timed discharge off → Work mode Limit home → Battery UPS/home
  Charge start: Limit home + UPS/home → Timed charge on
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import sa_client
from .config import load_config
from .influxdb import now_warsaw
from .sqlite_store import read_plan
from .timer_plan import (
    build_sa_schedule_from_hour_row,
    hour_has_timer_schedule,
    timer_charge_active_at,
    timer_charge_end_due,
    timer_discharge_active_at,
)
from .work_mode_scheduler import (
    limit_home_due_for_timer,
    on_grid_job_applied,
    run_work_mode_hour_start,
    run_work_mode_limit_home,
)

log = logging.getLogger(__name__)

_last_hour_boundary_sync: dict[str, Any] = {
    "ran_at": None,
    "phase": None,
    "minute": None,
    "hour": None,
    "timer_schedule": None,
    "work_mode": None,
    "work_mode_limit": None,
    "timer_sync": None,
    "timed_power": None,
    "ok": None,
    "error": None,
}


def get_last_hour_boundary_sync() -> dict[str, Any]:
    return dict(_last_hour_boundary_sync)


def _smart_mode_enabled(cfg: dict) -> bool:
    return bool(cfg.get("smart_mode_enabled", False))


async def _plan_rows(cfg: dict) -> list[dict]:
    """Return plan rows from SQLite (sole source of truth)."""
    del cfg
    stored = read_plan()
    if not stored or not stored.get("rows"):
        log.warning("No plan in SQLite — SA timer sync skipped")
        return []
    return stored["rows"]


def _sa_missing_active_plan_timer(
    timer_txt: str,
    now,
    rules: dict[str, Any],
) -> bool:
    """True when plan has an active Chg/Dis window but SA is not running it."""
    now_min = now.hour * 60 + now.minute

    def _slot_covers(slot: dict[str, Any] | None) -> bool:
        if not slot:
            return False
        try:
            fh, fm = str(slot.get("from") or "00:00").split(":")
            th, tm = str(slot.get("to") or "00:00").split(":")
            start = int(fh) * 60 + int(fm)
            end = int(th) * 60 + int(tm)
        except (TypeError, ValueError):
            return False
        if start == 0 and end == 0:
            return False
        return start <= now_min < end

    if timer_charge_active_at(timer_txt, now):
        if not rules.get("timed_charge_enabled"):
            return True
        return not _slot_covers((rules.get("charge_slots") or [{}])[0])
    if timer_discharge_active_at(timer_txt, now):
        if not rules.get("timed_discharge_enabled"):
            return True
        return not _slot_covers((rules.get("discharge_slots") or [{}])[0])
    return False


async def _clear_stale_timed_charge(
    cfg: dict,
    *,
    reason: str = "empty plan timer",
) -> dict[str, Any]:
    """Turn off Timed charge checkbox only (leave slot times / work mode alone)."""
    status: dict[str, Any] = {"ok": None, "error": None, "cleared": False}
    try:
        rules = await sa_client.get_rules(cfg)
    except Exception as exc:
        status["ok"] = False
        status["error"] = str(exc)
        return status
    if not rules.get("timed_charge_enabled"):
        status["ok"] = True
        status["skipped"] = True
        status["skip_reason"] = "timed_charge_already_off"
        return status
    ok = await sa_client.set_timed_power_flags(
        cfg,
        timed_charge_enabled=False,
        timed_discharge_enabled=bool(rules.get("timed_discharge_enabled")),
    )
    status["ok"] = ok
    status["cleared"] = bool(ok)
    if not ok:
        status["error"] = "SA timed charge clear failed"
    else:
        log.info("SA timer sync — cleared timed_charge (%s)", reason)
    return status


async def _sync_timer_from_hour_row(
    cfg: dict,
    rows: list[dict],
    hour: int,
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "hour": hour,
        "timer_schedule": None,
        "skipped": False,
        "skip_reason": None,
        "ok": None,
        "error": None,
    }
    if not hour_has_timer_schedule(rows, hour):
        status["skipped"] = True
        status["skip_reason"] = "empty_timer_schedule"
        clear_status = await _clear_stale_timed_charge(cfg)
        status["stale_clear"] = clear_status
        status["ok"] = clear_status.get("ok") is not False
        return status

    row = next(r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL")
    timer_txt = str(row.get("timer_schedule") or "").strip()
    status["timer_schedule"] = timer_txt

    if not timer_txt:
        status["skipped"] = True
        status["skip_reason"] = "empty_timer_schedule"
        clear_status = await _clear_stale_timed_charge(cfg)
        status["stale_clear"] = clear_status
        status["ok"] = clear_status.get("ok") is not False
        return status

    # Write the plan timer as-is — never shift start times.
    rules = await sa_client.get_rules(cfg)
    metrics = await sa_client.get_live_metrics(cfg)
    live_soc = metrics.get("battery_soc")
    try:
        live_soc_pct = float(live_soc) if live_soc is not None else None
    except (TypeError, ValueError):
        live_soc_pct = None
    schedule = build_sa_schedule_from_hour_row(
        rows, hour, cfg, existing=rules, live_soc_pct=live_soc_pct,
    )
    if not schedule:
        status["skipped"] = True
        status["skip_reason"] = "unparsed_timer_schedule"
        status["ok"] = False
        status["error"] = f"Could not parse timer schedule: {timer_txt!r}"
        return status

    log.info("SA timer sync — hour %02d from row: %s", hour, timer_txt)
    ok = await sa_client.apply_hourly_schedule_to_sa(cfg, schedule)
    status["ok"] = ok
    if not ok:
        status["error"] = "SA timer write failed"
    return status


async def _clear_timed_power_flags(cfg: dict) -> dict[str, Any]:
    """Disable Timed charge / Timed discharge on SA (checkboxes only, no slots)."""
    status: dict[str, Any] = {"ok": None, "error": None}
    ok = await sa_client.set_timed_power_flags(
        cfg,
        timed_charge_enabled=False,
        timed_discharge_enabled=False,
    )
    status["ok"] = ok
    if not ok:
        status["error"] = "SA timed power flags write failed"
    return status


async def _peek_limit_home_due(
    cfg: dict,
    rows: list[dict],
    *,
    now,
    hour: int,
) -> tuple[bool, str | None, str]:
    """Whether Limit-home (export end) applies now. Returns (due, end_hhmm, timer_txt)."""
    row = next(
        (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
        None,
    )
    timer_txt = str(row.get("timer_schedule") or "").strip() if row else ""
    rules = await sa_client.get_rules(cfg)
    due, end = limit_home_due_for_timer(
        timer_txt,
        now,
        plan_hour=hour,
        plan_row=row,
        sa_rules=rules,
    )
    return due, end, timer_txt


async def run_hour_boundary_start(now=None) -> dict[str, Any]:
    """:00 — export start modes (WM→BDM) then timer; or end: timed off → Limit modes."""
    global _last_hour_boundary_sync

    now = now or now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": "start",
        "minute": now.minute,
        "hour": hour,
        "timer_schedule": None,
        "work_mode": None,
        "work_mode_limit": None,
        "timer_sync": None,
        "timed_power": None,
        "ok": None,
        "error": None,
    }

    try:
        cfg = load_config()
        if not _smart_mode_enabled(cfg):
            status["ok"] = None
            status["error"] = "smart_mode_disabled"
            _last_hour_boundary_sync = status
            return status

        rows = await _plan_rows(cfg)
        if hour_has_timer_schedule(rows, hour):
            row = next(r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL")
            status["timer_schedule"] = str(row.get("timer_schedule") or "").strip()

        status["work_mode"] = await run_work_mode_hour_start(now=now)
        status["timer_sync"] = await _sync_timer_from_hour_row(cfg, rows, hour)

        # Charge-grid prepare already set Limit home; do not run Limit-home again
        # (and never clear timed_charge while a charge window is active).
        charge_prepared = bool((status["work_mode"] or {}).get("charge_grid_prepare"))
        if on_grid_job_applied(status["work_mode"]) or charge_prepared:
            status["work_mode_limit"] = {
                "skipped": True,
                "skip_reason": (
                    "charge_grid_prepare" if charge_prepared else "on_grid_applied"
                ),
                "ok": True,
            }
        else:
            # Export end at :00: clear Timed discharge BEFORE Limit/UPS modes.
            due, _end, timer_txt = await _peek_limit_home_due(
                cfg, rows, now=now, hour=hour,
            )
            discharge_still_active = bool(
                timer_txt and timer_discharge_active_at(timer_txt, now),
            )
            if due and not discharge_still_active:
                status["timed_power"] = await _clear_timed_power_flags(cfg)
            limit_status = await run_work_mode_limit_home(now=now)
            status["work_mode_limit"] = limit_status

        wm = status["work_mode"]
        ts = status["timer_sync"]
        tp = status.get("timed_power")
        limit_wm = status.get("work_mode_limit") or {}

        if not hour_has_timer_schedule(rows, hour):
            status["ok"] = True
        else:
            status["ok"] = (wm.get("ok") is not False) and (ts.get("ok") is not False)

        if limit_wm.get("limit_due"):
            status["ok"] = status["ok"] and (limit_wm.get("ok") is not False)
            if tp is not None:
                status["ok"] = status["ok"] and (tp.get("ok") is not False)

        if status["ok"] is False:
            parts = []
            if wm.get("ok") is False:
                parts.append("work mode on-grid")
            if ts.get("ok") is False:
                parts.append("timer")
            if limit_wm.get("ok") is False:
                parts.append("work mode limit")
            if tp and tp.get("ok") is False:
                parts.append("timed power")
            status["error"] = "SA sync failed: " + ", ".join(parts)

        _last_hour_boundary_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_hour_boundary_sync = status
        log.exception("Hour boundary sync failed (start)")
        return status


async def run_hour_boundary_limit_home(now=None) -> dict[str, Any]:
    """:15/:30/:45 — end export: Timed off → Limit home → UPS/home battery.

    Also write the open Chg/Dis timer if the :00 sync was missed, so the inverter
    is not idle until the next :00.
    """
    global _last_hour_boundary_sync

    now = now or now_warsaw()
    status: dict[str, Any] = {
        "ran_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "limit_home",
        "minute": now.minute,
        "hour": now.hour,
        "timer_schedule": None,
        "work_mode": None,
        "work_mode_limit": None,
        "timer_sync": {
            "skipped": True,
            "skip_reason": "limit_home_no_timer_write",
            "ok": True,
        },
        "timed_power": None,
        "ok": None,
        "error": None,
    }

    try:
        cfg = load_config()
        if not _smart_mode_enabled(cfg):
            status["ok"] = None
            status["error"] = "smart_mode_disabled"
            _last_hour_boundary_sync = status
            return status

        rows = await _plan_rows(cfg)
        due, due_end, timer_txt = await _peek_limit_home_due(
            cfg, rows, now=now, hour=now.hour,
        )
        if timer_txt:
            status["timer_schedule"] = timer_txt

        # Recover missed :00 Chg/Dis write before Limit-home may clear flags.
        charge_active = bool(timer_txt and timer_charge_active_at(timer_txt, now))
        discharge_active = bool(
            timer_txt and timer_discharge_active_at(timer_txt, now),
        )
        if charge_active or discharge_active:
            rules = await sa_client.get_rules(cfg)
            if _sa_missing_active_plan_timer(timer_txt, now, rules):
                if charge_active:
                    # Limit home + UPS/home before enabling timed charge.
                    status["work_mode"] = await run_work_mode_hour_start(now=now)
                status["timer_sync"] = await _sync_timer_from_hour_row(
                    cfg, rows, now.hour,
                )
                log.info(
                    "SA timer sync — mid-quarter retry hour %02d (%s)",
                    now.hour,
                    "charge" if charge_active else "discharge",
                )

        # Charge end ≠ discharge end: work mode already Limit home; only untick
        # Timed charge (slot times may stay until the next hour sync).
        chg_due, chg_end = (
            timer_charge_end_due(timer_txt, now, plan_hour=now.hour)
            if timer_txt
            else (False, None)
        )
        if chg_due and not charge_active:
            status["timed_charge_clear"] = await _clear_stale_timed_charge(
                cfg, reason=f"charge ended {chg_end}",
            )
            if chg_end:
                status["charge_end_hhmm"] = chg_end

        if due:
            # 1) Clear Timed discharge checkbox before mode changes.
            status["timed_power"] = await _clear_timed_power_flags(cfg)
            # 2–3) Limit home work mode, then UPS/home battery (via apply_home_modes).
            limit_status = await run_work_mode_limit_home(now=now)
        else:
            status["timed_power"] = {
                "skipped": True,
                "skip_reason": "discharge_not_ended",
                "ok": True,
            }
            limit_status = await run_work_mode_limit_home(now=now)

        status["work_mode_limit"] = limit_status
        if status.get("work_mode") is None:
            status["work_mode"] = limit_status
        if limit_status.get("timer_schedule"):
            status["timer_schedule"] = limit_status["timer_schedule"]
        if due_end and limit_status.get("limit_due"):
            status.setdefault("discharge_end_hhmm", due_end)

        tp = status.get("timed_power") or {}
        tc = status.get("timed_charge_clear") or {}
        ts = status.get("timer_sync") or {}
        if limit_status.get("skipped") and not limit_status.get("limit_due"):
            status["ok"] = (
                (ts.get("ok") is not False)
                and (tc.get("ok") is not False)
            )
        else:
            status["ok"] = (
                (tp.get("ok") is not False)
                and (limit_status.get("ok") is not False)
                and (ts.get("ok") is not False)
                and (tc.get("ok") is not False)
            )
        if status["ok"] is False:
            parts = []
            if tp.get("ok") is False:
                parts.append("timed power")
            if tc.get("ok") is False:
                parts.append("timed charge clear")
            if limit_status.get("ok") is False:
                parts.append("work mode limit")
            if ts.get("ok") is False:
                parts.append("timer")
            status["error"] = "SA sync failed: " + ", ".join(parts)
        _last_hour_boundary_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_hour_boundary_sync = status
        log.exception("Hour boundary limit home failed")
        return status


def register_hour_boundary_jobs(scheduler: AsyncIOScheduler) -> None:
    """Limit-home at :15/:30/:45 runs from quarter_plan_refresh (after plan write)."""
    log.info(
        "Hour boundary SA sync: :00 export start WM→BDM→timer; "
        ":15/:30/:45 export end timed-off→Limit→UPS/home (after plan refresh).",
    )
