"""
RCE (Rynek Cen Energii) electricity price fetcher — PSE public API.

API endpoint: https://api.raporty.pse.pl/api/rce-pln
Resolution:   15-minute intervals
PSE unit:     PLN/MWh netto → converted to PLN/kWh brutto (×1.23 VAT) for billing.
Tomorrow:     PSE publishes the next day around 14:00. Request it from 14:00 and retry each tick until that day is stored.

Returned structure (all PLN/kWh values are brutto):
  {
    "current_price_pln_kwh": 0.384,          # current 15-min slot
    "current_period": "2026-06-18 14:00",
    "today":    [0.384, 0.367, ...],         # 24 hourly averages
    ...
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any

import requests

from .grid_config import VAT_BRUTTO_MULTIPLIER
from .influxdb import now_warsaw
from .sqlite_store import read_meta, write_meta

log = logging.getLogger(__name__)

PSE_API_BASE = "https://api.raporty.pse.pl/api"
QUARTERS_PER_DAY = 96
# PSE publishes next-day RCE around 14:00. A miss retries on the next tick.
TOMORROW_FETCH_HOUR = 14
_STORE_KEY = "rce_quarters"
_quarters: dict[str, list[float]] | None = None
_lock = threading.Lock()


def rce_pln_kwh_brutto_from_mwh(rce_pln_mwh: float) -> float:
    """Convert PSE rce_pln (netto PLN/MWh) to billable PLN/kWh brutto."""
    return round(float(rce_pln_mwh) / 1000.0 * VAT_BRUTTO_MULTIPLIER, 4)


# ---------------------------------------------------------------------------
# Public async interface
# ---------------------------------------------------------------------------

async def get_rce_prices() -> dict[str, Any]:
    """Fetch and return RCE price data (async wrapper — runs sync call in thread)."""
    return await asyncio.to_thread(_get_prices_sync)


def get_rce_prices_sync() -> dict[str, Any]:
    """Synchronous version for use from non-async contexts."""
    return _get_prices_sync()


def invalidate_cache() -> None:
    """Drop the in-memory copy. Stored quarter prices stay for the next read."""
    global _quarters
    with _lock:
        _quarters = None


def _slots_complete(slots: list[float | None] | None) -> bool:
    return (
        isinstance(slots, list)
        and len(slots) >= QUARTERS_PER_DAY
        and all(v is not None for v in slots[:QUARTERS_PER_DAY])
    )


def _load_quarters() -> dict[str, list[float]]:
    """Load stored complete days once per process."""
    global _quarters
    if _quarters is not None:
        return _quarters
    loaded: dict[str, list[float]] = {}
    raw = read_meta(_STORE_KEY)
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            for day, slots in data.items():
                if _slots_complete(slots):
                    loaded[str(day)] = [float(v) for v in slots[:QUARTERS_PER_DAY]]
    _quarters = loaded
    return _quarters


def _save_quarters(stored: dict[str, list[float]]) -> None:
    write_meta(_STORE_KEY, json.dumps(stored, separators=(",", ":")))


def _dates_due(dates: list[str], now: datetime) -> list[str]:
    """Dates not stored yet. Tomorrow waits until TOMORROW_FETCH_HOUR."""
    stored = _load_quarters()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    due: list[str] = []
    for day in dates:
        if day in stored:
            continue
        if day > tomorrow:
            continue
        if day == tomorrow and now.hour < TOMORROW_FETCH_HOUR:
            continue
        if day < today or day == today or day == tomorrow:
            due.append(day)
    return due


def hourly_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    """Hourly RCE (PLN/kWh brutto) from the stored quarter cache."""
    quarters = quarter_rce_for_dates(*dates)
    return {day: hourly_mean_from_quarters(slots) for day, slots in quarters.items()}


def hourly_mean_from_quarters(quarters: list[float | None]) -> list[float | None]:
    """Hourly mean from 96 real 15-min RCE slots (for display only)."""
    out: list[float | None] = []
    for h in range(24):
        chunk = quarters[h * 4:(h + 1) * 4]
        vals = [float(v) for v in chunk if v is not None]
        out.append(round(sum(vals) / len(vals), 4) if vals else None)
    return out


async def get_hourly_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    return await asyncio.to_thread(hourly_rce_for_dates, *dates)


def _parse_quarter_days(raw: list[dict]) -> dict[str, list[float | None]]:
    """Map PSE period-end rows onto clock-hour quarters (q0 = HH:00–HH:15)."""
    buckets: dict[tuple[str, int, int], float] = {}
    for rec in raw:
        dtime_str: str = rec.get("dtime", "")
        rce_mwh: float | None = rec.get("rce_pln")
        if not dtime_str or rce_mwh is None:
            continue
        try:
            dt = datetime.strptime(dtime_str[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        pln_kwh = rce_pln_kwh_brutto_from_mwh(rce_mwh)
        end_q = dt.minute // 15
        if end_q == 0:
            slot_dt = dt - timedelta(hours=1)
            key = (slot_dt.strftime("%Y-%m-%d"), slot_dt.hour, 3)
        else:
            key = (dt.strftime("%Y-%m-%d"), dt.hour, end_q - 1)
        buckets[key] = pln_kwh
    days = sorted({key[0] for key in buckets})
    return {
        day: [
            buckets.get((day, hour, quarter))
            for hour in range(24)
            for quarter in range(4)
        ]
        for day in days
    }


def quarter_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    """96-slot RCE per date. Fetch PSE only for a due day that is not stored yet."""
    unique = sorted({d for d in dates if d})
    if not unique:
        return {}
    with _lock:
        now = now_warsaw()
        stored = _load_quarters()
        due = _dates_due(unique, now)
        if due:
            parsed = _parse_quarter_days(_fetch_rce(due[0], due[-1]))
            changed = False
            for day in due:
                slots = parsed.get(day)
                # A failed or partial response must not replace a stored day.
                if day in stored or not _slots_complete(slots):
                    continue
                stored[day] = [float(v) for v in slots[:QUARTERS_PER_DAY]]
                changed = True
            if changed:
                _save_quarters(stored)
            missing = [day for day in due if day not in stored]
            if missing:
                log.info("RCE still missing %s — keep stored prices", ", ".join(missing))
        return {
            day: list(stored[day]) if day in stored else [None] * QUARTERS_PER_DAY
            for day in unique
        }


async def get_quarter_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    return await asyncio.to_thread(quarter_rce_for_dates, *dates)


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _current_period_end(now: datetime) -> tuple[str, int, int]:
    """Clock-aligned 15-min slot containing *now* (date, hour, quarter 0..3)."""
    return now.strftime("%Y-%m-%d"), now.hour, now.minute // 15


def _refresh_current_price(data: dict[str, Any]) -> None:
    """Recompute live current slot price (safe to call on cached series)."""
    now = now_warsaw()
    date_key, hour, q = _current_period_end(now)
    time_label = f"{hour:02d}:{q * 15:02d}"
    current_price = None
    for slot in data.get("series_15min", []):
        if slot.get("date") == date_key and slot.get("time") == time_label:
            current_price = slot.get("price")
            break
    data["current_price_pln_kwh"] = current_price
    data["current_period"] = now.strftime("%Y-%m-%d %H:%M")


def _attach_g12_hours(data: dict[str, Any]) -> dict[str, Any]:
    """Add hourly G12 zone/buy_price for today and tomorrow from config."""
    from .config import load_config
    from .g12_pricing import g12_hours_for_dates

    dates = data.get("dates") or {}
    today = dates.get("today")
    tomorrow = dates.get("tomorrow")
    if not today:
        now = now_warsaw()
        today = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    wanted = [d for d in (today, tomorrow) if d]
    data["g12_hours"] = g12_hours_for_dates(wanted, load_config()) if wanted else []
    return data


def prepare_rce_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Refresh the live slot price and attach G12 hours for the UI."""
    _refresh_current_price(data)
    return _attach_g12_hours(data)


