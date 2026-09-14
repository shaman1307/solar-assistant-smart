"""
Work mode scheduler — SRNE grid export requires On-grid during timed discharge hours.

At :00 Europe/Warsaw: On-grid when Timer Schedule present (except charge-grid) or SOC is 100%.
Charge-grid hours: Limit power to home load + paired UPS/home battery (repair desync),
then SA timer write enables grid charge.
At :00/:15/:30/:45: Limit power to home load when the current hour has no Timer
Schedule, or a planned discharge has ended.
"""

from __future__ import annotations

import logging
from typing import Any

from . import sa_client
from .config import load_config
from .influxdb import now_warsaw
from .sqlite_store import read_plan
from .timer_plan import (
    ACTION_CHARGE_GRID,
    GRID_EXPORT_ACTION_MIN_KWH,
    hour_has_timer_schedule,
    normalize_action,
    plan_row_grid_export_kwh,
    sa_discharge_slot_active_at,
    timer_charge_active_at,
    timer_discharge_active_at,
    timer_discharge_end_due,
)

log = logging.getLogger(__name__)

SOC_FULL_PCT = 100.0

_last_work_mode_sync: dict[str, Any] = {
    "ran_at": None,
    "phase": None,
    "hour": None,
    "timer_schedule": None,
    "grid_export_kwh": None,
    "production_kw": None,
    "discharge_end_hhmm": None,
    "work_mode_target": None,
    "work_mode_before": None,
    "work_mode_after": None,
    "skipped": False,
    "skip_reason": None,
    "ok": None,
    "error": None,
}


def get_last_work_mode_sync() -> dict[str, Any]:
    return dict(_last_work_mode_sync)


def on_grid_job_applied(status: dict[str, Any] | None) -> bool:
    """True when :00 On-grid job ran for discharge/SOC-full trigger in this slot.

    Stale On-grid left from a previous hour (empty Timer Schedule, no trigger here)
    does not block Limit home in the same :00 slot.
    """
    if not status:
        return False
    if not status.get("on_grid_trigger_this_slot"):
        return False
    if status.get("skipped") and status.get("skip_reason") == "already_set":
        return True
    return status.get("ok") is not False


def _smart_mode_enabled(cfg: dict) -> bool:
    return bool(cfg.get("smart_mode_enabled", False))


async def _plan_rows(cfg: dict) -> list[dict]:
    """Return plan rows from SQLite (sole source of truth)."""
    del cfg
    stored = read_plan()
    if not stored or not stored.get("rows"):
        log.warning("No plan in SQLite — work mode sync skipped")
        return []
    return stored["rows"]


def _hour_row(rows: list[dict], hour: int) -> dict[str, Any] | None:
    return next(
        (r for r in rows if r.get("hour") == hour and r.get("start") != "TOTAL"),
        None,
    )


