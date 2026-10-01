"""Five evening ticks on the 2026-10-01 / 2026-10-02 EA table.

Inputs are the live forecast, hourly meters, and quarter RCE from that table.
Clock hours 17–19 use the supplied quarter prices. SOC at hour 16 is 61%.
Hour 17 is 59%: house load only, no export. Later ticks continue that
simulation. Each tick is the production plan plus the SQLite merge, so a
Timer Schedule already in the past stays put.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.grid_config import merge_grid_defaults
from src.plan_cache_merge import merge_incremental_plan
from src.simulation import build_energy_arbitrage_plan
from src.simulation_config import merge_battery_defaults, merge_simulation_defaults, merge_timer_schedule_defaults
from src.timer_plan import parse_timer_schedule_segments

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-10-01"
TOMORROW = "2026-10-02"
TICKS = (
    datetime(2026, 10, 1, 17, 30, tzinfo=TZ),
    datetime(2026, 10, 1, 18, 30, tzinfo=TZ),
    datetime(2026, 10, 1, 19, 30, tzinfo=TZ),
    datetime(2026, 10, 1, 20, 30, tzinfo=TZ),
    datetime(2026, 10, 1, 21, 30, tzinfo=TZ),
)
# Hour 16 metered. Hour 17 is house-load only (no export); the optimizer
# designs discharge from that level. Hours 18+ follow the simulation.
SOC_H16 = 61.0
SOC_H17_LOAD_ONLY = 59.0
RCE_BY_HOUR = {
    17: [0.920, 0.887, 0.922, 1.037],
    18: [0.968, 1.073, 1.179, 1.394],
    19: [1.264, 1.185, 1.139, 1.012],
}
_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ea_2026-10-01.json").read_text(encoding="utf-8")
)


def _cfg() -> dict:
    cfg = {
        "battery": {
            "capacity_kwh": 47.6,
            "max_charge_power_kw": 9.2,
            "max_discharge_power_kw": 8.0,
        },
        "inverter": {"ac_capacity_kw": 8.0},
        "simulation": {
            "min_soc_pct": 16,
            "horizon_hours": 24,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
                "pv_to_battery": 7.5,
            },
        },
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.68,
            "export_window_start_hour": 16,
            "g12": {
                "tariff_name": "Energa G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.7182,
                "offpeak_energy_only_pln_kwh": 0.4678,
                "peak_hours_weekday": [[6, 13], [15, 22]],
            },
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    merge_timer_schedule_defaults(cfg)
    return cfg


def _rce_pair() -> dict[str, list[float]]:
    today = [float(v) for v in _FIXTURE["rce_today"]]
    for hour, prices in RCE_BY_HOUR.items():
        for q, price in enumerate(prices):
            today[hour * 4 + q] = float(price)
    tomorrow = [float(v) for v in _FIXTURE["rce_tomorrow"]]
    return {TODAY: today, TOMORROW: tomorrow}


def _forecast() -> dict:
    pv_t = [float(v) for v in _FIXTURE["pv_forecast_today"]]
    load_t = [float(v) for v in _FIXTURE["load_forecast_today"]]
    pv_n = [float(v) for v in _FIXTURE["pv_forecast_tomorrow"]]
    load_n = [float(v) for v in _FIXTURE["load_forecast_tomorrow"]]
    return {
        "today": {
            "pv": pv_t,
            "load": load_t,
            "pv_forecast": pv_t,
            "load_forecast": load_t,
            "pv_total": sum(pv_t),
            "load_total": sum(load_t),
        },
        "tomorrow": {
            "pv": pv_n,
            "load": load_n,
            "pv_forecast": pv_n,
            "load_forecast": load_n,
            "pv_total": sum(pv_n),
            "load_total": sum(load_n),
        },
    }


def _row_soc_pct(row: dict, quarter: int | None = None) -> float | None:
    """End SOC % of a plan row, or of one quarter (0–3) when given."""
    slots = [s for s in (row.get("q15") or []) if s.get("quarter") is not None]
    if quarter is not None:
        for slot in slots:
            if int(slot["quarter"]) == quarter:
                try:
                    return float(slot.get("soc"))
                except (TypeError, ValueError):
                    return None
        return None
    if slots:
        last = max(slots, key=lambda s: int(s["quarter"]))
        try:
            return float(last.get("soc"))
        except (TypeError, ValueError):
            return None
    try:
        return float(row.get("soc"))
    except (TypeError, ValueError):
        return None


def _today_row(plan: dict | None, hour: int) -> dict | None:
    if not plan:
        return None
    for row in (plan.get("history_rows") or []) + (plan.get("rows") or []):
        if row.get("start") == "TOTAL" or row.get("hour") is None:
            continue
        if str(row.get("plan_date") or "") != TODAY:
            continue
        if int(row["hour"]) == hour:
            return row
    return None


def _live_soc_pct(stored: dict | None, tick: datetime) -> float:
    """Meter at this tick: 59% before any export, else the simulated quarter."""
    if stored is None:
        return SOC_H17_LOAD_ONLY
    # :30 is the end of quarter 1 (15–30).
    quarter_done = tick.minute // 15 - 1
    row = _today_row(stored, tick.hour)
    if row is not None and quarter_done >= 0:
        pct = _row_soc_pct(row, quarter_done)
        if pct is not None:
            return pct
    prev = _today_row(stored, tick.hour - 1)
    if prev is not None:
        pct = _row_soc_pct(prev)
        if pct is not None:
            return pct
    return SOC_H17_LOAD_ONLY


def _hourly_soc(tick_hour: int, stored: dict | None) -> list[float]:
    soc = [float(v) if v is not None else 50.0 for v in _FIXTURE["hourly_soc"]]
    soc[16] = SOC_H16
    for hour in range(17, tick_hour):
        row = _today_row(stored, hour)
        pct = _row_soc_pct(row) if row is not None else None
        soc[hour] = float(pct) if pct is not None else SOC_H17_LOAD_ONLY
    return soc


def _metrics(tick: datetime, stored: dict | None) -> dict:
    return {
        "battery_soc": _live_soc_pct(stored, tick),
        "today_hourly": {
            "pv": [float(v or 0) for v in _FIXTURE["hourly_pv"]],
            "load": [float(v or 0) for v in _FIXTURE["hourly_load"]],
            "soc": _hourly_soc(tick.hour, stored),
            "bat_charge": [0.0] * 24,
            "bat_discharge": [0.0] * 24,
            "grid_buy": [0.0] * 24,
            "grid_sell": [0.0] * 24,
        },
        "series_10min": None,
    }


def _timers(plan: dict) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for row in (plan.get("history_rows") or []) + (plan.get("rows") or []):
        if row.get("start") == "TOTAL" or row.get("hour") is None:
            continue
        date = str(row.get("plan_date") or "")
        if date not in (TODAY, TOMORROW):
            continue
        out[(date, int(row["hour"]))] = str(row.get("timer_schedule") or "").strip()
    return out


def _dis_spans(timers: dict[tuple[str, int], str]) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for (date, _hour), text in timers.items():
        day = 0 if date == TODAY else 1440
        for seg in parse_timer_schedule_segments(text):
            if seg.get("kind") != "dis":
                continue
            fh, fm = str(seg["from"]).split(":")
            th, tm = str(seg["to"]).split(":")
            start = day + int(fh) * 60 + int(fm)
            end = day + int(th) * 60 + int(tm)
            if end <= start:
                end += 1440
            spans.append((start, end, f"{date} {text}"))
    spans.sort()
    return spans


def _assert_export_gaps(timers: dict[tuple[str, int], str], tick: datetime) -> None:
    spans = _dis_spans(timers)
    bad = []
    for left, right in zip(spans, spans[1:]):
        gap = right[0] - left[1]
        if 0 < gap < 30:
            bad.append(f"{gap} min before {right[2]}")
    assert not bad, f"{tick:%H:%M} export pause under 30 min: {bad}\n{_fmt(timers)}"


def _fmt(timers: dict[tuple[str, int], str]) -> str:
    lines = []
    for (date, hour), text in sorted(timers.items()):
        if text:
            lines.append(f"  {date} H{hour:02d} {text}")
    return "\n".join(lines) or "  (no timers)"


def test_oct1_evening_ticks_keep_past_discharge_windows():
    """17:30..21:30 from H16=61% and load-only H17=59%. Past Dis windows do not move."""
    cfg = _cfg()
    rce = _rce_pair()
    forecast = _forecast()
    stored: dict | None = None
    previous: dict[tuple[str, int], str] | None = None

    for tick in TICKS:
        metrics = _metrics(tick, stored)
        assert metrics["today_hourly"]["soc"][16] == SOC_H16
        if tick.hour == 17:
            assert metrics["battery_soc"] == SOC_H17_LOAD_ONLY
        with (
            patch("src.simulation._now_warsaw", return_value=tick),
            patch("src.sqlite_store.read_plan", return_value=stored),
            patch("src.simulation.quarter_rce_for_dates", return_value=rce),
        ):
            fresh = build_energy_arbitrage_plan(forecast, metrics, {}, cfg, now=tick)
        base = stored or {
            "today_date": TODAY,
            "plan_from_hour": tick.hour,
            "rows": [],
            "history_rows": [],
        }
        stored = merge_incremental_plan(
            base, fresh, now=tick, metrics=metrics, cfg=cfg, rules={},
        )
        timers = _timers(stored)
        _assert_export_gaps(timers, tick)
        assert timers.get((TODAY, 18)) == "Dis 18:00-19:00 8.0kW cap41%", (
            f"{tick:%H:%M} H18\n{_fmt(timers)}"
        )
        assert not str(timers.get((TODAY, 17)) or "").startswith("Dis"), (
            f"{tick:%H:%M} H17 exported\n{_fmt(timers)}"
        )
        if previous is not None:
            moved = []
            for (date, hour), text in timers.items():
                if date != TODAY or hour >= tick.hour:
                    continue
                before = previous.get((date, hour), "")
                if before != text:
                    moved.append(f"H{hour:02d} {before!r} -> {text!r}")
            assert not moved, f"{tick:%H:%M} past Timer Schedule moved:\n" + "\n".join(moved)
        previous = timers
