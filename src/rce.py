"""
RCE (Rynek Cen Energii) electricity price fetcher — PSE public API.

API endpoint: https://api.raporty.pse.pl/api/rce-pln
Resolution:   15-minute intervals
PSE unit:     PLN/MWh netto → converted to PLN/kWh brutto (×1.23 VAT) for billing.
Tomorrow:     published by PSE usually after 14:00.

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
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import requests

from .grid_config import VAT_BRUTTO_MULTIPLIER
from .influxdb import now_warsaw

log = logging.getLogger(__name__)

PSE_API_BASE = "https://api.raporty.pse.pl/api"
CACHE_TTL_S = 1800  # 30 minutes — matches PSE publish cadence
_cache: dict[str, Any] = {}


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
    _cache.clear()


def hourly_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    """Hourly RCE (PLN/kWh brutto) for each YYYY-MM-DD date via PSE API."""
    unique = sorted({d for d in dates if d})
    if not unique:
        return {}
    raw = _fetch_rce(unique[0], unique[-1])
    buckets_60: dict[tuple[str, int], list[float]] = defaultdict(list)
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
            buckets_60[(slot_dt.strftime("%Y-%m-%d"), slot_dt.hour)].append(pln_kwh)
        else:
            buckets_60[(dt.strftime("%Y-%m-%d"), dt.hour)].append(pln_kwh)

    def _avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        d: [_avg(buckets_60.get((d, h), [])) for h in range(24)]
        for d in unique
    }


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


def quarter_rce_for_dates(*dates: str) -> dict[str, list[float | None]]:
    """96-slot RCE (PLN/kWh brutto) per date — index = hour*4 + quarter (q0 = HH:00–HH:15)."""
    unique = sorted({d for d in dates if d})
    if not unique:
        return {}
    raw = _fetch_rce(unique[0], unique[-1])
    buckets_15: dict[tuple[str, int, int], float] = {}
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
        # PSE dtime is period end; map to clock-hour quarter (q0 = :00–:15).
        end_q = dt.minute // 15
        if end_q == 0:
            slot_dt = dt - timedelta(hours=1)
            buckets_15[(slot_dt.strftime("%Y-%m-%d"), slot_dt.hour, 3)] = pln_kwh
        else:
            buckets_15[(dt.strftime("%Y-%m-%d"), dt.hour, end_q - 1)] = pln_kwh

    return {
        d: [
            buckets_15.get((d, h, q))
            for h in range(24)
            for q in range(4)
        ]
        for d in unique
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


def _get_prices_sync() -> dict[str, Any]:
    global _cache
    now_ts = time.time()
    if _cache.get("ts", 0) > now_ts - CACHE_TTL_S:
        data = dict(_cache["data"])
        return prepare_rce_payload(data)

    result = _fetch_and_build()
    if any(p is not None for p in result.get("today", [])):
        _cache = {"ts": now_ts, "data": result}
    return prepare_rce_payload(result)


def _fetch_and_build() -> dict[str, Any]:
    now = now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    raw = _fetch_rce(today_str, tomorrow_str)

    # Parse 15-min records into clock-hour quarters (q0 = HH:00–HH:15).
    buckets_15: dict[tuple[str, int, int], float] = {}
    buckets_30: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    buckets_60: dict[tuple[str, int], list[float]] = defaultdict(list)

    for rec in raw:
        dtime_str: str = rec.get("dtime", "")
        rce_mwh: float | None = rec.get("rce_pln")
        if not dtime_str or rce_mwh is None:
            continue
        try:
            dt = datetime.strptime(dtime_str[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        date_key = dt.strftime("%Y-%m-%d")
        pln_kwh = rce_pln_kwh_brutto_from_mwh(rce_mwh)
        end_q = dt.minute // 15
        if end_q == 0:
            slot_dt = dt - timedelta(hours=1)
            clock_date = slot_dt.strftime("%Y-%m-%d")
            clock_h = slot_dt.hour
            clock_q = 3
        else:
            clock_date = date_key
            clock_h = dt.hour
            clock_q = end_q - 1
        buckets_15[(clock_date, clock_h, clock_q)] = pln_kwh
        buckets_30[(clock_date, clock_h, clock_q // 2)].append(pln_kwh)
        buckets_60[(clock_date, clock_h)].append(pln_kwh)

    def _avg(vals: list) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    def hourly(date_str: str) -> list[float | None]:
        return [_avg(buckets_60.get((date_str, h), [])) for h in range(24)]

    def quarter_hourly(date_str: str) -> list[dict]:
        """Return 96 slots per day (15-min resolution)."""
        result = []
        for h in range(24):
            for q in range(4):
                label = f"{h:02d}:{q * 15:02d}"
                price = buckets_15.get((date_str, h, q))
                result.append({"time": label, "price": round(price, 4) if price is not None else None})
        return result

    def half_hourly(date_str: str) -> list[dict]:
        """Return 48 slots per day, each with time label and price."""
        result = []
        for h in range(24):
            for half in range(2):
                label = f"{h:02d}:{30*half:02d}"
                vals = buckets_30.get((date_str, h, half), [])
                result.append({"time": label, "price": _avg(vals)})
        return result

    today_prices = hourly(today_str)
    tomorrow_prices = hourly(tomorrow_str)

    tomorrow_has_prices = any(
        buckets_15.get((tomorrow_str, h, q)) is not None
        for h in range(24) for q in range(4)
    )

    # Series from current 15-min slot until end of today (+ tomorrow when published).
    series_from_now: list[dict] = []
    _, start_h, start_q = _current_period_end(now)

    for h in range(start_h, 24):
        q_start = start_q if h == start_h else 0
        for q in range(q_start, 4):
            price = buckets_15.get((today_str, h, q))
            series_from_now.append({
                "day": "today", "date": today_str,
                "time": f"{h:02d}:{q * 15:02d}",
                "price": round(price, 4) if price is not None else None,
            })

    if tomorrow_has_prices:
        for h in range(24):
            for q in range(4):
                price = buckets_15.get((tomorrow_str, h, q))
                series_from_now.append({
                    "day": "tomorrow", "date": tomorrow_str,
                    "time": f"{h:02d}:{q * 15:02d}",
                    "price": round(price, 4) if price is not None else None,
                })

    series_15min: list[dict] = []
    for slot in quarter_hourly(today_str):
        series_15min.append({"day": "today", "date": today_str, **slot})
    for slot in quarter_hourly(tomorrow_str):
        series_15min.append({"day": "tomorrow", "date": tomorrow_str, **slot})

    series_30min = []
    for slot in half_hourly(today_str):
        series_30min.append({"day": "today", "date": today_str, **slot})
    for slot in half_hourly(tomorrow_str):
        series_30min.append({"day": "tomorrow", "date": tomorrow_str, **slot})

    result = {
        "current_price_pln_kwh": None,
        "current_period": now.strftime("%Y-%m-%d %H:%M"),
        "today": today_prices,
        "tomorrow": tomorrow_prices,
        "series_from_now": series_from_now,
        "series_15min": series_15min,
        "series_30min": series_30min,
        "tomorrow_has_prices": tomorrow_has_prices,
        "dates": {"today": today_str, "tomorrow": tomorrow_str},
    }
    _refresh_current_price(result)
    return result


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
    current hour.  Returns None for hours where RCE is not yet published
    (tomorrow before 14:00 publication).
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