def _quarter_price(slots: list[float | None], hour: int, quarter: int) -> float | None:
    idx = hour * 4 + quarter
    if idx >= len(slots):
        return None
    raw = slots[idx]
    return None if raw is None else float(raw)


def _series_slots(day_name: str, date_str: str, slots: list[float | None]) -> list[dict]:
    out: list[dict] = []
    for hour in range(24):
        for quarter in range(4):
            price = _quarter_price(slots, hour, quarter)
            out.append({
                "day": day_name,
                "date": date_str,
                "time": f"{hour:02d}:{quarter * 15:02d}",
                "price": round(price, 4) if price is not None else None,
            })
    return out


def _half_hour_slots(day_name: str, date_str: str, slots: list[float | None]) -> list[dict]:
    out: list[dict] = []
    for hour in range(24):
        for half in range(2):
            vals = [
                v for v in (
                    _quarter_price(slots, hour, half * 2),
                    _quarter_price(slots, hour, half * 2 + 1),
                )
                if v is not None
            ]
            out.append({
                "day": day_name,
                "date": date_str,
                "time": f"{hour:02d}:{30 * half:02d}",
                "price": round(sum(vals) / len(vals), 4) if vals else None,
            })
    return out


def _payload_from_quarters(
    now: datetime,
    today_str: str,
    tomorrow_str: str,
    quarters: dict[str, list[float | None]],
) -> dict[str, Any]:
    today_slots = quarters.get(today_str) or [None] * QUARTERS_PER_DAY
    tomorrow_slots = quarters.get(tomorrow_str) or [None] * QUARTERS_PER_DAY
    tomorrow_has_prices = any(v is not None for v in tomorrow_slots)
    _, start_h, start_q = _current_period_end(now)
    series_from_now: list[dict] = []
    for hour in range(start_h, 24):
        q_start = start_q if hour == start_h else 0
        for quarter in range(q_start, 4):
            price = _quarter_price(today_slots, hour, quarter)
            series_from_now.append({
                "day": "today",
                "date": today_str,
                "time": f"{hour:02d}:{quarter * 15:02d}",
                "price": round(price, 4) if price is not None else None,
            })
    if tomorrow_has_prices:
        series_from_now.extend(_series_slots("tomorrow", tomorrow_str, tomorrow_slots))
    return {
        "current_price_pln_kwh": None,
        "current_period": now.strftime("%Y-%m-%d %H:%M"),
        "today": hourly_mean_from_quarters(today_slots),
        "tomorrow": hourly_mean_from_quarters(tomorrow_slots),
        "series_from_now": series_from_now,
        "series_15min": (
            _series_slots("today", today_str, today_slots)
            + _series_slots("tomorrow", tomorrow_str, tomorrow_slots)
        ),
        "series_30min": (
            _half_hour_slots("today", today_str, today_slots)
            + _half_hour_slots("tomorrow", tomorrow_str, tomorrow_slots)
        ),
        "tomorrow_has_prices": tomorrow_has_prices,
        "dates": {"today": today_str, "tomorrow": tomorrow_str},
    }


