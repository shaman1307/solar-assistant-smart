"""EV charging API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import ev_charging as ev
from .. import forecast as forecast_mod
from ..config import load_config

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/api/ev-charging")
async def api_get_ev_charging() -> dict[str, Any]:
    cfg = load_config()
    return ev.api_payload(cfg)


@router.post("/api/ev-charging")
async def api_save_ev_charging(body: dict) -> dict[str, Any]:
    """Save EV session and refresh forecast load cache.

    Plan / solid SOC rebuild is done by the client via
    ``GET /api/simulation?refresh=1&unlock_plan_soc=1`` after this returns, so
    the checkbox stays responsive and EA updates in one controlled rebuild.
    """
    cfg = load_config()
    date_str = str(body.get("date") or "").strip()
    if not date_str:
        raise HTTPException(status_code=400, detail="date is required")
    try:
        session = ev.apply_session_update(
            date_str=date_str,
            slots={key: body.get(key) for key in ev.SLOT_KEYS},
            cfg=cfg,
            edited_slot=body.get("edited_slot"),
            edited_side=body.get("edited_side"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Update forecast load cache before returning so the chart sees EV immediately.
    await forecast_mod.refresh_effective_forecast(cfg)
    summary = " ".join(
        "{key}={on} {start}-{end} {kw:.0f}kW".format(
            key=key,
            on=bool((session.get(key) or {}).get("enabled")),
            start=(session.get(key) or {}).get("start"),
            end=(session.get(key) or {}).get("end"),
            kw=float((session.get(key) or {}).get("power_kw") or 0),
        )
        for key in ev.SLOT_KEYS
    )
    log.info("EV charging updated for %s %s", date_str, summary)
    return {"status": "saved", "date": date_str, "session": session}
