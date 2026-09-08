"""Jul 25 Energy arbitrage from 16:00 — survive reserve leaves SOC above min until morning PV.

Replay uses Pi forecast / RCE as of 2026-07-25 evening. At 16:00 the discharge
window is still ahead; the new post-Dis reserve must:

  1. Write Dis timer capacity_pct above min_soc (not cap16%).
  2. Leave enough SOC after the last Dis hour so overnight house load does not
     hit min_soc before the first next-day hour where PV covers load (~07:00).
"""

from __future__ import annotations

import re

import pytest

from src.plan_q15 import run_day_smart_q15_plan, timer_schedule_by_hour
from src.grid_config import merge_grid_defaults
from src.plan_optimizer import HourControl, post_discharge_reserve_soc_kwh, simulate_hour
from src.plan_spill import pv_load_energy_split
from src.simulation_config import (
    get_simulation_params,
    merge_simulation_defaults,
    plan_min_soc_kwh,
    plan_min_soc_pct,
    plan_reserve_min_soc_kwh,
    plan_timer_discharge_power_kw,
)

DATE = "2026-07-25"
DATE_NEXT = "2026-07-26"

# Pi /api/forecast on 2026-07-25 (kWh).
PV_TODAY = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.012, 0.356, 1.131, 1.498, 2.904, 3.511, 3.181,
    4.065, 3.499, 4.122, 4.153, 4.419, 2.352, 1.245, 0.169, 0.0, 0.0, 0.0, 0.0,
]
LOAD_TODAY = [
    0.79, 0.698, 0.942, 0.689, 0.53, 0.509, 0.519, 0.52, 0.529, 0.59, 0.645, 0.759,
    0.698, 0.567, 0.544, 0.529, 0.645, 0.567, 0.523, 0.954, 0.943, 1.18, 1.058, 1.12,
]
PV_TOMORROW = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.11, 0.474, 0.842, 1.586, 2.91, 4.106, 4.395,
    4.723, 4.74, 4.322, 4.148, 3.66, 1.636, 1.079, 0.353, 0.0, 0.0, 0.0, 0.0,
]
LOAD_TOMORROW = [
    0.92, 0.999, 0.847, 0.635, 0.526, 0.531, 0.561, 0.527, 0.534, 0.622, 0.612, 0.771,
    0.7, 0.75, 0.928, 0.683, 0.538, 0.548, 0.584, 0.591, 0.707, 1.05, 1.276, 0.984,
]

# Pi /api/rce series_15min for 2026-07-25 (today + tomorrow).
RCE_Q_TODAY = [
    0.9968, 0.9242, 0.8918, 0.8600,
    0.8636, 0.8864, 0.8707, 0.8343,
    0.8617, 0.8569, 0.8551, 0.8456,
    0.8389, 0.8261, 0.8238, 0.8074,
    0.8254, 0.8038, 0.8014, 0.7901,
    0.8098, 0.7884, 0.7852, 0.7687,
    0.8292, 0.7762, 0.7428, 0.6620,
    0.7267, 0.6556, 0.6075, 0.5741,
    0.6154, 0.5753, 0.5903, 0.5570,
    0.3927, 0.1812, 0.0816, 0.0431,
    0.0479, 0.0164, -0.0001, -0.0008,
    0.0034, -0.0021, -0.0064, -0.0093,
    -0.0088, 0.0000, -0.0110, -0.0166,
    -0.0148, 0.0000, -0.0073, -0.0137,
    0.0000, -0.0143, -0.0151, 0.0000,
    -0.0000, -0.0000, -0.0019, 0.0572,
    0.0000, 0.1010, 0.3150, 0.4241,
    0.5276, 0.5914, 0.6648, 0.7494,
    0.6415, 0.6867, 0.7206, 0.8630,
    0.7345, 0.7700, 0.9393, 0.9814,
    0.8962, 0.9232, 0.8888, 0.8662,
    0.8894, 0.8773, 0.8643, 0.8533,
    0.8974, 0.8725, 0.8542, 0.8035,
    0.8669, 0.8466, 0.7940, 0.7627,
]
RCE_Q_TOMORROW = [
    0.7957, 0.7899, 0.7674, 0.7461,
    0.7603, 0.7445, 0.7309, 0.7151,
    0.7368, 0.7362, 0.7386, 0.7364,
    0.7581, 0.7858, 0.7830, 0.7754,
    0.7696, 0.7482, 0.7444, 0.7373,
    0.7136, 0.7177, 0.7011, 0.6825,
    0.6996, 0.6729, 0.6535, 0.6147,
    0.6355, 0.6103, 0.5623, 0.5090,
    0.5450, 0.3930, 0.1968, 0.1009,
    0.0967, -0.0125, -0.0708, -0.0217,
    -0.0094, -0.0081, 0.0000, 0.0000,
    0.0000, 0.0000, -0.0000, -0.0000,
    -0.0001, -0.0027, -0.0020, -0.0034,
    -0.0021, -0.0021, -0.0022, -0.0030,
    -0.0017, -0.0007, -0.0007, -0.0007,
    -0.0029, -0.0008, -0.0008, 0.0216,
    0.0003, 0.0022, 0.0677, 0.3223,
    0.4649, 0.5569, 0.7939, 0.7828,
    0.6550, 0.6845, 0.7625, 0.7992,
    0.7247, 0.7713, 0.8182, 0.8416,
    0.8060, 0.8396, 0.8382, 0.8399,
    0.8271, 0.8188, 0.8068, 0.8193,
    0.8426, 0.8190, 0.8197, 0.7920,
    0.7925, 0.7608, 0.7548, 0.7237,
]

