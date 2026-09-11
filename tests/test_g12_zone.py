"""G12 vs G12w peak windows (Energa Operator 2026 §3.2.5 / §3.2.6)."""

from datetime import datetime

from src.g12_pricing import get_g12_zone
from src.grid_config import merge_grid_defaults


def _cfg(preset: str) -> dict:
    return merge_grid_defaults({"grid": {"g12": {"tariff_preset": preset}}})


def test_g12_saturday_daytime_is_peak():
    dt = datetime(2026, 8, 1, 10, 0)  # Saturday
    assert get_g12_zone(dt, _cfg("G12")) == "peak"


def test_g12_sunday_daytime_is_peak():
    dt = datetime(2026, 8, 2, 16, 0)  # Sunday
    assert get_g12_zone(dt, _cfg("G12")) == "peak"


def test_g12_weekday_afternoon_gap_is_offpeak():
    dt = datetime(2026, 8, 3, 14, 0)  # Monday 13–15
    assert get_g12_zone(dt, _cfg("G12")) == "offpeak"


def test_g12_weekday_night_is_offpeak():
    dt = datetime(2026, 8, 3, 22, 0)
    assert get_g12_zone(dt, _cfg("G12")) == "offpeak"


def test_g12w_saturday_daytime_is_offpeak():
    dt = datetime(2026, 8, 1, 10, 0)  # Saturday
    assert get_g12_zone(dt, _cfg("G12w")) == "offpeak"


def test_g12w_monday_daytime_is_peak():
    dt = datetime(2026, 8, 3, 10, 0)
    assert get_g12_zone(dt, _cfg("G12w")) == "peak"


def test_g12_uses_hardcoded_preset_windows():
    cfg = merge_grid_defaults({
        "grid": {"g12": {"tariff_preset": "G12", "peak_hours_weekday": [[7, 13]]}},
    })
    assert get_g12_zone(datetime(2026, 8, 1, 6, 0), cfg) == "peak"
    assert get_g12_zone(datetime(2026, 8, 1, 7, 0), cfg) == "peak"
    assert get_g12_zone(datetime(2026, 8, 1, 13, 0), cfg) == "offpeak"


def test_g11_has_no_peak_hours():
    dt = datetime(2026, 8, 3, 10, 0)
    assert get_g12_zone(dt, _cfg("G11")) == "offpeak"


def test_get_buy_price_reads_config_rates():
    from src.g12_pricing import get_buy_price

    cfg = merge_grid_defaults({
        "grid": {
            "g12": {
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
            },
        },
    })
    peak, zone_p = get_buy_price(datetime(2026, 8, 1, 10, 0), cfg)
    off, zone_o = get_buy_price(datetime(2026, 8, 1, 14, 0), cfg)
    assert zone_p == "peak" and peak == 1.2444
    assert zone_o == "offpeak" and off == 0.6229


def test_g12_hours_for_dates_g12_windows():
    from src.g12_pricing import g12_hours_for_dates

    cfg = merge_grid_defaults({
        "grid": {
            "g12": {
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
            },
        },
    })
    rows = g12_hours_for_dates(["2026-09-11"], cfg)
    assert len(rows) == 24
    by_h = {r["hour"]: r for r in rows}
    assert by_h[5]["zone"] == "offpeak"
    assert by_h[6]["zone"] == "peak" and by_h[6]["buy_price"] == 1.2444
    assert by_h[12]["zone"] == "peak"
    assert by_h[13]["zone"] == "offpeak"
    assert by_h[14]["zone"] == "offpeak"
    assert by_h[15]["zone"] == "peak"
    assert by_h[21]["zone"] == "peak"
    assert by_h[22]["zone"] == "offpeak" and by_h[22]["buy_price"] == 0.6229


def test_rce_attach_g12_hours_today_tomorrow(monkeypatch):
    from src import rce as rce_mod

    cfg = merge_grid_defaults({
        "grid": {
            "g12": {
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
            },
        },
    })
    monkeypatch.setattr("src.config.load_config", lambda: cfg)
    data = {"dates": {"today": "2026-09-11", "tomorrow": "2026-09-12"}}
    out = rce_mod._attach_g12_hours(data)
    assert len(out["g12_hours"]) == 48
    by = {(r["date"], r["hour"]): r for r in out["g12_hours"]}
    assert by[("2026-09-11", 6)]["zone"] == "peak"
    assert by[("2026-09-11", 13)]["zone"] == "offpeak"
    assert by[("2026-09-11", 15)]["zone"] == "peak"
    assert by[("2026-09-11", 22)]["zone"] == "offpeak"


def test_reprice_saturday_archive_row_uses_g12_peak():
    from src.plan_hourly_actuals import reprice_history_rows_to_current_g12

    cfg = merge_grid_defaults({
        "grid": {
            "g12": {
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.7182,
                "offpeak_energy_only_pln_kwh": 0.4678,
            },
        },
    })
    rows = [{
        "plan_date": "2026-09-05",
        "hour": 10,
        "start": "05-09-2026 11:00",
        "grid_import": 1.0,
        "grid_export": 0.0,
        "buy_price": 0.6229,
        "g12_zone": "offpeak",
        "rce_price": 0.4,
    }]
    out, changed = reprice_history_rows_to_current_g12(rows, cfg)
    assert changed
    assert out[0]["g12_zone"] == "peak"
    assert out[0]["buy_price"] == 1.2444
    assert out[0]["import_cost"] == round(1.2444, 4)


def test_apply_current_g12_reprices_archive_payload_totals():
    from src.plan_simulation import apply_current_g12_to_ea_payload

    cfg = merge_grid_defaults({
        "grid": {
            "g12": {
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.7182,
                "offpeak_energy_only_pln_kwh": 0.4678,
            },
        },
    })
    payload = {
        "history_rows": [{
            "plan_date": "2026-09-05",
            "hour": 16,
            "start": "05-09-2026 17:00",
            "grid_import": 2.0,
            "grid_export": 0.0,
            "buy_price": 0.6229,
            "g12_zone": "offpeak",
            "rce_price": None,
            "import_cost": 1.2458,
            "energy_cost": 0.9356,
        }],
        "rows": [],
    }
    out, changed = apply_current_g12_to_ea_payload(payload, cfg)
    assert changed
    row = out["history_rows"][0]
    assert row["g12_zone"] == "peak"
    assert row["buy_price"] == 1.2444
    assert out["totals"]["import_cost"] == round(2.0 * 1.2444, 4)
