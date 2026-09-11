"""Grid tariff defaults: G12 distribution fees (brutto) and export threshold."""

from __future__ import annotations

from typing import Any

VAT_BRUTTO_MULTIPLIER = 1.23

# Bump when billing formulas change (invalidates cached month_history).
BILLING_MODEL_VERSION = "17"

# Official Energa G12 distribution tariff (invoice table 2 — ROZLICZENIE DYSTRYBUCJI).
# All values netto from invoice × VAT_BRUTTO_MULTIPLIER, PLN brutto, 4 dp.
DEFAULT_DISTRIBUTION: dict[str, float] = {
    "subscription_pln_month": round(0.74 * VAT_BRUTTO_MULTIPLIER, 4),
    "fixed_network_pln_month": round(20.17 * VAT_BRUTTO_MULTIPLIER, 4),
    "capacity_pln_month": round(24.05 * VAT_BRUTTO_MULTIPLIER, 4),
    # Opłata sieciowa zmienna dzienna (L1) / nocna (L2) — Service Cost in Monthly history.
    "peak_variable_network_pln_kwh": round(0.3844 * VAT_BRUTTO_MULTIPLIER, 4),
    "offpeak_variable_network_pln_kwh": round(0.0827 * VAT_BRUTTO_MULTIPLIER, 4),
    # Opłata jakościowa, OZE, kogeneracyjna — per total import kWh (Service Fee variable part).
    "quality_pln_kwh": round(0.0332 * VAT_BRUTTO_MULTIPLIER, 4),
    "oze_pln_kwh": round(0.0073 * VAT_BRUTTO_MULTIPLIER, 4),
    "cogeneration_pln_kwh": round(0.0030 * VAT_BRUTTO_MULTIPLIER, 4),
}


def merge_grid_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure grid.g12, grid.distribution, and grid_export_threshold exist."""
    grid = cfg.setdefault("grid", {})
    g12 = grid.setdefault("g12", {})
    g12.setdefault("tariff_name", "G12")
    g12.setdefault("tariff_preset", "G12")
    g12.setdefault("peak_price_pln_kwh", 1.0)
    g12.setdefault("offpeak_price_pln_kwh", 0.5)
    g12.setdefault("peak_energy_only_pln_kwh", 0.6)
    g12.setdefault("offpeak_energy_only_pln_kwh", 0.4)

    dist = grid.setdefault("distribution", {})
    for key, val in DEFAULT_DISTRIBUTION.items():
        dist.setdefault(key, val)

    if grid.get("grid_export_threshold_pln_kwh") is None:
        grid["grid_export_threshold_pln_kwh"] = float(g12["offpeak_price_pln_kwh"])
    else:
        grid["grid_export_threshold_pln_kwh"] = round(
            float(grid["grid_export_threshold_pln_kwh"]), 4,
        )

    if grid.get("export_window_start_hour") is None:
        grid["export_window_start_hour"] = 16
    else:
        grid["export_window_start_hour"] = max(
            0, min(23, int(grid["export_window_start_hour"])),
        )

    return cfg


def grid_export_threshold_pln_kwh(cfg: dict[str, Any]) -> float:
    merge_grid_defaults(cfg)
    return float(cfg["grid"]["grid_export_threshold_pln_kwh"])


def export_window_start_hour(cfg: dict[str, Any] | None = None) -> int:
    """Clock hour when the battery→grid sale window opens (default 16)."""
    if cfg is None:
        return 16
    merge_grid_defaults(cfg)
    return int(cfg["grid"]["export_window_start_hour"])


def compute_service_fee_pln(total_import_kwh: float, cfg: dict[str, Any]) -> float:
    """Monthly distribution fees (fixed + per-kWh on total import), PLN brutto."""
    merge_grid_defaults(cfg)
    dist = cfg["grid"]["distribution"]
    imp = max(0.0, float(total_import_kwh))
    fixed = (
        float(dist["subscription_pln_month"])
        + float(dist["fixed_network_pln_month"])
        + float(dist["capacity_pln_month"])
    )
    per_kwh_rate = (
        float(dist["quality_pln_kwh"])
        + float(dist["oze_pln_kwh"])
        + float(dist["cogeneration_pln_kwh"])
    )
    return round(fixed + imp * per_kwh_rate, 4)