# EA history row at 16:00 on 2026-07-25 (SOC while Dis window still ahead).
SOC_AT_16_PCT = 53.1
FROM_HOUR = 16

_DIS_CAP_RE = re.compile(
    r"^Dis\s+\d{1,2}:\d{2}-\d{1,2}:\d{2}\s+[\d.]+kW\s+cap(?P<cap>\d+)%"
)


def _cfg() -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 2.0},
        "grid": {},
    }
    merge_grid_defaults(cfg)
    return merge_simulation_defaults(cfg)


def _split_hourly_to_q15(hourly: list[float]) -> list[float]:
    out: list[float] = []
    for v in hourly:
        out.extend([float(v) / 4.0] * 4)
    return out


def _idle_forward_soc(
    *,
    soc_kwh: float,
    pv_hourly: list[float],
    load_hourly: list[float],
    from_hour: int,
    until_hour_exclusive: int,
    cfg: dict,
) -> list[tuple[int, float, float]]:
    """House-on-battery idle walk (no timer Dis/Chg).

    Returns (hour, end_soc_pct, grid_import_kwh).
    """
    params = get_simulation_params(cfg)
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    discharge_dc = plan_timer_discharge_power_kw(cfg)
    inverter_ac = float(cfg["inverter"]["ac_capacity_kw"])
    eta_grid = float(params["eta_grid_battery"])
    eta_out = float(params["eta_battery_out"])
    eta_pv_load = float(params["eta_pv_load"])
    eta_pv_grid = float(params["eta_pv_grid"])
    eta_pv_battery = float(params["eta_pv_battery"])
    eps = float(params["epsilon_kwh"]) * 0.25
    step_ac = inverter_ac * 0.25
    step_dc = discharge_dc * 0.25
    soc = float(soc_kwh)
    out: list[tuple[int, float, float]] = []
    for h in range(from_hour, until_hour_exclusive):
        pv_q = float(pv_hourly[h]) / 4.0
        load_q = float(load_hourly[h]) / 4.0
        imported = 0.0
        for _ in range(4):
            phys = simulate_hour(
                soc, pv_q, load_q, HourControl(0.0, 0.0),
                battery_cap=battery_cap,
                min_kwh=min_kwh,
                ac_cap_kw=step_ac,
                discharge_dc_cap_kwh=step_dc,
                eta_grid=eta_grid,
                eta_out=eta_out,
                eta_pv_load=eta_pv_load,
                eta_pv_grid=eta_pv_grid,
                eta_pv_battery=eta_pv_battery,
                epsilon=eps,
                reserve_soc_kwh=min_kwh,
            )
            imported += float(phys.grid_import)
            soc = phys.soc_end
        pct = 100.0 * soc / battery_cap if battery_cap else 0.0
        out.append((h, pct, imported))
    return out


