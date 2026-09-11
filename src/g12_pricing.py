"""G12 Energa buy-zone pricing (peak / offpeak) from hardcoded presets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .grid_config import merge_grid_defaults

# Sole G12 zone table (start inclusive, end exclusive). UI injects this as TARIFF_PRESETS.
TARIFF_PRESETS: dict[str, dict[str, Any]] = {
    "G12": {
        "name": "Energa G12",
        "peak": 1.2444,
        "offpeak": 0.6229,
        "peakHours": [[6, 13], [15, 22]],
        "weekendOffpeak": False,
        "peakHoursLabel": "every day 06-13, 15-22",
    },
    "G12w": {
        "name": "Energa G12w",
        "peak": 1.3100,
        "offpeak": 0.6109,
        "peakHours": [[6, 13], [15, 22]],
        "weekendOffpeak": True,
        "peakHoursLabel": "weekdays 06-13, 15-22; Saturday and Sunday offpeak",
    },
    "G11": {
        "name": "Energa G11",
        "peak": 1.1006,
        "offpeak": 1.1006,
        "peakHours": [],
        "weekendOffpeak": False,
        "peakHoursLabel": "no zones (flat rate)",
    },
}


def g12_tariff_preset(cfg: dict) -> str:
    """Short tariff code: G12, G12w, or G11."""
    merge_grid_defaults(cfg)
    g12 = cfg["grid"]["g12"]
    preset = str(g12.get("tariff_preset") or "").strip()
    if preset:
        return preset
    name = str(g12.get("tariff_name") or "")
    if "G12w" in name or "G12W" in name:
        return "G12w"
    if "G11" in name:
        return "G11"
    return "G12"


def g12_peak_windows(cfg: dict) -> list[tuple[int, int]]:
    """Peak clock windows for the selected tariff preset."""
    merge_grid_defaults(cfg)
    preset = g12_tariff_preset(cfg)
    info = TARIFF_PRESETS.get(preset, TARIFF_PRESETS["G12"])
    return [(int(a), int(b)) for a, b in info.get("peakHours") or []]


def _hour_in_peak_windows(hour: int, windows: list[tuple[int, int]]) -> bool:
    return any(start <= hour < end for start, end in windows)


def get_g12_zone(dt: datetime, cfg: dict) -> str:
    """Peak vs offpeak for the Warsaw clock hour that contains dt.

    Zone windows come from TARIFF_PRESETS for tariff_preset.
    G12: peak windows every calendar day.
    G12w: same windows Monday–Friday; Saturday and Sunday offpeak.
    G11: no peak windows (flat rate).
    """
    merge_grid_defaults(cfg)
    preset = g12_tariff_preset(cfg)
    info = TARIFF_PRESETS.get(preset, TARIFF_PRESETS["G12"])
    windows = g12_peak_windows(cfg)
    if not _hour_in_peak_windows(dt.hour, windows):
        return "offpeak"
    if info.get("weekendOffpeak") and dt.weekday() >= 5:
        return "offpeak"
    return "peak"


def get_buy_price(dt: datetime, cfg: dict) -> tuple[float, str]:
    """Buy PLN/kWh brutto and zone from config G12 prices."""
    merge_grid_defaults(cfg)
    g12 = cfg["grid"]["g12"]
    zone = get_g12_zone(dt, cfg)
    price = g12["peak_price_pln_kwh"] if zone == "peak" else g12["offpeak_price_pln_kwh"]
    return float(price), zone


def g12_hours_for_dates(dates: list[str], cfg: dict) -> list[dict[str, Any]]:
    """Hourly G12 zone and buy price for each YYYY-MM-DD date."""
    merge_grid_defaults(cfg)
    out: list[dict[str, Any]] = []
    for date_str in dates:
        if not date_str:
            continue
        base = datetime.strptime(date_str, "%Y-%m-%d")
        for h in range(24):
            dt = base.replace(hour=h)
            price, zone = get_buy_price(dt, cfg)
            out.append({
                "date": date_str,
                "hour": h,
                "zone": zone,
                "buy_price": round(price, 4),
            })
    return out


def g12_buy_energy_price_pln_kwh(zone: str, cfg: dict) -> float:
    """Energy (obrót) component of G12 buy price, PLN/kWh brutto."""
    merge_grid_defaults(cfg)
    g12 = cfg["grid"]["g12"]
    key = "peak_energy_only_pln_kwh" if zone == "peak" else "offpeak_energy_only_pln_kwh"
    return float(g12[key])


def g12_buy_service_price_pln_kwh(zone: str, cfg: dict) -> float:
    """Official G12 variable network rate (brutto): opłata sieciowa zmienna dzienna/nocna."""
    merge_grid_defaults(cfg)
    dist = cfg["grid"]["distribution"]
    key = (
        "peak_variable_network_pln_kwh"
        if zone == "peak"
        else "offpeak_variable_network_pln_kwh"
    )
    return float(dist[key])


def g12_import_cost_split(
    grid_import: float,
    zone: str,
    cfg: dict,
) -> tuple[float, float]:
    """Split grid import kWh cost into energy vs service (PLN)."""
    imp = max(0.0, float(grid_import))
    energy = imp * g12_buy_energy_price_pln_kwh(zone, cfg)
    service = imp * g12_buy_service_price_pln_kwh(zone, cfg)
    return energy, service
