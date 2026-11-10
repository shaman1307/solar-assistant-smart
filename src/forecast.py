"""
PV generation forecast (Open-Meteo) and Load forecast (weekday cache).

Load/PV profiles for today and tomorrow come from the nightly file cache
(see forecast_cache.py). PV past hours today use Influx actuals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from . import forecast_cache as fc
from . import ev_charging as ev
from . import pv_horizon as pvh
from .influxdb import WARSAW, now_warsaw

log = logging.getLogger(__name__)

CACHE_TTL_S = 3600
_pv_cache: dict[str, Any] = {}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEZONE = "Europe/Warsaw"
OM_TIMEOUT_S = 20
OM_FETCH_RETRIES = 2
OM_RETRY_DELAY_S = 2

_archive_cache: dict[tuple, dict[str, Any]] = {}
ARCHIVE_CACHE_TTL_S = 86400


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_forecast(
    cfg: dict,
    *,
    today_pv_actual: list[float | None] | None = None,
) -> dict[str, Any]:
    """Return hourly PV + load for today and tomorrow (cache + PV actuals)."""
    return await asyncio.to_thread(_assemble_forecast_with_heal, cfg, today_pv_actual)


def _assemble_forecast_with_heal(
    cfg: dict,
    today_pv_actual: list[float | None] | None = None,
) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    cache = fc.load_day_cache()
    day_t = cache.get("days", {}).get(today_str, {})
    pv_total = float((day_t.get("pv") or {}).get("total_base") or 0.0)
    if pv_total <= 0.01:
        log.warning("Today PV cache empty — running intraday OM refresh")
        fc.refresh_intraday_pv(cfg)
    return _assemble_forecast(cfg, today_pv_actual)


async def get_pv_profiles(cfg: dict) -> dict[str, list[float]]:
    """Cached today/tomorrow PV hourly kWh from day cache (effective)."""
    return await asyncio.to_thread(_pv_from_day_cache, cfg)


async def run_nightly_forecast_cache(cfg: dict) -> dict[str, Any]:
    """23:59 job: base Load+PV for D+1/D+2, reset overrides."""
    return await asyncio.to_thread(fc.build_nightly_cache, cfg)


async def run_hourly_pv_refresh(cfg: dict) -> dict[str, Any]:
    """Hourly :00 — refresh Open-Meteo PV for remaining today + tomorrow."""
    return await asyncio.to_thread(fc.refresh_intraday_pv, cfg)


async def apply_overrides_to_cache(cfg: dict) -> dict[str, Any]:
    return await asyncio.to_thread(fc.apply_overrides_to_cache, cfg)


async def recompute_pv_from_cached_radiation(cfg: dict) -> dict[str, Any]:
    """Rebuild today/tomorrow PV from cached radiation (no Open-Meteo request)."""
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    invalidate_cache()
    return await asyncio.to_thread(
        fc.recompute_pv_from_cached_radiation, cfg, [today_str, tomorrow_str],
    )


def _pv_from_day_cache(cfg: dict) -> dict[str, list[float]]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    fc.ensure_cache_days(cfg, [today_str, tomorrow_str])
    return {
        "today": fc.effective_hourly(cfg, today_str, "pv"),
        "tomorrow": fc.effective_hourly(cfg, tomorrow_str, "pv"),
    }


# ---------------------------------------------------------------------------
# PV — Open-Meteo (used for nightly cache build and horizon dates)
# ---------------------------------------------------------------------------

def _pv_cache_key(cfg: dict) -> tuple:
    solar = cfg["solar"]
    blocks = tuple(
        (
            b["power_kwp"], b["tilt"], json.dumps(b.get("horizon_mask") or []),
            b.get("rear_gain"), b.get("rear_az_from"), b.get("rear_az_to"), b.get("rear_alt_max"),
        )
        for b in solar.get("blocks", [])
    )
    return (
        round(cfg["location"]["latitude"], 4),
        round(cfg["location"]["longitude"], 4),
        round(solar["azimuth"], 2),
        round(solar["system_loss_factor"], 4),
        blocks,
    )


def _get_pv_profiles_sync(cfg: dict) -> dict[str, list[float]]:
    """Open-Meteo rolling PV cache for horizon / fallback only."""
    global _pv_cache
    key = _pv_cache_key(cfg)
    now = time.time()
    cached = _pv_cache.get(key)
    if cached and cached.get("ts", 0) > now - CACHE_TTL_S:
        return cached["data"]

    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    today_pv = [0.0] * 24
    tomorrow_pv = [0.0] * 24
    fetch_ok = False

    try:
        batch, om_failed = fetch_pv_for_dates_sync(cfg, [today_str, tomorrow_str])
        if om_failed:
            if cached:
                log.warning("OM fetch failed, serving stale cache")
                return cached["data"]
        else:
            today_pv = list((batch.get(today_str) or {}).get("hourly") or [0.0] * 24)
            tomorrow_pv = list((batch.get(tomorrow_str) or {}).get("hourly") or [0.0] * 24)
            fetch_ok = True
    except Exception as exc:
        log.warning("Open-Meteo PV fetch failed: %s", exc)
        if cached:
            log.warning("OM fetch failed, serving stale cache")
            return cached["data"]

    data = {"today": today_pv, "tomorrow": tomorrow_pv}
    if fetch_ok:
        _pv_cache[key] = {"ts": now, "data": data}
    return data


def get_pv_hourly_for_date_sync(cfg: dict, date_str: str) -> tuple[list[float], str]:
    """Open-Meteo PV hourly kWh for any calendar date."""
    batch, _om_failed = fetch_pv_for_dates_sync(cfg, [date_str])
    prof = batch.get(date_str)
    if prof is not None:
        today = now_warsaw().date()
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        src = "open-meteo-archive" if target < today else "open-meteo-15min"
        return list(prof["hourly"]), src
    return [0.0] * 24, "forecast"


def fetch_pv_hourly_for_dates_sync(cfg: dict, date_strs: list[str]) -> dict[str, list[float]]:
    """Hourly PV kWh per date (derived from q15 when forecast API is used)."""
    batch, _om_failed = fetch_pv_for_dates_sync(cfg, date_strs)
    return {
        d: [round(float(v), 3) for v in list(prof.get("hourly") or [0.0] * 24)[:24]]
        for d, prof in batch.items()
    }


def fetch_pv_for_dates_sync(
    cfg: dict, date_strs: list[str],
) -> tuple[dict[str, dict[str, list[float]]], bool]:
    """Fetch Open-Meteo PV: q15 kWh (forecast) + hourly sums.

    Returns ``(profiles_by_date, om_fetch_failed)``. On 15-min batch failure future
    dates are omitted so callers can keep serving stale file cache.
    """
    if not date_strs:
        return {}, False

    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    today = now_warsaw().date()
    out: dict[str, dict[str, list[float]]] = {}
    om_fetch_failed = False

    past = [d for d in date_strs if datetime.strptime(d, "%Y-%m-%d").date() < today]
    future = [d for d in date_strs if d not in past]

    for date_str in past:
        cache_key = (_pv_cache_key(cfg), date_str)
        now_ts = time.time()
        cached = _archive_cache.get(cache_key)
        if cached and cached.get("ts", 0) > now_ts - ARCHIVE_CACHE_TTL_S:
            hourly = [round(float(v), 3) for v in list(cached["data"])[:24]]
        else:
            try:
                hourly_data = _fetch_irradiance_archive(lat, lon, date_str)
                hourly = _fill_pv_for_date(
                    cfg,
                    hourly_data["time"],
                    hourly_data["direct_radiation"],
                    hourly_data["diffuse_radiation"],
                    date_str,
                )
                hourly = [round(float(v), 3) for v in hourly[:24]]
                _archive_cache[cache_key] = {"ts": now_ts, "data": hourly}
            except Exception as exc:
                log.warning("Open-Meteo archive PV for %s failed: %s", date_str, exc)
                hourly = [0.0] * 24
        q15 = fc.hourly_to_q15_equal(hourly)
        out[date_str] = {"hourly": hourly, "q15": q15}

    if not future:
        return out, False

    offsets = [
        (datetime.strptime(d, "%Y-%m-%d").date() - today).days
        for d in future
    ]
    try:
        forecast_days = max(2, max(o + 1 for o in offsets))
        q15_data = _fetch_irradiance_q15(lat, lon, forecast_days=forecast_days)
        times = q15_data["time"]
        direct = q15_data["direct_radiation"]
        diffuse = q15_data["diffuse_radiation"]
        for date_str in future:
            radiation = _radiation_for_date(times, direct, diffuse, date_str)
            q15 = pv_q15_from_radiation(cfg, radiation, date_str)
            hourly = [round(float(v), 3) for v in fc.q15_to_hourly(q15)[:24]]
            out[date_str] = {"hourly": hourly, "q15": q15, "radiation": radiation}
    except Exception as exc:
        log.warning("Open-Meteo 15-min PV batch fetch failed: %s", exc)
        om_fetch_failed = True

    return out, om_fetch_failed


def _radiation_for_date(
    times: list[str],
    direct: list[float],
    diffuse: list[float],
    date_str: str,
) -> dict[str, list]:
    """Open-Meteo q15 radiation rows whose interval falls on this Warsaw day."""
    next_midnight = (
        datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%dT00:00")
    out: dict[str, list] = {"time": [], "direct": [], "diffuse": []}
    for t, d, df in zip(times, direct, diffuse):
        if t[:10] == date_str or t[:16] == next_midnight:
            out["time"].append(t[:16])
            out["direct"].append(float(d or 0))
            out["diffuse"].append(float(df or 0))
    return out


def pv_q15_from_radiation(
    cfg: dict,
    radiation: dict[str, list],
    date_str: str,
    *,
    apply_horizon_mask: bool = True,
) -> list[float]:
    """PV q15 kWh for a Warsaw day from stored Open-Meteo q15 radiation."""
    q15 = _fill_pv_q15_for_date(
        cfg, radiation["time"], radiation["direct"], radiation["diffuse"], date_str,
        apply_horizon_mask=apply_horizon_mask,
    )
    return [round(float(v), 6) for v in q15[:fc.Q15_PER_DAY]]


def unshaded_pv_q15(
    cfg: dict,
    date_str: str,
    radiation: dict[str, list] | None,
    *,
    shaded_effective: list[float] | None = None,
    shaded_base: list[float] | None = None,
) -> list[float] | None:
    """PV q15 with horizon masks ignored, scaled like the shaded series on the chart."""
    if not radiation or not radiation.get("time"):
        return None
    raw = pv_q15_from_radiation(
        cfg, radiation, date_str, apply_horizon_mask=False,
    )
    if shaded_base is None:
        shaded_base = pv_q15_from_radiation(
            cfg, radiation, date_str, apply_horizon_mask=True,
        )
    base_sum = sum(float(v) for v in shaded_base)
    effective = shaded_effective if shaded_effective is not None else shaded_base
    eff_sum = sum(float(v) for v in effective)
    scale = (eff_sum / base_sum) if base_sum > 1e-9 else 1.0
    return [round(float(v) * scale, 6) for v in raw]


def _http_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET JSON with one retry on transient network/server errors."""
    last_exc: Exception | None = None
    for attempt in range(OM_FETCH_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=OM_TIMEOUT_S)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt + 1 < OM_FETCH_RETRIES:
                time.sleep(OM_RETRY_DELAY_S)
    assert last_exc is not None
    raise last_exc


