"""Per-block horizon masks: beam shading by sun azimuth/altitude.

A mask is a list of horizon elevations (degrees) at azimuths 0, 5, ... 355°.
When the sun is below a block's mask, that block loses beam irradiance and
keeps only diffuse. Masks are built per MPPT from string power and copied to
every block on that MPPT.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

MASK_STEP_DEG = 5
MASK_BINS = 360 // MASK_STEP_DEG
MASK_MAX_DEG = 90.0
MASK_VALUE_STEP_DEG = 5
MPPT_FIELD = "inverter_0"
MIN_SAMPLE_ALT_DEG = 4.0
CALIBRATION_ALT_DEG = 8.0
CALIBRATION_PERCENTILE = 0.8
SHADED_BEAM_FRACTION = 0.5
MIN_SHADED_DAYS = 2


def horizon_elevation_deg(mask: list[float], az_deg: float) -> float:
    """Linear interpolation of a mask at a compass azimuth."""
    pos = (az_deg % 360.0) / MASK_STEP_DEG
    i0 = int(pos) % MASK_BINS
    i1 = (i0 + 1) % MASK_BINS
    frac = pos - int(pos)
    return float(mask[i0]) * (1 - frac) + float(mask[i1]) * frac


def beam_visible(mask: list[float] | None, sun_alt: float, sun_az: float) -> bool:
    """True when the sun (radians) is above a block's horizon mask."""
    if not mask or len(mask) != MASK_BINS:
        return True
    return math.degrees(sun_alt) >= horizon_elevation_deg(mask, math.degrees(sun_az))


def rear_gain(block: dict, sun_alt: float, sun_az: float) -> float:
    """Rear-side gain of a block while the sun (radians) is inside its rear window."""
    gain = float(block.get("rear_gain") or 0.0)
    lo, hi = block.get("rear_az_from"), block.get("rear_az_to")
    if gain <= 0 or lo is None or hi is None:
        return 0.0
    alt_max = block.get("rear_alt_max")
    if alt_max is not None and math.degrees(sun_alt) > float(alt_max):
        return 0.0
    return gain if float(lo) <= math.degrees(sun_az) % 360 <= float(hi) else 0.0


def assign_block_masks(cfg: dict, masks: dict[str, list[float]]) -> None:
    """Copy each MPPT mask onto every block wired to that MPPT, rounded to 5°."""
    step = MASK_VALUE_STEP_DEG
    for block in cfg["solar"]["blocks"]:
        mask = masks.get(str(block.get("mppt")))
        if mask:
            block["horizon_mask"] = [
                min(MASK_MAX_DEG, step * round(v / step)) for v in mask
            ]


def clearsky_irradiance(sun_alt: float, day_of_year: int) -> tuple[float, float]:
    """Clear-sky (direct horizontal, diffuse) W/m²: Haurwitz GHI split by Erbs."""
    if sun_alt <= 0:
        return 0.0, 0.0
    cos_z = math.sin(sun_alt)
    ghi = 1098.0 * cos_z * math.exp(-0.057 / cos_z)
    e0 = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    kt = ghi / (1367.0 * e0 * cos_z)
    if kt <= 0.22:
        kd = 1 - 0.09 * kt
    elif kt <= 0.8:
        kd = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    else:
        kd = 0.165
    dhi = ghi * kd
    return ghi - dhi, dhi


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def _mppt_samples(cfg: dict, mppt: int, date_str: str) -> list[dict[str, Any]]:
    """Beam visibility samples of one MPPT for one clear Warsaw day."""
    from .forecast import _block_interval_kwh, _solar_position
    from .influxdb import (
        CHART_SLOT_MIN,
        WARSAW,
        _influx_chart_mean_kw,
        _warsaw_day_bounds_utc,
    )

    solar = cfg["solar"]
    blocks = [b for b in solar["blocks"] if b.get("mppt") == mppt]
    if not blocks:
        return []
    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    start_utc, end_utc = _warsaw_day_bounds_utc(date_str)
    since = f"'{start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    until = f"'{end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    actual = _influx_chart_mean_kw(f"PV power {mppt}", since, until, date_str, field=MPPT_FIELD)
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=WARSAW)

    raw: list[dict[str, Any]] = []
    for slot, kw in enumerate(actual):
        if kw is None:
            continue
        mid_utc = (
            day_start.astimezone(timezone.utc)
            + timedelta(minutes=slot * CHART_SLOT_MIN + CHART_SLOT_MIN / 2)
        )
        alt, az = _solar_position(lat, lon, mid_utc)
        if math.degrees(alt) < MIN_SAMPLE_ALT_DEG:
            continue
        direct_h, diffuse = clearsky_irradiance(alt, mid_utc.timetuple().tm_yday)
        beam = sum(
            _block_interval_kwh(direct_h, 0.0, b["power_kwp"], b["tilt"], solar["azimuth"],
                                alt, az, solar["system_loss_factor"])
            for b in blocks
        )
        diff = sum(
            _block_interval_kwh(0.0, diffuse, b["power_kwp"], b["tilt"], solar["azimuth"],
                                alt, az, solar["system_loss_factor"])
            for b in blocks
        )
        raw.append({"alt": math.degrees(alt), "az": math.degrees(az), "kw": kw,
                    "beam": beam, "diffuse": diff})

    calib = [r["kw"] / (r["beam"] + r["diffuse"]) for r in raw
             if r["alt"] >= CALIBRATION_ALT_DEG and r["beam"] + r["diffuse"] > 0.1]
    if not calib:
        return []
    k = _percentile(calib, CALIBRATION_PERCENTILE)
    out: list[dict[str, Any]] = []
    for r in raw:
        if r["beam"] < 0.05:
            continue
        visible = (r["kw"] / k - r["diffuse"]) / r["beam"]
        out.append({"date": date_str, "alt": r["alt"], "az": r["az"],
                    "shaded": visible < SHADED_BEAM_FRACTION})
    return out


def _bin_threshold(samples: list[dict[str, Any]]) -> float:
    """Horizon elevation that best separates shaded (below) from lit (above)."""
    shaded_days = {s["date"] for s in samples if s["shaded"]}
    if len(shaded_days) < MIN_SHADED_DAYS:
        return 0.0
    candidates = [0.0] + sorted({round(s["alt"] + 0.01, 2) for s in samples if s["shaded"]})
    best_h, best_err = 0.0, None
    for h in candidates:
        below_shaded_days = {s["date"] for s in samples if s["shaded"] and s["alt"] < h}
        if h > 0 and len(below_shaded_days) < MIN_SHADED_DAYS:
            continue
        err = sum(1 for s in samples if s["shaded"] != (s["alt"] < h))
        if best_err is None or err < best_err:
            best_h, best_err = h, err
    return round(best_h, 1)


def build_masks(cfg: dict, dates: list[str]) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Horizon masks per MPPT from Influx string power on clear days."""
    mppts = sorted({b["mppt"] for b in cfg["solar"]["blocks"] if b.get("mppt") is not None})
    masks: dict[str, list[float]] = {}
    report: dict[str, Any] = {}
    for mppt in mppts:
        samples = [s for d in dates for s in _mppt_samples(cfg, mppt, d)]
        mask = []
        for i in range(MASK_BINS):
            center = i * MASK_STEP_DEG
            in_bin = [s for s in samples
                      if abs((s["az"] - center + 180) % 360 - 180) <= MASK_STEP_DEG / 2]
            mask.append(_bin_threshold(in_bin))
        masks[str(mppt)] = mask
        report[str(mppt)] = {
            "samples": len(samples),
            "shaded": sum(1 for s in samples if s["shaded"]),
        }
    return masks, report