def test_jul25_from_16_dis_cap_is_survive_reserve_not_min_soc():
    """Dis window planned at 16:00 must stop at overnight reserve %, not min 16%."""
    cfg = _cfg()
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc = int(plan_min_soc_pct(cfg))
    soc0 = SOC_AT_16_PCT / 100.0 * battery_cap

    plan = run_day_smart_q15_plan(
        date_str=DATE,
        pv_hourly=PV_TODAY,
        load_hourly=LOAD_TODAY,
        tomorrow_pv=PV_TOMORROW,
        tomorrow_load=LOAD_TOMORROW,
        cfg=cfg,
        rce_quarters=list(RCE_Q_TODAY),
        initial_soc_kwh=soc0,
        from_hour=FROM_HOUR,
    )
    assert plan is not None

    timers = timer_schedule_by_hour(plan["q15_by_hour"], cfg, plan["epsilon"])
    dis_hours = sorted(h for h, txt in timers.items() if txt.startswith("Dis"))
    assert dis_hours, "expected an evening Dis window from 16:00 replay"

    caps: list[int] = []
    for h in dis_hours:
        m = _DIS_CAP_RE.match(timers[h])
        assert m, f"unparseable Dis timer hour {h}: {timers[h]!r}"
        cap = int(m.group("cap"))
        caps.append(cap)
        assert cap > min_soc, (
            f"hour {h}: {timers[h]!r} still uses min_soc floor — "
            f"survive reserve must raise Dis capacity_pct"
        )

    schedule = plan.get("timer_schedule") or {}
    for slot in schedule.get("discharge_slots") or []:
        if float(slot.get("power_kw") or 0) <= 0:
            continue
        if str(slot.get("from")) == "00:00" and str(slot.get("to")) == "00:00":
            continue
        cap = int(round(float(slot.get("capacity_pct") or 0)))
        assert cap > min_soc, f"SA discharge slot still at min: {slot!r}"

    last_dis = max(dis_hours)
    params = get_simulation_params(cfg)
    pv_q = _split_hourly_to_q15(PV_TODAY[FROM_HOUR:] + PV_TOMORROW)
    load_q = _split_hourly_to_q15(LOAD_TODAY[FROM_HOUR:] + LOAD_TOMORROW)
    from src.g12_pricing import get_buy_price
    from datetime import datetime

    buy: list[float] = []
    base = datetime.strptime(DATE, "%Y-%m-%d")
    for h in range(FROM_HOUR, 24):
        buy.extend([float(get_buy_price(base.replace(hour=h), cfg)[0])] * 4)
    base2 = datetime.strptime(DATE_NEXT, "%Y-%m-%d")
    for h in range(24):
        buy.extend([float(get_buy_price(base2.replace(hour=h), cfg)[0])] * 4)
    off = float(cfg["grid"]["g12"]["offpeak_price_pln_kwh"])
    end_floor = post_discharge_reserve_soc_kwh(
        last_dis,
        pv_q,
        load_q,
        plan_reserve_min_soc_kwh(cfg),
        float(params["eta_battery_out"]),
        float(params["eta_pv_load"]),
        float(params["epsilon_kwh"]) * 0.25,
        buy_series=buy,
        offpeak_buy=off,
        slots_per_hour=4,
        global_step_offset=FROM_HOUR * 4,
    )
    end_floor_pct = 100.0 * end_floor / battery_cap
    assert max(caps) >= end_floor_pct - 2.0, (
        f"Dis caps {caps} below post-Dis reserve {end_floor_pct:.1f}% "
        f"(last Dis hour {last_dis})"
    )