def _fetch_irradiance_archive(lat: float, lon: float, date_str: str) -> dict[str, list]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": "direct_radiation,diffuse_radiation",
        "timezone": TIMEZONE,
    }
    return _http_get_json(OPEN_METEO_ARCHIVE_URL, params)["hourly"]


def _fetch_irradiance_q15(lat: float, lon: float, *, forecast_days: int = 2) -> dict[str, list]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "minutely_15": "direct_radiation,diffuse_radiation",
        "forecast_days": max(2, min(int(forecast_days), 16)),
        "timezone": TIMEZONE,
    }
    return _http_get_json(OPEN_METEO_URL, params)["minutely_15"]


def _fetch_irradiance(lat: float, lon: float, *, forecast_days: int = 2) -> dict[str, list]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "direct_radiation,diffuse_radiation",
        "forecast_days": max(2, min(int(forecast_days), 16)),
        "timezone": TIMEZONE,
    }
    return _http_get_json(OPEN_METEO_URL, params)["hourly"]


def _fill_pv_q15_for_date(
    cfg: dict,
    times: list[str],
    direct: list[float],
    diffuse: list[float],
    date_str: str,
    *,
    apply_horizon_mask: bool = True,
) -> list[float]:
    return _fill_pv_intervals(
        cfg, times, direct, diffuse, date_str,
        interval_min=15, apply_horizon_mask=apply_horizon_mask,
    )