async def _set_work_mode_if_needed(
    cfg: dict,
    *,
    target_mode: str,
    status: dict[str, Any],
    timer_txt: str,
) -> None:
    rules = await sa_client.get_rules(cfg)
    before = rules.get("work_mode")
    status["work_mode_before"] = before
    if before == target_mode:
        status["work_mode_after"] = before
        status["skipped"] = True
        status["skip_reason"] = "already_set"
        bdm_ok = await sa_client.ensure_paired_battery_discharge_mode(cfg, target_mode)
        rules_after = await sa_client.get_rules(cfg, fresh=True)
        status["battery_discharge_mode_after"] = rules_after.get("battery_discharge_mode")
        # Soft-fail BDM when already On-grid so timer can still apply.
        if target_mode == sa_client.WORK_MODE_ON_GRID:
            status["ok"] = True
            if not bdm_ok:
                status["error"] = (
                    "SA did not confirm paired battery discharge mode within "
                    f"{int(sa_client.BATTERY_DISCHARGE_VERIFY_TIMEOUT_S)}s "
                    "(soft-fail; On-grid already set)"
                )
        else:
            status["ok"] = bdm_ok
            if not bdm_ok:
                status["error"] = (
                    "SA did not confirm paired battery discharge mode within "
                    f"{int(sa_client.BATTERY_DISCHARGE_VERIFY_TIMEOUT_S)}s"
                )
        log.info(
            "Work mode %s skipped — already %s (hour %02d, timer=%s); "
            "battery discharge %s",
            status["phase"],
            target_mode,
            status["hour"],
            timer_txt,
            "ok" if bdm_ok else "FAILED",
        )
        return

    log.info(
        "Work mode %s — hour %02d timer=%s production=%.3f kW: %s → %s",
        status["phase"],
        status["hour"],
        timer_txt or "—",
        float(status.get("production_kw") or 0),
        before,
        target_mode,
    )
    # Ordered SA writes (timed flags are handled by hour_boundary_scheduler):
    #   export start → On-grid, then Battery Grid export (BDM soft-fail)
    #   export end / charge prepare → Limit home, then UPS and home
    if target_mode == sa_client.WORK_MODE_ON_GRID:
        ok = await sa_client.apply_export_start_modes(cfg)
    elif target_mode == sa_client.WORK_MODE_LIMIT_HOME_LOAD:
        ok = await sa_client.apply_home_modes(cfg)
    else:
        ok = await sa_client.set_work_mode(cfg, target_mode)
    status["ok"] = ok
    rules_after = await sa_client.get_rules(cfg, fresh=True)
    status["work_mode_after"] = rules_after.get("work_mode")
    status["battery_discharge_mode_after"] = rules_after.get("battery_discharge_mode")
    if not ok:
        status["error"] = (
            f"SA did not confirm work mode or paired battery discharge mode "
            f"within {int(sa_client.WORK_MODE_VERIFY_TIMEOUT_S)}s"
        )


async def run_work_mode_hour_start(now=None) -> dict[str, Any]:
    """:00 — On-grid when Timer Schedule (not charge-grid) or SOC is 100%."""
    global _last_work_mode_sync

    now = now or now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": "start",
        "hour": hour,
        "timer_schedule": None,
        "grid_export_kwh": None,
        "production_kw": None,
        "discharge_end_hhmm": None,
        "work_mode_target": sa_client.WORK_MODE_ON_GRID,
        "work_mode_before": None,
        "work_mode_after": None,
        "on_grid_trigger_this_slot": False,
        "skipped": False,
        "skip_reason": None,
        "ok": None,
        "error": None,
    }

    try:
        cfg = load_config()
        if not _smart_mode_enabled(cfg):
            status["skipped"] = True
            status["skip_reason"] = "smart_mode_disabled"
            status["ok"] = None
            _last_work_mode_sync = status
            return status

        rows = await _plan_rows(cfg)
        row = _hour_row(rows, hour)
        if not row:
            status["skipped"] = True
            status["skip_reason"] = "no_hour_row"
            status["ok"] = True
            _last_work_mode_sync = status
            return status

        timer_txt = str(row.get("timer_schedule") or "").strip()
        status["timer_schedule"] = timer_txt or None
        status["grid_export_kwh"] = round(plan_row_grid_export_kwh(row), 3)

        metrics = await sa_client.get_live_metrics(cfg)
        live_soc = float(metrics.get("battery_soc", 0.0))
        soc_full = live_soc >= SOC_FULL_PCT

        has_timer = hour_has_timer_schedule(rows, hour)
        charge_grid = normalize_action(row.get("action") or "") == ACTION_CHARGE_GRID
        # Timed grid charge needs Limit home + UPS/home battery pair (not On-grid
        # export pair). Repair desync before SA timer write.
        if charge_grid:
            status["work_mode_target"] = sa_client.WORK_MODE_LIMIT_HOME_LOAD
            status["on_grid_trigger_this_slot"] = False
            status["charge_grid_prepare"] = True
            await _set_work_mode_if_needed(
                cfg,
                target_mode=sa_client.WORK_MODE_LIMIT_HOME_LOAD,
                status=status,
                timer_txt=timer_txt,
            )
            _last_work_mode_sync = status
            return status

        timer_on_grid = has_timer and not charge_grid
        on_grid_trigger = timer_on_grid or soc_full
        status["on_grid_trigger_this_slot"] = on_grid_trigger

        if not on_grid_trigger:
            status["skipped"] = True
            status["skip_reason"] = "no_on_grid_trigger"
            status["ok"] = True
            log.info(
                "Work mode start skipped — hour %02d (timer=%s action=%s soc=%.1f%%)",
                hour,
                timer_txt or "—",
                row.get("action"),
                live_soc,
            )
            _last_work_mode_sync = status
            return status

        await _set_work_mode_if_needed(
            cfg,
            target_mode=sa_client.WORK_MODE_ON_GRID,
            status=status,
            timer_txt=timer_txt,
        )
        _last_work_mode_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_work_mode_sync = status
        log.exception("Work mode start job failed")
        return status


