"""Monthly history daily totals split grid import by G12 zone."""

from src.plan_monthly_history import _summarize_day_rows


def test_summarize_splits_grid_import_by_g12_zone():
    rows = [
        {
            "production": 0.0,
            "consumption": 1.0,
            "grid_import": 2.0,
            "grid_export": 0.0,
            "energy_cost": 0.0,
            "export_revenue": 0.0,
            "service_cost": 0.0,
            "g12_zone": "peak",
        },
        {
            "production": 0.0,
            "consumption": 1.0,
            "grid_import": 3.0,
            "grid_export": 0.0,
            "energy_cost": 0.0,
            "export_revenue": 0.0,
            "service_cost": 0.0,
            "g12_zone": "offpeak",
        },
    ]
    summary = _summarize_day_rows(rows, "2026-08-03")
    assert summary["grid_import"] == 5.0
    assert summary["grid_import_peak"] == 2.0
    assert summary["grid_import_offpeak"] == 3.0