def _fill_pv_for_date(
    cfg: dict,
    times: list[str],
    direct: list[float],
    diffuse: list[float],
    date_str: str,
) -> list[float]:
    return _fill_pv_intervals(cfg, times, direct, diffuse, date_str, interval_min=60)


def _fill_pv_intervals(
    cfg: dict,
    times: list[str],
    direct: list[float],
    diffuse: list[float],
    date_str: str,
    *,
    interval_min: int,
    apply_horizon_mask: bool = True,
) -> list[float]:
    """PV kWh per Warsaw-day slot from Open-Meteo radiation.

    Open-Meteo labels each radiation value with the end of its averaging
    interval; the value fills the slot that starts ``interval_min`` earlier,
    with the sun taken at the interval midpoint.
    """
    n_slots = 24 * 60 // interval_min
    out = [0.0] * n_slots
    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    solar = cfg["solar"]
    azimuth = solar["azimuth"]
    loss = solar["system_loss_factor"]
    blocks = solar["blocks"]
    interval = timedelta(minutes=interval_min)

    for t, d, df in zip(times, direct, diffuse):
        end_utc = (
            datetime.strptime(t[:16], "%Y-%m-%dT%H:%M")
            .replace(tzinfo=WARSAW)
            .astimezone(timezone.utc)
        )
        start_local = (end_utc - interval).astimezone(WARSAW)
        if start_local.strftime("%Y-%m-%d") != date_str:
            continue
        slot = (start_local.hour * 60 + start_local.minute) // interval_min
        if not (0 <= slot < n_slots):
            continue
        sun_alt, sun_az = _solar_position(lat, lon, end_utc - interval / 2)
        kwh = 0.0
        for block in blocks:
            kwh += _block_interval_kwh(
                d, df,
                block["power_kwp"],
                block["tilt"],
                azimuth,
                sun_alt,
                sun_az,
                loss,
                interval_h=interval_min / 60.0,
                beam_visible=(
                    pvh.beam_visible(block.get("horizon_mask"), sun_alt, sun_az)
                    if apply_horizon_mask else True
                ),
                rear_gain=pvh.rear_gain(block, sun_alt, sun_az),
            )
        out[slot] += kwh
    return out