def limit_home_due_for_timer(
    timer_txt: str,
    now,
    *,
    plan_hour: int,
    plan_row: dict[str, Any] | None = None,
    sa_rules: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Whether Limit home applies for the current hour's Timer Schedule cell."""
    timer_txt = str(timer_txt or "").strip()
    if timer_txt:
        if timer_charge_active_at(timer_txt, now):
            return False, None
        if timer_discharge_active_at(timer_txt, now):
            return False, None
        return timer_discharge_end_due(timer_txt, now, plan_hour=plan_hour)

    # Plan cell empty — do not cut discharge while SA slot or in-hour export is active.
    if sa_rules and sa_discharge_slot_active_at(sa_rules, now):
        return False, None
    if plan_row and plan_row_grid_export_kwh(plan_row) > GRID_EXPORT_ACTION_MIN_KWH:
        return False, None
    return True, None


async def run_work_mode_limit_home(now=None) -> dict[str, Any]:
    """:00/:15/:30/:45 — Limit home when timer empty or discharge ended."""
    global _last_work_mode_sync

    now = now or now_warsaw()
    hour = now.hour
    ran_at = now.strftime("%Y-%m-%d %H:%M:%S")
    status: dict[str, Any] = {
        "ran_at": ran_at,
        "phase": "limit_home",
        "hour": hour,
        "timer_schedule": None,
        "grid_export_kwh": None,
        "production_kw": None,
        "discharge_end_hhmm": None,
        "limit_due": False,
        "work_mode_target": sa_client.WORK_MODE_LIMIT_HOME_LOAD,
        "work_mode_before": None,
        "work_mode_after": None,
        "skipped": False,
        "skip_reason": None,
        "ok": None,
        "error": None,
    }

    try:
        cfg = load_config()
        if not _smart_mode_enabled(cfg):
            status["skipped"] = True
            status["skip_reason"] = "smart_mode_disabled"
            status["ok"] = None
            _last_work_mode_sync = status
            return status

        metrics = await sa_client.get_live_metrics(cfg)
        pv_kw = float(metrics.get("pv_power", 0.0))
        status["production_kw"] = round(pv_kw, 3)

        rows = await _plan_rows(cfg)
        row = _hour_row(rows, hour)
        if not row:
            status["skipped"] = True
            status["skip_reason"] = "no_hour_row"
            status["ok"] = True
            log.info("Work mode limit_home skipped — hour %02d no plan row", hour)
            _last_work_mode_sync = status
            return status

        timer_txt = str(row.get("timer_schedule") or "").strip()
        rules = await sa_client.get_rules(cfg)
        is_due, due_end = limit_home_due_for_timer(
            timer_txt,
            now,
            plan_hour=hour,
            plan_row=row,
            sa_rules=rules,
        )
        if not is_due:
            status["skipped"] = True
            status["skip_reason"] = "discharge_not_ended"
            status["ok"] = True
            log.info("Work mode limit_home skipped — hour %02d discharge not ended yet", hour)
            _last_work_mode_sync = status
            return status

        status["limit_due"] = True
        status["discharge_end_hhmm"] = due_end
        status["timer_schedule"] = timer_txt or None
        status["grid_export_kwh"] = round(plan_row_grid_export_kwh(row), 3)

        await _set_work_mode_if_needed(
            cfg,
            target_mode=sa_client.WORK_MODE_LIMIT_HOME_LOAD,
            status=status,
            timer_txt=timer_txt,
        )
        _last_work_mode_sync = status
        return status
    except Exception as exc:
        status["error"] = str(exc)
        status["ok"] = False
        _last_work_mode_sync = status
        log.exception("Work mode limit_home job failed")
        return status
