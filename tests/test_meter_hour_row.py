"""Meter hour row builder: history and H0 carryover share one function."""

from datetime import datetime

from src.grid_config import merge_grid_defaults
from src.plan_hourly_actuals import build_h0_carryover_row, build_meter_hour_row
from src.simulation_config import merge_battery_defaults, merge_simulation_defaults


def _cfg() -> dict:
    cfg = {
        "battery": {"capacity_kwh": 20.0},
        "grid": {
            "g12": {
                "tariff_preset": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
            },
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def _params(cfg: dict) -> dict:
    return {"min_soc_pct": 20, "epsilon_kwh": 0.01}


def _hourly(**overrides) -> dict[str, list[float | None]]:
    base: dict[str, list[float | None]] = {
        "pv": [0.1] * 24,
        "load": [0.4] * 24,
        "bat_charge": [0.0] * 24,
        "bat_discharge": [0.0] * 24,
        "grid_buy": [-0.3] * 24,
        "grid_sell": [0.0] * 24,
        "soc": [50.0] * 24,
    }
    base.update(overrides)
    return base


def test_history_row_uses_clock_hour_and_interval_end():
    cfg = _cfg()
    hourly = _hourly()
    dt = datetime(2026, 9, 11, 6, 0)
    row = build_meter_hour_row(
        dt, hourly,
        cfg=cfg, params=_params(cfg), rce_price=0.4, plan_date="2026-09-11",
        extra_flags={"history_hour": True},
    )
    assert row is not None
    assert row["hour"] == 6
    assert row["plan_date"] == "2026-09-11"
    assert row["start"] == "11-09-2026 07:00"
    assert row["g12_zone"] == "peak"
    assert row["buy_price"] == 1.2444
    assert row["history_hour"] is True
    assert row["q15"]


def test_h0_carryover_reads_prev_h23_priced_as_hour0():
    cfg = _cfg()
    prev = _hourly()
    prev["soc"] = [None] * 23 + [42.0]
    row = build_h0_carryover_row(
        "2026-09-11", prev,
        forecast_pv=0.0, forecast_load=0.5,
        cfg=cfg, params=_params(cfg), rce_price=0.2,
    )
    assert row is not None
    assert row["hour"] == 0
    assert row["carryover_hour"] is True
    assert row["g12_zone"] == "offpeak"
    assert row["soc"] == 42.0
    assert "q15" not in row
    assert row["start"] == "11-09-2026 01:00"


def test_now_warsaw_matches_zoneinfo():
    from zoneinfo import ZoneInfo

    from src.influxdb import now_warsaw

    expected = datetime.now(ZoneInfo("Europe/Warsaw")).replace(tzinfo=None)
    got = now_warsaw()
    assert abs((got - expected).total_seconds()) < 2


def test_buy_tariff_start_is_interval_end(monkeypatch):
    from src import plan_simulation as ps

    monkeypatch.setattr(ps, "now_warsaw", lambda: datetime(2026, 9, 11, 10, 5))
    cfg = _cfg()
    rows = ps._compute_buy_tariff_rows(cfg)
    h10 = next(r for r in rows if r["hour"] == 10)
    assert h10["plan_date"] == "2026-09-11"
    assert h10["start"] == "11-09-2026 11:00"
    assert h10["g12_zone"] == "peak"
