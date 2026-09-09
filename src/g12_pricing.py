"""G12 Energa buy-zone pricing (peak / offpeak) from config."""

from __future__ import annotations

from datetime import datetime

from .grid_config import merge_grid_defaults


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
    """Peak clock windows from config (start inclusive, end exclusive)."""
    merge_grid_defaults(cfg)
    raw = cfg["grid"]["g12"].get("peak_hours_weekday") or []
    out: list[tuple[int, int]] = []
    for pair in raw:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            out.append((int(pair[0]), int(pair[1])))
    return out


def _hour_in_peak_windows(hour: int, windows: list[tuple[int, int]]) -> bool:
    return any(start <= hour < end for start, end in windows)


def get_g12_zone(dt: datetime, cfg: dict) -> str:
    """Peak vs offpeak for the Warsaw clock hour that contains dt.

    Read tariff_preset and peak_hours_weekday from config.
    G12 / G11: peak windows every calendar day.
    G12w: same windows Monday–Friday; Saturday and Sunday offpeak.
    """
    merge_grid_defaults(cfg)
    windows = g12_peak_windows(cfg)
    if not _hour_in_peak_windows(dt.hour, windows):
        return "offpeak"
    if g12_tariff_preset(cfg) == "G12w" and dt.weekday() >= 5:
        return "offpeak"
    return "peak"


def get_buy_price(dt: datetime, cfg: dict) -> tuple[float, str]:
    """Buy PLN/kWh brutto and zone from config G12 prices."""
    merge_grid_defaults(cfg)
    g12 = cfg["grid"]["g12"]
    zone = get_g12_zone(dt, cfg)
    price = g12["peak_price_pln_kwh"] if zone == "peak" else g12["offpeak_price_pln_kwh"]
    return float(price), zone


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
