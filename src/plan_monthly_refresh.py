"""Daily / manual refresh of SQLite month_history and deposit total."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .influxdb import now_warsaw
from .plan_deposits import (
    DEPOSIT_START_MONTH,
    iter_months,
    open_month_id,
    run_deposit_cascade,
)
from .plan_monthly_history import build_month_history
from .plan_simulation import backfill_all_stored_ea_rce, reprice_all_stored_ea_g12
from .sqlite_store import (
    load_month_history,
    read_cached_deposit_total,
    read_month_history_daily_date,
    save_month_history,
    write_cached_deposit_total,
    write_month_history_daily_date,
)

log = logging.getLogger(__name__)


def deposit_total_needs_refresh(today: date | None = None) -> bool:
    """True when calendar month rolled since last cached deposit total."""
    today = today or now_warsaw().date()
    cached = read_cached_deposit_total()
    if cached is None:
        return True
    as_of = cached.get("as_of_month")
    return as_of != open_month_id(today)


async def _ensure_month_history_through(
    cfg: dict,
    open_month: str,
) -> list[str]:
    """Build any missing closed/open months so cascade can draw imports correctly.

    Without this, a missing June history leaves June's deposit_initial in the pool
    while skipping June's import draw — Energy Deposit Total becomes a naive sum.
    """
    rebuilt: list[str] = []
    for month_id in iter_months(DEPOSIT_START_MONTH, open_month):
        if load_month_history(month_id) is not None:
            continue
        payload = await build_month_history(month_id, cfg)
        if payload.get("error"):
            log.warning("month_history ensure failed %s: %s", month_id, payload["error"])
            continue
        save_month_history(month_id, payload)
        rebuilt.append(month_id)
    return rebuilt


async def refresh_open_month_history(
    cfg: dict,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Rebuild open month from Influx, replay deposit cascade, cache deposit total."""
    today = today or now_warsaw().date()
    open_month = open_month_id(today)
    if open_month < DEPOSIT_START_MONTH:
        return {"ok": True, "skipped": True, "reason": "before_deposit_start"}

    ensured = await _ensure_month_history_through(cfg, open_month)
    payload = await build_month_history(open_month, cfg)
    if payload.get("error"):
        return {"ok": False, "error": payload["error"], "month": open_month}

    _, deposit_total = run_deposit_cascade(open_month, payload, today=today)
    write_cached_deposit_total(deposit_total, open_month)
    write_month_history_daily_date(today.isoformat())
    log.info(
        "month_history daily refresh %s deposit_total=%.2f ensured=%s",
        open_month,
        deposit_total,
        ensured,
    )
    return {
        "ok": True,
        "month": open_month,
        "deposit_total": deposit_total,
        "days": len(payload.get("rows") or []),
        "months_ensured": ensured,
    }


async def rebuild_all_month_history(cfg: dict, *, today: date | None = None) -> dict[str, Any]:
    """Full rebuild from Influx for every month, then replay deposit cascade."""
    today = today or now_warsaw().date()
    open_month = open_month_id(today)
    if open_month < DEPOSIT_START_MONTH:
        return {"ok": False, "error": "before deposit start month"}

    months = iter_months(DEPOSIT_START_MONTH, open_month)
    rebuilt: list[str] = []
    errors: list[str] = []

    for month_id in months:
        payload = await build_month_history(month_id, cfg)
        if payload.get("error"):
            errors.append(f"{month_id}: {payload['error']}")
            continue
        save_month_history(month_id, payload)
        rebuilt.append(month_id)

    target_payload = load_month_history(open_month)
    if target_payload is None:
        return {"ok": False, "error": "open month missing after rebuild", "errors": errors}

    _, deposit_total = run_deposit_cascade(open_month, target_payload, today=today)
    write_cached_deposit_total(deposit_total, open_month)
    write_month_history_daily_date(today.isoformat())
    ea_g12 = reprice_all_stored_ea_g12(cfg)
    ea_rce = backfill_all_stored_ea_rce(cfg)
    log.info(
        "month_history full rebuild through %s deposit_total=%.2f ea_archives=%s ea_rce=%s",
        open_month,
        deposit_total,
        ea_g12.get("archives_repriced"),
        ea_rce.get("archives_filled"),
    )
    return {
        "ok": True,
        "months_rebuilt": rebuilt,
        "deposit_total": deposit_total,
        "errors": errors,
        "ea_g12": ea_g12,
        "ea_rce": ea_rce,
    }


async def ensure_deposit_total_current(cfg: dict, *, today: date | None = None) -> float:
    """Refresh when calendar month changed or cache missing; else return cached."""
    today = today or now_warsaw().date()
    if deposit_total_needs_refresh(today):
        result = await refresh_open_month_history(cfg, today=today)
        if result.get("ok") and result.get("deposit_total") is not None:
            return float(result["deposit_total"])
    cached = read_cached_deposit_total()
    if cached is not None:
        return float(cached["deposit_total"])
    result = await refresh_open_month_history(cfg, today=today)
    return float(result.get("deposit_total") or 0.0)


async def maybe_run_daily_month_history(cfg: dict, *, today: date | None = None) -> None:
    """Once per Warsaw calendar day: append today's actuals into open month."""
    today = today or now_warsaw().date()
    today_s = today.isoformat()
    if read_month_history_daily_date() == today_s:
        return
    await refresh_open_month_history(cfg, today=today)
