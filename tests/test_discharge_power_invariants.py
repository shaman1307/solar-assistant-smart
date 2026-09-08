"""Full-day replays guarding planned battery discharge power vs config.

Ranked export fills middle hours of a multi-hour window at max config power.
The last hour in the run may be a partial-power Dis tail when leftover still
exceeds the overnight survive floor; otherwise export stops earlier and hour 23
only feeds the house so morning min SOC is reachable.

These tests replay a whole day with today's real generation/consumption/RCE
(2026-07-19 values from the Pi) and after every rebuild assert:

  P1. No Dis timer exceeds the configured discharge power.
  P2. DC draw never exceeds max_discharge_power_kw / 4 per quarter.
  P3. Evening Dis ends at post_dis(last); leftover above that floor may use H23.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from src.plan_q15 import run_day_smart_q15_plan, timer_schedule_by_hour
from src.grid_config import merge_grid_defaults
from src.plan_optimizer import post_discharge_reserve_soc_kwh
from src.simulation_config import (
    get_simulation_params,
    merge_simulation_defaults,
    plan_reserve_min_soc_kwh,
    plan_timer_discharge_power_kw,
)

DATE = "2026-07-19"

# Hourly forecast as served by the Pi on 2026-07-19 (kWh).
PV_TODAY = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.03, 0.55, 1.22, 1.88, 3.51, 6.51, 5.78,
    5.3, 6.87, 3.35, 3.81, 2.77, 1.54, 0.64, 0.22, 0.0, 0.0, 0.0, 0.0,
]
LOAD_TODAY = [
    0.87, 0.94, 0.85, 0.64, 0.54, 0.55, 0.54, 0.48, 0.54, 0.62, 0.66, 0.74,
    0.69, 0.66, 0.95, 0.72, 0.52, 0.53, 0.58, 0.59, 0.56, 1.04, 1.11, 0.98,
]

# Quarter-hour RCE (PLN/kWh) for the same day: cheap PV midday, rich evening.
RCE_Q = [
    0.8186, 0.7422, 0.7111, 0.6629,   # 00
    0.7194, 0.6944, 0.6552, 0.6457,   # 01
    0.6781, 0.6457, 0.6355, 0.6403,   # 02
    0.6483, 0.6418, 0.6415, 0.6358,   # 03
    0.6396, 0.6327, 0.6138, 0.6042,   # 04
    0.6225, 0.6118, 0.6109, 0.5893,   # 05
    0.6035, 0.5889, 0.5881, 0.5546,   # 06
    0.5754, 0.5591, 0.5314, 0.4914,   # 07
    0.4524, 0.3606, 0.2034, 0.1045,   # 08
    0.1333, 0.0901, 0.0038, 0.038,    # 09
    0.0002, 0.01, 0.0193, 0.0403,     # 10
    0.0142, 0.0053, 0.0227, 0.0002,   # 11
    0.0027, 0.0059, 0.0105, 0.0093,   # 12
    0.0031, 0.0007, 0.0099, 0.0405,   # 13
    0.0015, 0.0192, 0.1238, 0.226,    # 14
    0.2014, 0.2852, 0.2952, 0.4257,   # 15
    0.5336, 0.5375, 0.5571, 0.5802,   # 16
    0.7096, 0.5954, 0.5833, 0.7019,   # 17
    0.5793, 0.7334, 0.7098, 0.6819,   # 18
    0.6432, 0.6736, 0.7998, 0.819,    # 19
    0.7293, 0.7969, 0.8413, 0.8359,   # 20
    0.8495, 0.8215, 0.797, 0.7574,    # 21
    0.8536, 0.7974, 0.7788, 0.7501,   # 22
    0.8328, 0.7949, 0.7312, 0.6844,   # 23
]

_DIS_RE = re.compile(r"^Dis \d{2}:\d{2}-\d{2}:\d{2} (?P<kw>\d+(?:\.\d+)?)kW cap\d+%$")


def _cfg(max_discharge_kw: float = 8.0) -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 43.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": max_discharge_kw,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
        "grid": {},
    }
    merge_grid_defaults(cfg)
    return merge_simulation_defaults(cfg)


def _run_plan(cfg: dict, *, from_hour: int, soc_kwh: float) -> dict:
    res = run_day_smart_q15_plan(
        date_str=DATE,
        pv_hourly=PV_TODAY,
        load_hourly=LOAD_TODAY,
        tomorrow_pv=PV_TODAY,
        tomorrow_load=LOAD_TODAY,
        cfg=cfg,
        rce_quarters=list(RCE_Q),
        initial_soc_kwh=soc_kwh,
        from_hour=from_hour,
    )
    assert res is not None
    return res


def _assert_discharge_power_invariants(res: dict, cfg: dict, context: str) -> None:
    dis_cap = plan_timer_discharge_power_kw(cfg)
    dc_step = float(cfg["battery"]["max_discharge_power_kw"]) / 4.0
    eps = float(res["epsilon"])

    timers = timer_schedule_by_hour(res["q15_by_hour"], cfg, eps)
    for hour, txt in timers.items():
        if not txt or not txt.startswith("Dis"):
            continue
        m = _DIS_RE.match(txt)
        assert m, f"{context}: unparseable Dis timer for hour {hour}: {txt!r}"
        kw = float(m.group("kw"))
        # P1: never above the config limit (partial kW below max is allowed).
        assert kw <= dis_cap + 0.01, (
            f"{context}: hour {hour} timer {txt!r} exceeds config "
            f"max_discharge_power_kw={dis_cap}"
        )

    # P2: never discharge above the DC step.
    for hour, slots in res["q15_by_hour"].items():
        for s in slots:
            if float(s.get("battery_export_kwh") or 0) <= eps:
                continue
            dc = -float(s.get("battery_delta") or 0)
            assert dc <= dc_step + eps, (
                f"{context}: hour {hour} q{s['quarter']} discharges {dc:.3f} kWh "
                f"per quarter — above config step {dc_step:.3f}"
            )


@pytest.mark.parametrize("start_soc_pct", [16, 30, 45, 62, 90])
def test_full_day_hourly_rebuilds_keep_config_discharge_power(start_soc_pct: int):
    """Chronological day replay: rebuild at every hour, SOC carried forward."""
    cfg = _cfg()
    soc = start_soc_pct / 100.0 * float(cfg["battery"]["capacity_kwh"])

    for hour in range(24):
        res = _run_plan(cfg, from_hour=hour, soc_kwh=soc)
        _assert_discharge_power_invariants(
            res, cfg, f"start {start_soc_pct}%, rebuild @{hour:02d}:00",
        )
        hour_slots = res["q15_by_hour"].get(hour) or []
        if hour_slots:
            soc = float(hour_slots[-1]["soc_end"])


def test_lower_config_limit_is_respected_all_day():
    """With max_discharge_power_kw=5 every middle Dis timer must say 5.0kW."""
    cfg = _cfg(max_discharge_kw=5.0)
    soc = 0.62 * float(cfg["battery"]["capacity_kwh"])

    for hour in range(0, 24, 4):
        res = _run_plan(cfg, from_hour=hour, soc_kwh=soc)
        _assert_discharge_power_invariants(res, cfg, f"5kW cap, rebuild @{hour:02d}:00")
        hour_slots = res["q15_by_hour"].get(hour) or []
        if hour_slots:
            soc = float(hour_slots[-1]["soc_end"])


def test_evening_export_stops_at_overnight_survive_floor():
    """Sell evening Dis down to post_dis(last); leftover above that floor may use H23.

    Ranked window on this fixture is H20+ at 8 kW (last hour may be a short
    tail). After the last Dis hour, end SOC equals post_dis(last). The next-day
    replay lands on min SOC before morning PV.
    """
    cfg = _cfg()
    cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_reserve_min_soc_kwh(cfg)
    params = get_simulation_params(cfg)
    eta_out = float(params["eta_battery_out"])
    eta_pv = float(params["eta_pv_load"])
    eps = float(params["epsilon_kwh"])

    res = _run_plan(cfg, from_hour=0, soc_kwh=0.30 * cap)
    timers = timer_schedule_by_hour(res["q15_by_hour"], cfg, res["epsilon"])
    evening = [h for h in range(19, 24) if (timers.get(h) or "").startswith("Dis")]
    assert evening, (
        f"expected evening Dis window, got timers="
        f"{[timers.get(h) for h in range(19, 24)]}"
    )
    last_dis_h = max(evening)
    assert last_dis_h >= 20, (
        f"expected evening Dis to reach at least H20, got through H{last_dis_h}; "
        f"timers={[timers.get(h) for h in range(19, 24)]}"
    )

    # Extended today+tomorrow q15 series matches optimizer overnight walk.
    pv_ext = [PV_TODAY[h] / 4.0 for h in range(24) for _ in range(4)] * 2
    load_ext = [LOAD_TODAY[h] / 4.0 for h in range(24) for _ in range(4)] * 2
    floor = post_discharge_reserve_soc_kwh(
        last_dis_h, pv_ext, load_ext, min_kwh, eta_out, eta_pv, eps,
        slots_per_hour=4, global_step_offset=0,
    )
    last_slots = res["q15_by_hour"].get(last_dis_h) or []
    assert last_slots, f"missing q15 slots for last Dis hour {last_dis_h}"
    soc_after_last = float(last_slots[-1]["soc_end"])
    min_hourly = float(cfg["timer_schedule"]["min_hourly_transfer_kwh"])
    assert soc_after_last >= floor - 0.15, (
        f"after H{last_dis_h} SOC {soc_after_last:.3f} dipped below post_dis={floor:.3f}; "
        f"timers={[timers.get(h) for h in range(19, 24)]}"
    )
    leftover = soc_after_last - floor
    assert leftover < min_hourly + 0.15, (
        f"after H{last_dis_h} leftover {leftover:.3f} kWh above post_dis={floor:.3f} "
        f"exceeds min hourly block {min_hourly}; "
        f"timers={[timers.get(h) for h in range(19, 24)]}"
    )

    # Next calendar day from tonight end SOC: coast to min before morning PV cover.
    end_soc = float(res["end_soc_kwh"])
    tom = (date.fromisoformat(DATE) + timedelta(days=1)).isoformat()
    res2 = run_day_smart_q15_plan(
        date_str=tom,
        pv_hourly=PV_TODAY,
        load_hourly=LOAD_TODAY,
        tomorrow_pv=PV_TODAY,
        tomorrow_load=LOAD_TODAY,
        cfg=cfg,
        rce_quarters=list(RCE_Q),
        initial_soc_kwh=end_soc,
        from_hour=0,
    )
    assert res2 is not None
    morning_floor_hours = []
    for h in range(0, 8):
        slots = res2["q15_by_hour"].get(h) or []
        if not slots:
            continue
        soc_e = float(slots[-1]["soc_end"])
        morning_floor_hours.append((h, soc_e))
        if soc_e <= min_kwh + 0.15:
            break
    assert morning_floor_hours, "tomorrow morning SOC walk missing"
    hit_h, hit_soc = morning_floor_hours[-1]
    assert hit_soc < end_soc - 1.0, (
        f"overnight from {100 * end_soc / cap:.1f}% should fall on house load, "
        f"last checked H{hit_h}={hit_soc:.3f} (start={end_soc:.3f})"
    )
