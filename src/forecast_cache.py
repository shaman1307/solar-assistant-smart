"""Daily Load/PV forecast cache (weekday Load + Open-Meteo PV)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import BASE_DIR, load_config, save_config
from .json_store import atomic_json_save, load_json
from . import ev_charging as ev
from .influxdb import get_load_kwh_10min_for_date_sync, now_warsaw

log = logging.getLogger(__name__)

Q15_PER_DAY = 96
HOURS_PER_DAY = 24
WEEKDAY_SAMPLES = 4
TEN_MIN_PER_DAY = 144

_CACHE_PATH = BASE_DIR / "data" / "forecast_day_cache.json"


def cache_path() -> Path:
    return _CACHE_PATH


def _empty_day_metric() -> dict[str, Any]:
    return {
        "base_q15": [0.0] * Q15_PER_DAY,
        "base_hourly": [0.0] * HOURS_PER_DAY,
        "effective_q15": [0.0] * Q15_PER_DAY,
        "effective_hourly": [0.0] * HOURS_PER_DAY,
        "total_base": 0.0,
        "total_effective": 0.0,
    }


def _empty_day() -> dict[str, Any]:
    return {"load": _empty_day_metric(), "pv": _empty_day_metric()}


def load_day_cache() -> dict[str, Any]:
    data = load_json(_CACHE_PATH, default={"computed_at": None, "days": {}})
    data.setdefault("computed_at", None)
    data.setdefault("days", {})
    return data


def save_day_cache(data: dict[str, Any]) -> None:
    try:
        atomic_json_save(_CACHE_PATH, data)
    except OSError as exc:
        log.warning("Forecast day cache save failed: %s", exc)


def invalidate_day_cache_file() -> None:
    if _CACHE_PATH.is_file():
        _CACHE_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 10-min → 15-min overlap redistribution (energy-preserving)
# ---------------------------------------------------------------------------

def aggregate_10min_to_q15(energy_10min: list[float]) -> list[float]:
    """Redistribute 10-min kWh buckets into 15-min windows by overlap weight."""
    out: list[float] = []
    for j in range(Q15_PER_DAY):
        j_start = j * 15
        j_end = j_start + 15
        e_j = 0.0
        for i in range(TEN_MIN_PER_DAY):
            if i >= len(energy_10min):
                break
            i_start = i * 10
            i_end = i_start + 10
            overlap = max(0, min(i_end, j_end) - max(i_start, j_start))
            if overlap > 0:
                e_j += float(energy_10min[i]) * overlap / 10.0
        out.append(round(e_j, 6))
    return out


def q15_to_hourly(q15: list[float]) -> list[float]:
    hourly: list[float] = []
    for h in range(HOURS_PER_DAY):
        chunk = q15[h * 4:(h + 1) * 4]
        hourly.append(round(sum(chunk), 6))
    return hourly


def hourly_to_q15_equal(hourly: list[float]) -> list[float]:
    q15: list[float] = []
    for h in range(HOURS_PER_DAY):
        v = float(hourly[h] if h < len(hourly) else 0.0) / 4.0
        q15.extend([round(v, 6)] * 4)
    return q15


def cfg_load_hourly(cfg: dict) -> list[float]:
    fallback = cfg.get("load", {}).get("hourly_profile_kwh") or []
    out = [float(v or 0.0) for v in fallback[:HOURS_PER_DAY]]
    while len(out) < HOURS_PER_DAY:
        out.append(out[-1] if out else 0.0)
    return out


def cfg_hourly_to_10min(hourly: list[float]) -> list[float]:
    ten: list[float] = []
    for h in range(HOURS_PER_DAY):
        per = float(hourly[h] if h < len(hourly) else 0.0) / 6.0
        ten.extend([per] * 6)
    return ten


def _weekday_sample_dates(target: datetime.date, count: int = WEEKDAY_SAMPLES) -> list[str]:
    return [
        (target - timedelta(days=7 * k)).strftime("%Y-%m-%d")
        for k in range(1, count + 1)
    ]


def _day_has_load_data(ten_min: list[float] | None) -> bool:
    if not ten_min:
        return False
    return sum(float(v or 0.0) for v in ten_min) > 0.01


def compute_weekday_load_profile(target_date_str: str, cfg: dict) -> tuple[list[float], list[float]]:
    """Average q15/hourly load from four prior same weekdays (10-min Influx → q15)."""
    target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    today_str = now_warsaw().strftime("%Y-%m-%d")
    cfg_hourly = cfg_load_hourly(cfg)
    cfg_ten = cfg_hourly_to_10min(cfg_hourly)

    q15_samples: list[list[float]] = []
    for date_str in _weekday_sample_dates(target):
        ten_min = get_load_kwh_10min_for_date_sync(date_str)
        if not _day_has_load_data(ten_min):
            ten_min = list(cfg_ten)
        else:
            ten_min = [float(v or 0.0) for v in ten_min]
        q15 = aggregate_10min_to_q15(ten_min)
        hist = ev.session_for_history_subtract(date_str, today_str, cfg)
        if hist:
            ev_q15 = ev.build_ev_q15(hist)
            q15 = [max(0.0, round(q15[i] - ev_q15[i], 6)) for i in range(Q15_PER_DAY)]
        q15_samples.append(q15)

    def _robust_slot_avg(values: list[float]) -> float:
        """Average with outlier resistance (trim extremes when possible)."""
        if not values:
            return 0.0
        if len(values) >= 4:
            s = sorted(float(v) for v in values)
            mid = s[1:-1]  # trimmed mean: drop min/max
            return sum(mid) / len(mid)
        return sum(float(v) for v in values) / len(values)

    avg_q15: list[float] = []
    for slot in range(Q15_PER_DAY):
        vals = [s[slot] for s in q15_samples]
        avg_q15.append(round(_robust_slot_avg(vals), 6))
    return avg_q15, q15_to_hourly(avg_q15)


def _set_metric_effective(metric: dict[str, Any], hourly: list[float], q15: list[float]) -> None:
    metric["base_q15"] = [round(v, 6) for v in q15]
    metric["base_hourly"] = [round(v, 3) for v in hourly]
    metric["effective_q15"] = list(metric["base_q15"])
    metric["effective_hourly"] = list(metric["base_hourly"])
    total = round(sum(hourly), 2)
    metric["total_base"] = total
    metric["total_effective"] = total


def _scale_metric(metric: dict[str, Any], target_total: float | None) -> None:
    base_total = float(metric.get("total_base") or 0.0)
    if target_total is None or base_total <= 0.0:
        metric["effective_q15"] = list(metric.get("base_q15") or [])
        metric["effective_hourly"] = list(metric.get("base_hourly") or [])
        metric["total_effective"] = base_total
        return
    scale = float(target_total) / base_total
    metric["effective_q15"] = [round(v * scale, 6) for v in metric["base_q15"]]
    metric["effective_hourly"] = [round(v * scale, 3) for v in metric["base_hourly"]]
    metric["total_effective"] = round(target_total, 2)


def _apply_load_effective(
    metric: dict[str, Any],
    override_base_total: float | None,
    ev_q15: list[float],
) -> None:
    """Scale base load only (override slider); add EV energy on top."""
    base_q15 = list(metric.get("base_q15") or [0.0] * Q15_PER_DAY)
    base_total = float(metric.get("total_base") or 0.0)
    if override_base_total is None or base_total <= 0.0:
        scaled_q15 = list(base_q15)
    else:
        scale = float(override_base_total) / base_total
        scaled_q15 = [round(v * scale, 6) for v in base_q15]
    effective_q15 = [round(a + b, 6) for a, b in zip(scaled_q15, ev_q15)]
    metric["effective_q15"] = effective_q15
    metric["effective_hourly"] = q15_to_hourly(effective_q15)
    metric["total_effective"] = round(sum(metric["effective_hourly"]), 2)


def build_day_forecast(
    date_str: str,
    cfg: dict,
    pv_hourly: list[float],
    pv_q15: list[float] | None = None,
) -> dict[str, Any]:
    load_q15, load_hourly = compute_weekday_load_profile(date_str, cfg)
    day = _empty_day()
    _set_metric_effective(day["load"], load_hourly, load_q15)
    day["load"]["base_source"] = "weekday_samples_trimmed_v1"
    q15 = list(pv_q15) if pv_q15 is not None else hourly_to_q15_equal(pv_hourly)
    while len(q15) < Q15_PER_DAY:
        q15.append(0.0)
    _set_metric_effective(day["pv"], pv_hourly, q15[:Q15_PER_DAY])
    return day


def compute_pv_profiles_for_date(date_str: str, cfg: dict) -> tuple[list[float], list[float]]:
    from .forecast import fetch_pv_for_dates_sync

    batch, _om_failed = fetch_pv_for_dates_sync(cfg, [date_str])
    prof = batch.get(date_str) or {}
    hourly = [round(float(v), 3) for v in list(prof.get("hourly") or [0.0] * HOURS_PER_DAY)[:HOURS_PER_DAY]]
    q15_raw = list(prof.get("q15") or hourly_to_q15_equal(hourly))
    while len(hourly) < HOURS_PER_DAY:
        hourly.append(0.0)
    q15 = [round(float(v), 6) for v in q15_raw[:Q15_PER_DAY]]
    while len(q15) < Q15_PER_DAY:
        q15.append(0.0)
    return hourly, q15


def compute_pv_hourly_for_date(date_str: str, cfg: dict) -> list[float]:
    hourly, _q15 = compute_pv_profiles_for_date(date_str, cfg)
    return hourly


def refresh_intraday_pv(cfg: dict, *, now: datetime | None = None) -> dict[str, Any]:
    """Re-fetch Open-Meteo PV for today (full profile) and tomorrow."""
    from .forecast import fetch_pv_for_dates_sync, invalidate_cache as invalidate_om_mem

    now = now or now_warsaw()
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    cache = ensure_cache_days(cfg, [today_str, tomorrow_str])
    days = cache["days"]

    invalidate_om_mem()
    fresh_by_date, om_failed = fetch_pv_for_dates_sync(cfg, [today_str, tomorrow_str])

    if om_failed:
        log.warning("OM fetch failed, serving stale cache")
        return refresh_effective_metrics(cfg, [today_str, tomorrow_str])

    for date_str in (today_str, tomorrow_str):
        prof = fresh_by_date.get(date_str)
        if not prof:
            log.warning("Intraday PV refresh — no data for %s", date_str)
            continue
        merged = [round(float(v), 3) for v in list(prof.get("hourly") or [])[:HOURS_PER_DAY]]
        merged_q15 = [round(float(v), 6) for v in list(prof.get("q15") or [])[:Q15_PER_DAY]]
        while len(merged) < HOURS_PER_DAY:
            merged.append(0.0)
        while len(merged_q15) < Q15_PER_DAY:
            merged_q15.append(0.0)
        pv_metric = days[date_str]["pv"]
        _set_metric_effective(pv_metric, merged, merged_q15)

    cache["last_om_refresh"] = now.strftime("%Y-%m-%d %H:%M:%S")
    log.info(
        "Intraday PV refresh — today + tomorrow full (%s)",
        cache["last_om_refresh"],
    )
    save_day_cache(cache)
    return refresh_effective_metrics(cfg, [today_str, tomorrow_str])


def build_nightly_cache(cfg: dict, *, now: datetime | None = None) -> dict[str, Any]:
    """Compute base Load+PV for tomorrow and day-after; reset overrides in config."""
    now = now or now_warsaw()
    d1 = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    today_str = now.strftime("%Y-%m-%d")
    cache: dict[str, Any] = {
        "computed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "anchor_date": today_str,
        "days": {},
    }
    for date_str in (today_str, d1, d2):
        pv_h, pv_q = compute_pv_profiles_for_date(date_str, cfg)
        cache["days"][date_str] = build_day_forecast(date_str, cfg, pv_h, pv_q)

    save_day_cache(cache)

    overrides = cfg.setdefault("overrides", {})
    for key in ("today_pv_kwh", "today_load_kwh", "tomorrow_pv_kwh", "tomorrow_load_kwh"):
        overrides[key] = None
    save_config(cfg)
    ev.nightly_rollover(cfg, now=now)
    log.info("Nightly forecast cache saved for %s, %s, %s", today_str, d1, d2)
    return cache


def ensure_cache_days(cfg: dict, dates: list[str]) -> dict[str, Any]:
    """Return cache, building any missing dates on demand (dev / first boot)."""
    cache = load_day_cache()
    days: dict[str, Any] = cache.setdefault("days", {})
    changed = False
    for date_str in dates:
        existing = days.get(date_str)
        if existing:
            load_ok = bool(existing.get("load", {}).get("base_hourly"))
            pv_total = float((existing.get("pv") or {}).get("total_base") or 0.0)
            if load_ok and pv_total > 0.01:
                continue
        pv_h, pv_q = compute_pv_profiles_for_date(date_str, cfg)
        days[date_str] = build_day_forecast(date_str, cfg, pv_h, pv_q)
        changed = True
    if changed:
        cache["computed_at"] = now_warsaw().strftime("%Y-%m-%d %H:%M:%S")
        save_day_cache(cache)
    return cache


def apply_overrides_to_cache(cfg: dict) -> dict[str, Any]:
    """Scale effective Load/PV in file cache from sa-config overrides + EV plans."""
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    return refresh_effective_metrics(cfg, [today_str, tomorrow_str])


def refresh_effective_metrics(cfg: dict, dates: list[str]) -> dict[str, Any]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    cache = ensure_cache_days(cfg, dates)
    days = cache["days"]
    overrides: dict = cfg.get("overrides", {})

    load_keys = {
        today_str: "today_load_kwh",
        tomorrow_str: "tomorrow_load_kwh",
    }
    pv_keys = {
        today_str: "today_pv_kwh",
        tomorrow_str: "tomorrow_pv_kwh",
    }

    for date_str in dates:
        if date_str not in days:
            continue
        load_metric = days[date_str]["load"]
        # Rebuild today's base load profile when base_source is not weekday_samples_trimmed_v1.
        if date_str == today_str and load_metric.get("base_source") != "weekday_samples_trimmed_v1":
            load_q15, load_hourly = compute_weekday_load_profile(date_str, cfg)
            _set_metric_effective(load_metric, load_hourly, load_q15)
            load_metric["base_source"] = "weekday_samples_trimmed_v1"
        val = overrides.get(load_keys.get(date_str, ""))
        target = float(val) if val is not None else None
        session = ev.session_for_load_add(date_str, today_str, tomorrow_str, cfg)
        ev_q15 = ev.build_ev_q15(session)
        _apply_load_effective(load_metric, target, ev_q15)

        pv_metric = days[date_str]["pv"]
        pval = overrides.get(pv_keys.get(date_str, ""))
        ptarget = float(pval) if pval is not None else None
        _scale_metric(pv_metric, ptarget)

    cache["computed_at"] = now_warsaw().strftime("%Y-%m-%d %H:%M:%S")
    save_day_cache(cache)
    return cache


def effective_hourly(cfg: dict, date_str: str, metric: str) -> list[float]:
    cache = ensure_cache_days(cfg, [date_str])
    day = cache["days"].get(date_str) or _empty_day()
    m = day.get(metric) or _empty_day_metric()
    hourly = list(m.get("effective_hourly") or [0.0] * HOURS_PER_DAY)
    while len(hourly) < HOURS_PER_DAY:
        hourly.append(0.0)
    return hourly[:HOURS_PER_DAY]


def effective_q15(cfg: dict, date_str: str, metric: str) -> list[float]:
    cache = ensure_cache_days(cfg, [date_str])
    day = cache["days"].get(date_str) or _empty_day()
    m = day.get(metric) or _empty_day_metric()
    q15 = list(m.get("effective_q15") or [0.0] * Q15_PER_DAY)
    while len(q15) < Q15_PER_DAY:
        q15.append(0.0)
    return q15[:Q15_PER_DAY]


def hourly_actual_to_q15(hourly: list[float | None]) -> list[float | None]:
    """Completed-hour actuals → 96 q15 slots (equal split within each hour)."""
    out: list[float | None] = [None] * Q15_PER_DAY
    for h in range(HOURS_PER_DAY):
        v = hourly[h] if h < len(hourly) else None
        if v is None:
            continue
        quarter = float(v) / 4.0
        for q in range(4):
            out[h * 4 + q] = round(quarter, 6)
    return out


def ten_min_to_q15(ten_min: list[float]) -> list[float]:
    """Redistribute 10-min kWh buckets into 15-min windows (energy-preserving)."""
    padded = [float(v or 0.0) for v in ten_min[:TEN_MIN_PER_DAY]]
    while len(padded) < TEN_MIN_PER_DAY:
        padded.append(0.0)
    return aggregate_10min_to_q15(padded)


def ten_min_kw_to_q15_kw(ten_min_kw: list[float | None]) -> list[float | None]:
    """Resample 10-min mean power (kW) to 15-min buckets (overlap-weighted average)."""
    out: list[float | None] = [None] * Q15_PER_DAY
    for j in range(Q15_PER_DAY):
        j_start = j * 15
        j_end = j_start + 15
        weighted_sum = 0.0
        weight = 0.0
        for i in range(TEN_MIN_PER_DAY):
            if i >= len(ten_min_kw):
                break
            v = ten_min_kw[i]
            if v is None:
                continue
            i_start = i * 10
            i_end = i_start + 10
            overlap = max(0, min(i_end, j_end) - max(i_start, j_start))
            if overlap > 0:
                weighted_sum += float(v) * overlap
                weight += overlap
        if weight > 0:
            out[j] = round(weighted_sum / weight, 3)
    return out


def baseline_totals(cfg: dict) -> dict[str, dict[str, float]]:
    today_str = now_warsaw().strftime("%Y-%m-%d")
    tomorrow_str = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    cache = ensure_cache_days(cfg, [today_str, tomorrow_str])
    out: dict[str, dict[str, float]] = {}
    for label, date_str in (("today", today_str), ("tomorrow", tomorrow_str)):
        day = cache["days"].get(date_str) or _empty_day()
        out[label] = {
            "pv_kwh": float(day["pv"].get("total_base") or 0.0),
            "load_kwh": float(day["load"].get("total_base") or 0.0),
        }
    return out