def _get_prices_sync() -> dict[str, Any]:
    now = now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    quarters = quarter_rce_for_dates(today_str, tomorrow_str)
    return prepare_rce_payload(
        _payload_from_quarters(now, today_str, tomorrow_str, quarters)
    )


def _fetch_rce(date_from: str, date_to: str) -> list[dict]:
    """Call PSE OData API and return raw list of 15-min records.

    NOTE: The requests library percent-encodes `$` in param keys ($filter →
    %24filter), which PSE API rejects.  We build the query string manually
    to keep literal `$` characters.

    Follows PSE ``nextLink`` cursor pagination ($first=500 ≈ 5 days per page).
    """
    qs = (
        f"$select=business_date,dtime,rce_pln"
        f"&$filter=business_date ge '{date_from}' and business_date le '{date_to}'"
        f"&$orderby=dtime asc"
        f"&$first=500"
    )
    url: str | None = f"{PSE_API_BASE}/rce-pln?{qs}"
    out: list[dict] = []
    while url:
        try:
            resp = requests.get(url, timeout=20, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("value", [])
            if batch:
                out.extend(batch)
            url = data.get("nextLink")
        except Exception as exc:
            log.warning("PSE RCE fetch failed: %s", exc)
            break
    return out


# ---------------------------------------------------------------------------
# Helpers used by other modules
# ---------------------------------------------------------------------------

def build_price_hourly_series(rce_data: dict[str, Any], cfg: dict) -> list[float | None]:
    """Build a continuous hourly price series from now until end of tomorrow.

    Returns a list of PLN/kWh prices indexed sequentially starting at the
    current hour.      Returns None for hours whose day is not in the RCE cache yet.
    """
    now = now_warsaw()
    start_hour = now.hour
    today_str = now.strftime("%Y-%m-%d")
    tomorrow = now + timedelta(days=1)

    # Build indexed list: (date_str, hour) → price
    price_map: dict[tuple[str, int], float | None] = {}
    for h, p in enumerate(rce_data.get("today", [])):
        price_map[(today_str, h)] = p
    for h, p in enumerate(rce_data.get("tomorrow", [])):
        price_map[(tomorrow.strftime("%Y-%m-%d"), h)] = p

    series: list[float | None] = []
    for step in range(_simulation_steps(now)):
        dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=step)
        series.append(price_map.get((dt.strftime("%Y-%m-%d"), dt.hour)))
    return series


def _simulation_steps(now: datetime) -> int:
    """Number of hourly steps from current hour to 23:00 tomorrow (inclusive)."""
    end = (now + timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    start = now.replace(minute=0, second=0, microsecond=0)
    return int((end - start).total_seconds() // 3600) + 1

