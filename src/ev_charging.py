"""EV charging sessions — per-date plans, history for load de-trending, q15 profiles."""

from __future__ import annotations

import logging
import threading
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import BASE_DIR
from .sqlite_store import load_ev_store_json, migrate_ev_json_to_sqlite, save_ev_store_json
from .influxdb import now_warsaw

log = logging.getLogger(__name__)
_session_lock = threading.Lock()

Q15_PER_DAY = 96
HOURS_PER_DAY = 24
HISTORY_RETENTION_DAYS = 35

_STORE_PATH = BASE_DIR / "data" / "ev_charging.json"

# UI order. An end of 00:00 after a later start is the end of the same day (24:00).
SLOT_DEFAULTS: dict[str, dict[str, Any]] = {
    "night": {"enabled": False, "start": "00:00", "end": "04:00", "power_kw": 5.0},
    "extra": {"enabled": False, "start": "13:00", "end": "15:00", "power_kw": 5.0},
    "day": {"enabled": False, "start": "22:00", "end": "00:00", "power_kw": 5.0},
}
SLOT_KEYS = tuple(SLOT_DEFAULTS)


def store_path() -> Path:
    return _STORE_PATH


def default_session() -> dict[str, Any]:
    return {key: {**SLOT_DEFAULTS[key]} for key in SLOT_KEYS}


def load_store() -> dict[str, Any]:
    migrate_ev_json_to_sqlite()
    data = load_ev_store_json()
    if data is None:
        data = {"sessions": {}}
    if data.get("version") != 2:
        data.setdefault("sessions", {})
    return data


def save_store(data: dict[str, Any]) -> None:
    save_ev_store_json(data)


def max_power_kw(cfg: dict) -> float:
    return max(0.0, float((cfg.get("ev") or {}).get("max_power_kw") or 11.0))


def clamp_power_kw(value: float, cfg: dict) -> float:
    cap = max_power_kw(cfg)
    if cap <= 0:
        return 0.0
    return round(max(0.0, min(float(value), cap)), 2)


def parse_hhmm(value: str) -> int | None:
    try:
        parts = str(value).strip().split(":")
        if len(parts) < 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
        return h * 60 + m
    except (ValueError, TypeError):
        return None


def format_hhmm(total_min: int) -> str:
    """Clock time HH:MM on a 24h calendar day (1440 → 00:00)."""
    wrapped = max(0, int(total_min)) % MINUTES_PER_DAY
    return f"{wrapped // 60:02d}:{wrapped % 60:02d}"


Q15_STEP_MIN = 15
MINUTES_PER_DAY = 24 * 60


def slot_end_minutes(start_min: int, end_min: int) -> int:
    """End as minutes since 00:00; a 00:00 end after a later start is 24:00."""
    if end_min == 0 and start_min > 0:
        return MINUTES_PER_DAY
    return end_min


def _coerce_slot_end_after_start(start_min: int, end_min: int) -> int:
    """Minimum valid end: strictly after start, 15-minute grid, at most 24:00."""
    if end_min > start_min:
        return end_min
    return min(start_min + Q15_STEP_MIN, MINUTES_PER_DAY)