def test_jul25_from_16_overnight_soc_stays_above_min_until_morning_pv_cover():
    """After evening Dis, idle overnight must not buy from grid before ~07:00 PV cover."""
    cfg = _cfg()
    battery_cap = float(cfg["battery"]["capacity_kwh"])
    min_soc = float(plan_min_soc_pct(cfg))
    eta_pv_load = float(get_simulation_params(cfg)["eta_pv_load"])
    soc0 = SOC_AT_16_PCT / 100.0 * battery_cap

    plan = run_day_smart_q15_plan(
        date_str=DATE,
        pv_hourly=PV_TODAY,
        load_hourly=LOAD_TODAY,
        tomorrow_pv=PV_TOMORROW,
        tomorrow_load=LOAD_TOMORROW,
        cfg=cfg,
        rce_quarters=list(RCE_Q_TODAY),
        initial_soc_kwh=soc0,
        from_hour=FROM_HOUR,
    )
    assert plan is not None

    timers = timer_schedule_by_hour(plan["q15_by_hour"], cfg, plan["epsilon"])
    dis_hours = sorted(h for h, txt in timers.items() if txt.startswith("Dis"))
    assert dis_hours
    last_dis = max(dis_hours)

    last_slots = plan["q15_by_hour"].get(last_dis) or []
    assert last_slots
    soc_after_dis = float(last_slots[-1]["soc_end"])
    soc_after_dis_pct = 100.0 * soc_after_dis / battery_cap
    assert soc_after_dis_pct >= min_soc + 5.0, (
        f"after Dis hour {last_dis} SOC {soc_after_dis_pct:.1f}% — "
        f"survive reserve should leave well above min"
    )

    tonight = _idle_forward_soc(
        soc_kwh=soc_after_dis,
        pv_hourly=PV_TODAY,
        load_hourly=LOAD_TODAY,
        from_hour=last_dis + 1,
        until_hour_exclusive=24,
        cfg=cfg,
    )
    soc = soc_after_dis
    for h, pct, imported in tonight:
        assert imported <= 0.01, f"tonight hour {h:02d} grid import {imported:.3f} kWh"
        assert pct >= min_soc - 0.05, f"tonight hour {h:02d} SOC {pct:.1f}% below min"
    if tonight:
        soc = tonight[-1][1] / 100.0 * battery_cap

    cover_hour = None
    for h in range(0, 12):
        deficit, _ = pv_load_energy_split(
            PV_TOMORROW[h], LOAD_TOMORROW[h], eta_pv_load=eta_pv_load,
        )
        if deficit <= 0.01:
            cover_hour = h
            break
    assert cover_hour == 7, (
        f"fixture expects PV cover at 07:00 on 2026-07-26, got {cover_hour}"
    )

    # Through 06:00 inclusive: no grid buy; SOC may reach min only at the
    # last pre-cover hour (mathematical end of the survive budget).
    morning = _idle_forward_soc(
        soc_kwh=soc,
        pv_hourly=PV_TOMORROW,
        load_hourly=LOAD_TOMORROW,
        from_hour=0,
        until_hour_exclusive=cover_hour,
        cfg=cfg,
    )
    assert morning
    for h, pct, imported in morning:
        assert imported <= 0.01, (
            f"next day {h:02d}:00 bought {imported:.3f} kWh from grid before "
            f"PV cover at {cover_hour:02d}:00 — Dis window left too little"
        )
        assert pct >= min_soc - 0.05, f"next day {h:02d}:00 SOC {pct:.1f}% below min"
        if h < cover_hour - 1:
            assert pct >= min_soc + 0.15, (
                f"next day {h:02d}:00 SOC {pct:.1f}% scraped min too early "
                f"(cover at {cover_hour:02d}:00)"
            )

    # Cover hour: PV ≥ load, still no forced grid for the house deficit.
    at_cover = _idle_forward_soc(
        soc_kwh=morning[-1][1] / 100.0 * battery_cap,
        pv_hourly=PV_TOMORROW,
        load_hourly=LOAD_TOMORROW,
        from_hour=cover_hour,
        until_hour_exclusive=cover_hour + 1,
        cfg=cfg,
    )
    assert at_cover
    assert at_cover[0][2] <= 0.01
    deficit, _ = pv_load_energy_split(
        PV_TOMORROW[cover_hour], LOAD_TOMORROW[cover_hour], eta_pv_load=eta_pv_load,
    )
    assert deficit <= 0.01