def _solar_position(lat_deg: float, lon_deg: float, when_utc: datetime) -> tuple[float, float]:
    """Sun altitude and compass azimuth (radians) at an aware UTC instant."""
    day_of_year = when_utc.timetuple().tm_yday
    b = math.radians(360 / 365 * (day_of_year - 81))
    eot_min = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    hour = (
        when_utc.hour + when_utc.minute / 60.0 + when_utc.second / 3600.0
        + lon_deg / 15.0 + eot_min / 60.0
    )
    declination = math.radians(23.45 * math.sin(math.radians(360 / 365 * (day_of_year + 284))))
    hour_angle = math.radians(15 * (hour - 12))
    lat_rad = math.radians(lat_deg)
    altitude = math.asin(
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    denom = math.cos(altitude) * math.cos(lat_rad)
    cos_az = (
        (math.sin(declination) - math.sin(altitude) * math.sin(lat_rad)) / denom
        if abs(denom) > 1e-9
        else 0.0
    )
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.acos(cos_az) if hour_angle < 0 else 2 * math.pi - math.acos(cos_az)
    return altitude, az


def sun_path(cfg: dict, date_str: str, step_min: int = 5) -> list[dict[str, Any]]:
    """Sun above the horizon on a Warsaw day: local time, azimuth and altitude (degrees)."""
    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=WARSAW)
    out: list[dict[str, Any]] = []
    for k in range(24 * 60 // step_min):
        local = start + timedelta(minutes=k * step_min)
        alt, az = _solar_position(lat, lon, local.astimezone(timezone.utc))
        if alt > 0:
            out.append({
                "time": local.strftime("%H:%M"),
                "az": round(math.degrees(az), 2),
                "alt": round(math.degrees(alt), 2),
            })
    return out


def _block_interval_kwh(
    direct: float,
    diffuse: float,
    power_kwp: float,
    tilt_deg: float,
    azimuth_compass: float,
    sun_alt: float,
    sun_az: float,
    loss: float,
    *,
    interval_h: float = 1.0,
    beam_visible: bool = True,
    rear_gain: float = 0.0,
) -> float:
    """Front-plane irradiance plus rear-side gain as a fraction of direct normal."""
    if sun_alt <= 0.05:
        return 0.0
    t_r = math.radians(tilt_deg)
    az_r = math.radians(azimuth_compass)
    cos_i = (
        math.sin(sun_alt) * math.cos(t_r)
        + math.cos(sun_alt) * math.sin(t_r) * math.cos(sun_az - az_r)
    )
    proj = max(0.0, cos_i / math.sin(sun_alt)) if beam_visible else 0.0
    rear = rear_gain / math.sin(sun_alt) if beam_visible else 0.0
    irr_wh_m2 = (
        (direct or 0) * (proj + rear) + (diffuse or 0) * (1 + math.cos(t_r)) / 2
    ) * interval_h
    return irr_wh_m2 / 1000.0 * power_kwp * loss


def _block_hour_kwh(
    direct: float,
    diffuse: float,
    power_kwp: float,
    tilt_deg: float,
    azimuth_compass: float,
    sun_alt: float,
    sun_az: float,
    loss: float,
) -> float:
    return _block_interval_kwh(
        direct, diffuse, power_kwp, tilt_deg, azimuth_compass,
        sun_alt, sun_az, loss, interval_h=1.0,
    )


# ---------------------------------------------------------------------------
# Assembly (today / tomorrow from file cache)
# ---------------------------------------------------------------------------

def _assemble_forecast(
    cfg: dict,
    today_pv_actual_raw: list[float | None] | None = None,
) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    fc.refresh_effective_metrics(cfg, [today_str, tomorrow_str])

    today_pv_cached = fc.effective_hourly(cfg, today_str, "pv")
    tomorrow_pv = fc.effective_hourly(cfg, tomorrow_str, "pv")
    today_load = fc.effective_hourly(cfg, today_str, "load")
    tomorrow_load = fc.effective_hourly(cfg, tomorrow_str, "load")
    today_pv_q15 = fc.effective_q15(cfg, today_str, "pv")
    tomorrow_pv_q15 = fc.effective_q15(cfg, tomorrow_str, "pv")
    today_pv_forecast_q15 = list(today_pv_q15)
    tomorrow_pv_forecast_q15 = list(tomorrow_pv_q15)
    today_load_q15 = fc.effective_q15(cfg, today_str, "load")
    tomorrow_load_q15 = fc.effective_q15(cfg, tomorrow_str, "load")

    today_pv_actual_raw = today_pv_actual_raw or [None] * 24
    today_pv_actual = [
        round(float(v), 3) if v is not None else None
        for v in today_pv_actual_raw[:24]
    ]
    while len(today_pv_actual) < 24:
        today_pv_actual.append(None)

    today_pv = _today_pv_profile(today_pv_actual_raw, today_pv_cached)
    today_pv_q15_merged = _today_pv_q15(today_pv_actual, today_pv_q15)

    cache = fc.load_day_cache()
    day_t = cache.get("days", {}).get(today_str, {})
    day_tm = cache.get("days", {}).get(tomorrow_str, {})
    base_load_today = list((day_t.get("load") or {}).get("base_hourly") or today_load)
    base_load_tomorrow = list((day_tm.get("load") or {}).get("base_hourly") or tomorrow_load)

    base_today = _make_day(today_pv, today_load)
    base_today["pv_q15"] = [round(v, 6) for v in today_pv_q15_merged]
    base_today["load_q15"] = [round(v, 6) for v in today_load_q15]
    base_today["pv_forecast"] = [round(v, 3) for v in today_pv_cached]
    base_today["pv_forecast_q15"] = [round(v, 6) for v in today_pv_forecast_q15]
    today_unshaded = unshaded_pv_q15(
        cfg, today_str, (day_t.get("pv") or {}).get("radiation"),
        shaded_effective=today_pv_forecast_q15,
        shaded_base=list((day_t.get("pv") or {}).get("base_q15") or today_pv_forecast_q15),
    )
    if today_unshaded is not None:
        base_today["pv_unshaded_q15"] = today_unshaded
    base_today["pv_actual"] = today_pv_actual
    base_today["load_forecast"] = [round(v, 3) for v in today_load]

    base_tomorrow = _make_day(tomorrow_pv, tomorrow_load)
    base_tomorrow["pv_q15"] = [round(v, 6) for v in tomorrow_pv_q15]
    base_tomorrow["load_q15"] = [round(v, 6) for v in tomorrow_load_q15]
    base_tomorrow["pv_forecast"] = list(tomorrow_pv)
    base_tomorrow["pv_forecast_q15"] = [round(v, 6) for v in tomorrow_pv_forecast_q15]
    tomorrow_unshaded = unshaded_pv_q15(
        cfg, tomorrow_str, (day_tm.get("pv") or {}).get("radiation"),
        shaded_effective=tomorrow_pv_forecast_q15,
        shaded_base=list((day_tm.get("pv") or {}).get("base_q15") or tomorrow_pv_forecast_q15),
    )
    if tomorrow_unshaded is not None:
        base_tomorrow["pv_unshaded_q15"] = tomorrow_unshaded
    base_tomorrow["pv_actual"] = [None] * 24
    base_tomorrow["load_forecast"] = [round(v, 3) for v in tomorrow_load]

    pv_actual_h = sum(1 for h in range(24) if today_pv_actual[h] is not None)
    baseline = fc.baseline_totals(cfg)
    ev_payload = ev.api_payload(cfg)

    result: dict[str, Any] = {
        "today": dict(base_today),
        "tomorrow": dict(base_tomorrow),
        "meta": {
            "today_pv_actual_hours": pv_actual_h,
            "today_pv_forecast_hours": 24 - pv_actual_h,
            "cache_computed_at": cache.get("computed_at"),
            "last_om_refresh": cache.get("last_om_refresh"),
            "interval_minutes": 15,
            "pv_om_resolution": "15min",
        },
        "baseline": baseline,
        "overrides_saved": {
            k: cfg.get("overrides", {}).get(k)
            for k in (
                "today_pv_kwh", "today_load_kwh",
                "tomorrow_pv_kwh", "tomorrow_load_kwh",
            )
        },
        "ev_charging": ev_payload,
    }
    return result


def _today_pv_q15(
    actual_hourly: list[float | None],
    forecast_q15: list[float],
) -> list[float]:
    out = list(forecast_q15[:fc.Q15_PER_DAY])
    while len(out) < fc.Q15_PER_DAY:
        out.append(0.0)
    for h in range(24):
        v = actual_hourly[h] if h < len(actual_hourly) else None
        if v is None:
            continue
        quarter = float(v) / 4.0
        for q in range(4):
            out[h * 4 + q] = round(quarter, 6)
    return out


def _today_pv_profile(
    actual: list[float | None] | None,
    forecast: list[float],
) -> list[float]:
    actual = actual or [None] * 24
    result: list[float] = []
    for h in range(24):
        v = actual[h] if h < len(actual) else None
        if v is not None:
            result.append(float(v))
        else:
            result.append(float(forecast[h]) if h < len(forecast) else 0.0)
    return result


def _make_day(pv_hourly: list[float], load_hourly: list[float]) -> dict:
    return {
        "pv": [round(v, 3) for v in pv_hourly],
        "load": [round(v, 3) for v in load_hourly],
        "pv_total": round(sum(pv_hourly), 2),
        "load_total": round(sum(load_hourly), 2),
    }


async def get_horizon_day_profile(date_str: str, cfg: dict) -> dict[str, Any]:
    """PV/load for a calendar date (cache for today/tomorrow, else compute)."""
    from . import influxdb as influxdb_mod

    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    offset = (datetime.strptime(date_str, "%Y-%m-%d").date() - now_warsaw().date()).days

    if date_str in (today_str, tomorrow_str):
        fc.ensure_cache_days(cfg, [date_str])
        pv = fc.effective_hourly(cfg, date_str, "pv")
        load = fc.effective_hourly(cfg, date_str, "load")
        source = "day-cache"
    else:
        pv, pv_source = await asyncio.to_thread(get_pv_hourly_for_date_sync, cfg, date_str)
        if offset < 0:
            accruals = await influxdb_mod.get_accruals_for_date(date_str)
            load_raw = accruals.get("hourly", {}).get("load") or []
            if any(v is not None for v in load_raw):
                load = [float(v) if v is not None else 0.0 for v in load_raw[:24]]
                source = f"{pv_source}+influx"
            else:
                _, load = fc.compute_weekday_load_profile(date_str, cfg)
                source = f"{pv_source}+weekday"
        else:
            _, load = await asyncio.to_thread(fc.compute_weekday_load_profile, date_str, cfg)
            source = f"{pv_source}+weekday"

    while len(pv) < 24:
        pv.append(0.0)
    while len(load) < 24:
        load.append(0.0)

    return {
        "date": date_str,
        "pv": [round(v, 3) for v in pv[:24]],
        "load": [round(v, 3) for v in load[:24]],
        "pv_total": round(sum(pv[:24]), 2),
        "load_total": round(sum(load[:24]), 2),
        "source": source,
    }


def invalidate_cache() -> None:
    """Clear Open-Meteo in-memory caches (not the daily file cache)."""
    _pv_cache.clear()
    _archive_cache.clear()