def _snap_up(minute: int) -> int:
    step = Q15_STEP_MIN
    return ((int(minute) + step - 1) // step) * step


def _snap_down(minute: int) -> int:
    return (int(minute) // Q15_STEP_MIN) * Q15_STEP_MIN


def slot_interval(start: str, end: str) -> tuple[int, int]:
    """Half-open [start, end) minutes. A 00:00 end after a later start is 24:00."""
    start_min = parse_hhmm(start)
    end_min = parse_hhmm(end)
    if start_min is None or end_min is None:
        return 0, Q15_STEP_MIN
    end_min = _coerce_slot_end_after_start(start_min, slot_end_minutes(start_min, end_min))
    return start_min, end_min


def _intervals_conflict(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when one slot reaches the 15-minute step of the other.

    A slot ending at 04:00 blocks the next one through 04:00; the next grid
    time is 04:15. A 00:00 end (24:00) does not meet a 00:00 start.
    """
    a0, a1 = a
    b0, b1 = b
    return a0 < b1 + Q15_STEP_MIN and b0 < a1 + Q15_STEP_MIN


def separate_edited_slot(session: dict[str, Any], edited_key: str, side: str) -> dict[str, Any]:
    """Move the edited slot off the other two. Other slots stay put."""
    if edited_key not in SLOT_KEYS or side not in ("start", "end"):
        return session
    intervals = {
        key: slot_interval(session[key]["start"], session[key]["end"])
        for key in SLOT_KEYS
        if key in session
    }
    if edited_key not in intervals:
        return session
    me0, me1 = intervals[edited_key]
    for _ in range(6):
        moved = False
        for key, (o0, o1) in intervals.items():
            if key == edited_key or not _intervals_conflict((me0, me1), (o0, o1)):
                continue
            if side == "end" and me0 < o0:
                new_end = _snap_down(o0 - 1)
                if new_end <= me0:
                    new_end = min(me0 + Q15_STEP_MIN, MINUTES_PER_DAY)
                if new_end != me1:
                    me1 = new_end
                    moved = True
            elif me0 < o1 + Q15_STEP_MIN and me0 + Q15_STEP_MIN > o0:
                new_start = min(_snap_up(o1 + 1), MINUTES_PER_DAY - Q15_STEP_MIN)
                if new_start != me0:
                    me0 = new_start
                    moved = True
            if me1 + Q15_STEP_MIN > o0 and me0 < o0:
                new_end = _snap_down(o0 - 1)
                if new_end != me1:
                    me1 = new_end
                    moved = True
            if me1 <= me0:
                me1 = min(me0 + Q15_STEP_MIN, MINUTES_PER_DAY)
                moved = True
            if me1 > MINUTES_PER_DAY:
                me1 = MINUTES_PER_DAY
                moved = True
        if not moved:
            break
    session[edited_key]["start"] = format_hhmm(me0)
    session[edited_key]["end"] = format_hhmm(me1)
    return session


def _normalize_slot(raw: dict | None, *, key: str, cfg: dict) -> dict[str, Any]:
    defaults = SLOT_DEFAULTS[key]
    src = raw if isinstance(raw, dict) else {}
    start = str(src.get("start") or defaults["start"])
    end = str(src.get("end") or defaults["end"])
    if parse_hhmm(start) is None:
        start = defaults["start"]
    if parse_hhmm(end) is None:
        end = defaults["end"]
    start_min = parse_hhmm(start)
    end_min = parse_hhmm(end)
    if start_min is not None and end_min is not None:
        end_min = slot_end_minutes(start_min, end_min)
        coerced_end = _coerce_slot_end_after_start(start_min, end_min)
        if coerced_end != end_min:
            end_min = coerced_end
            end = format_hhmm(end_min)
    power = clamp_power_kw(float(src.get("power_kw", defaults["power_kw"]) or 0), cfg)
    wants_enabled = bool(src.get("enabled", False))
    enabled = (
        wants_enabled
        and power > 0
        and start_min is not None
        and end_min is not None
        and end_min > start_min
    )
    return {
        "enabled": enabled,
        "start": start,
        "end": end,
        "power_kw": power if enabled else clamp_power_kw(float(src.get("power_kw", defaults["power_kw"]) or 0), cfg),
    }


def normalize_session(raw: dict | None, cfg: dict) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    return {key: _normalize_slot(src.get(key), key=key, cfg=cfg) for key in SLOT_KEYS}


def session_has_charging(session: dict[str, Any] | None) -> bool:
    if not session:
        return False
    for key in SLOT_KEYS:
        slot = session.get(key) or {}
        if slot.get("enabled") and float(slot.get("power_kw") or 0) > 0:
            return True
    return False


def _calendar_pair(now: datetime | None = None) -> tuple[str, str]:
    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return today_str, tomorrow_str


def _migrate_v1_to_v2(data: dict[str, Any], today_str: str) -> None:
    """Migrate per-date sessions dict into relative today/tomorrow + history."""
    old = data.pop("sessions", {}) or {}
    tomorrow_str = (
        datetime.strptime(today_str, "%Y-%m-%d").date() + timedelta(days=1)
    ).strftime("%Y-%m-%d")
    data.clear()
    data.update({
        "version": 2,
        "anchor_date": today_str,
        "today": old.get(today_str),
        "tomorrow": old.get(tomorrow_str),
        "history": {k: v for k, v in old.items() if k < today_str},
    })


def _do_one_rollover(data: dict[str, Any], cfg: dict, anchor_date_str: str) -> str:
    """Archive relative today; relative today ← tomorrow; tomorrow cleared."""
    anchor = datetime.strptime(anchor_date_str, "%Y-%m-%d").date()
    next_anchor = (anchor + timedelta(days=1)).strftime("%Y-%m-%d")

    today_sess = data.get("today")
    tomorrow_sess = data.get("tomorrow")
    history: dict[str, Any] = data.setdefault("history", {})

    if today_sess and session_has_charging(today_sess):
        history[anchor_date_str] = normalize_session(today_sess, cfg)

    if tomorrow_sess and session_has_charging(tomorrow_sess):
        data["today"] = normalize_session(tomorrow_sess, cfg)
    else:
        data["today"] = None

    data["tomorrow"] = None
    data["anchor_date"] = next_anchor
    return next_anchor


def _load_store_ready(cfg: dict, *, now: datetime | None = None) -> dict[str, Any]:
    """Load store, migrate v1, roll over once per calendar day change (00:00 Warsaw)."""
    now = now or now_warsaw()
    today_str, _ = _calendar_pair(now)
    data = load_store()
    if data.get("version") != 2:
        _migrate_v1_to_v2(data, today_str)
        save_store(data)

    anchor = str(data.get("anchor_date") or today_str)
    while anchor < today_str:
        _do_one_rollover(data, cfg, anchor)
        anchor = str(data["anchor_date"])
        save_store(data)

    if anchor != today_str:
        data["anchor_date"] = today_str
        save_store(data)

    return data


def _resolve_session_slot(
    data: dict[str, Any],
    date_str: str,
    *,
    today_str: str,
    tomorrow_str: str,
) -> dict[str, Any] | None:
    if date_str == today_str:
        return data.get("today")
    if date_str == tomorrow_str:
        return data.get("tomorrow")
    return (data.get("history") or {}).get(date_str)


def get_session(date_str: str, cfg: dict) -> dict[str, Any] | None:
    data = _load_store_ready(cfg)
    today_str, tomorrow_str = _calendar_pair()
    return _resolve_session_slot(
        data, date_str, today_str=today_str, tomorrow_str=tomorrow_str,
    )


def set_session(date_str: str, session: dict[str, Any], cfg: dict) -> dict[str, Any]:
    """Persist one calendar session. Serialize read-modify-write against concurrent POSTs."""
    normalized = normalize_session(session, cfg)
    today_str, tomorrow_str = _calendar_pair()

    if date_str == today_str:
        slot_key = "today"
    elif date_str == tomorrow_str:
        slot_key = "tomorrow"
    else:
        raise ValueError("EV charging can only be set for today or tomorrow")

    with _session_lock:
        data = _load_store_ready(cfg)
        data[slot_key] = normalized
        save_store(data)
        prune_old_sessions(cfg)
    return normalized


def get_session_for_ui(date_str: str, cfg: dict) -> dict[str, Any]:
    """UI state: stored plan for calendar date, or disabled defaults."""
    stored = get_session(date_str, cfg)
    if stored:
        return normalize_session(stored, cfg)
    return default_session()


def is_plannable_date(date_str: str, today_str: str, tomorrow_str: str) -> bool:
    return date_str in (today_str, tomorrow_str)


def is_history_date(date_str: str, today_str: str) -> bool:
    return date_str < today_str


def session_for_load_add(
    date_str: str,
    today_str: str,
    tomorrow_str: str,
    cfg: dict,
) -> dict[str, Any] | None:
    if not is_plannable_date(date_str, today_str, tomorrow_str):
        return None
    session = get_session(date_str, cfg)
    if not session_has_charging(session):
        return None
    return normalize_session(session, cfg)


def session_for_history_subtract(date_str: str, today_str: str, cfg: dict) -> dict[str, Any] | None:
    if not is_history_date(date_str, today_str):
        return None
    session = get_session(date_str, cfg)
    if not session_has_charging(session):
        return None
    return normalize_session(session, cfg)


def _slot_q15_energy(
    start_min: int,
    end_min: int,
    power_kw: float,
) -> list[float]:
    out = [0.0] * Q15_PER_DAY
    if power_kw <= 0 or end_min <= start_min:
        return out
    for j in range(Q15_PER_DAY):
        slot_start = j * 15
        slot_end = slot_start + 15
        overlap = max(0, min(end_min, slot_end) - max(start_min, slot_start))
        if overlap > 0:
            out[j] = round(power_kw * overlap / 60.0, 6)
    return out


def build_ev_q15(session: dict[str, Any] | None) -> list[float]:
    if not session:
        return [0.0] * Q15_PER_DAY
    total = [0.0] * Q15_PER_DAY
    for key in SLOT_KEYS:
        default = SLOT_DEFAULTS[key]
        slot = session.get(key) or {}
        if not slot.get("enabled"):
            continue
        power = float(slot.get("power_kw") or 0)
        if power <= 0:
            continue
        start = parse_hhmm(str(slot.get("start") or default["start"]))
        end = parse_hhmm(str(slot.get("end") or default["end"]))
        if start is None or end is None:
            continue
        end = slot_end_minutes(start, end)
        if end <= start:
            continue
        slot_q15 = _slot_q15_energy(start, end, power)
        total = [round(a + b, 6) for a, b in zip(total, slot_q15)]
    return total


def q15_to_hourly(q15: list[float]) -> list[float]:
    hourly: list[float] = []
    for h in range(HOURS_PER_DAY):
        chunk = q15[h * 4:(h + 1) * 4]
        hourly.append(round(sum(chunk), 3))
    return hourly


def build_ev_hourly(session: dict[str, Any] | None) -> list[float]:
    return q15_to_hourly(build_ev_q15(session))


def ev_total_kwh(session: dict[str, Any] | None) -> float:
    return round(sum(build_ev_hourly(session)), 2)


def prune_old_sessions(cfg: dict) -> None:
    today = now_warsaw().date()
    cutoff = (today - timedelta(days=HISTORY_RETENTION_DAYS)).strftime("%Y-%m-%d")
    data = _load_store_ready(cfg)
    history: dict[str, Any] = data.get("history") or {}
    kept = {d: s for d, s in history.items() if d >= cutoff}
    if len(kept) != len(history):
        data["history"] = kept
        save_store(data)


def api_payload(cfg: dict) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "max_power_kw": max_power_kw(cfg),
        "defaults": default_session(),
        "today": {
            "date": today_str,
            "session": get_session_for_ui(today_str, cfg),
        },
        "tomorrow": {
            "date": tomorrow_str,
            "session": get_session_for_ui(tomorrow_str, cfg),
        },
    }


def apply_session_update(
    *,
    date_str: str,
    slots: dict[str, dict | None],
    cfg: dict,
    edited_slot: str | None = None,
    edited_side: str | None = None,
) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str not in (today_str, tomorrow_str):
        raise ValueError("EV charging can only be set for today or tomorrow")
    current = get_session_for_ui(date_str, cfg)
    merged = deepcopy(current)
    for key in SLOT_KEYS:
        update = slots.get(key)
        if update is not None:
            merged[key] = {**merged[key], **update}
    if edited_slot in SLOT_KEYS and edited_side in ("start", "end"):
        separate_edited_slot(merged, edited_slot, edited_side)
    return set_session(date_str, merged, cfg)
