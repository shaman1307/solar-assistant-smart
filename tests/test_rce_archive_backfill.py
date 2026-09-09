"""Fill missing archived EA RCE quarters from PSE."""

from src.grid_config import merge_grid_defaults
from src.plan_hourly_actuals import backfill_history_rows_rce, history_rows_have_rce_holes
from src.plan_simulation import apply_rce_backfill_to_ea_payload


def _cfg() -> dict:
    return merge_grid_defaults({
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


def test_backfill_fills_empty_hour_from_pse_quarters():
    cfg = _cfg()
    rows = [{
        "plan_date": "2026-08-22",
        "hour": 19,
        "start": "22-08-2026 20:00",
        "grid_import": 0.0,
        "grid_export": 1.0,
        "buy_price": 1.2444,
        "g12_zone": "peak",
        "rce_price": None,
        "rce_q15": [None, None, None, None],
        "energy_cost": 0.0,
    }]
    quarters = {"2026-08-22": [None] * 96}
    quarters["2026-08-22"][19 * 4:20 * 4] = [0.6795, 0.7344, 0.805, 0.8473]
    out, filled = backfill_history_rows_rce(rows, quarters, cfg)
    assert filled == [("2026-08-22", 19)]
    assert out[0]["rce_q15"] == [0.6795, 0.7344, 0.805, 0.8473]
    mean = round((0.6795 + 0.7344 + 0.805 + 0.8473) / 4, 4)
    assert out[0]["rce_price"] == mean
    assert out[0]["export_revenue"] == round(1.0 * mean, 4)


def test_backfill_keeps_existing_quarters():
    cfg = _cfg()
    rows = [{
        "plan_date": "2026-08-22",
        "hour": 18,
        "start": "22-08-2026 19:00",
        "grid_import": 0.0,
        "grid_export": 0.0,
        "buy_price": 1.2444,
        "g12_zone": "peak",
        "rce_price": 0.642,
        "rce_q15": [0.5475, 0.5782, 0.7297, 0.7127],
    }]
    quarters = {"2026-08-22": [0.9] * 96}
    out, filled = backfill_history_rows_rce(rows, quarters, cfg)
    assert filled == []
    assert out[0]["rce_q15"] == [0.5475, 0.5782, 0.7297, 0.7127]


def test_payload_backfill_updates_totals():
    cfg = _cfg()
    payload = {
        "history_rows": [{
            "plan_date": "2026-08-22",
            "hour": 19,
            "start": "22-08-2026 20:00",
            "grid_import": 0.0,
            "grid_export": 2.0,
            "buy_price": 1.2444,
            "g12_zone": "peak",
            "rce_price": None,
            "rce_q15": [None, None, None, None],
        }],
        "rows": [],
    }
    quarters = {"2026-08-22": [None] * 96}
    quarters["2026-08-22"][19 * 4:20 * 4] = [0.5, 0.5, 0.5, 0.5]
    out, filled = apply_rce_backfill_to_ea_payload(payload, quarters, cfg)
    assert filled == [("2026-08-22", 19)]
    assert out["totals"]["export_revenue"] == round(2.0 * 0.5, 4)


def test_history_rows_have_rce_holes():
    empty = {
        "hour": 19,
        "rce_q15": [None, None, None, None],
    }
    full = {
        "hour": 18,
        "rce_q15": [0.5, 0.5, 0.5, 0.5],
    }
    assert history_rows_have_rce_holes([empty]) is True
    assert history_rows_have_rce_holes([full]) is False
    assert history_rows_have_rce_holes([full, empty]) is True
    assert history_rows_have_rce_holes([]) is False


def test_backfill_fills_partial_hour():
    cfg = _cfg()
    rows = [{
        "plan_date": "2026-08-22",
        "hour": 19,
        "start": "22-08-2026 20:00",
        "grid_import": 0.0,
        "grid_export": 0.0,
        "buy_price": 1.2444,
        "g12_zone": "peak",
        "rce_price": 0.6795,
        "rce_q15": [0.6795, None, None, None],
    }]
    quarters = {"2026-08-22": [None] * 96}
    quarters["2026-08-22"][19 * 4:20 * 4] = [0.9, 0.7344, 0.805, 0.8473]
    out, filled = backfill_history_rows_rce(rows, quarters, cfg)
    assert filled == [("2026-08-22", 19)]
    assert out[0]["rce_q15"] == [0.6795, 0.7344, 0.805, 0.8473]
