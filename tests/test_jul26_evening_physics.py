"""Jul 26 2026 evening: SOC physics + discharge to morning min.

Locked trajectory (fact H16–H19, optimized Dis from H20 @ 56.1%):

  Day  H   PV    Load  Export  BatΔ   SOC%
  26  16  3.16  0.60   0       —     56.6  fact
  26  17  1.84  0.84   0       —     57.6  fact
  26  18  0.99  0.75   0       —     57.2  fact
  26  19  0.09  0.58   0       —     56.1  fact
  26  20  0     0.85   6.55   −8.00  39.4  Dis 20:00-21:00 8.0kW
  26  21  0     1.21   2.15   −3.64  31.9  Dis 21:00-21:30
  26  22  0     1.56   0      −1.69  28.3  bat→home
  26  23  0     0.92   0      −0.99  26.3
  27  00  0     1.00   0      −1.08  24.0
  27  01  0     0.84   0      −0.91  22.1
  27  02  0     0.62   0      −0.67  20.7
  27  03  0     0.56   0      −0.61  19.5
  27  04  0     0.53   0      −0.57  18.3
  27  05  0.04  0.49   0      −0.48  17.3
  27  06  0.09  0.49   0      −0.43  16.4
  27  07  0.38  0.55   0      −0.18  16.0
  27  08  1.00  0.55   0      +0.34  16.7  PV→bat
"""

from __future__ import annotations

from datetime import date, datetime

from src.grid_config import merge_grid_defaults
from src.g12_pricing import get_buy_price
from src.plan_optimizer import (
    HourControl,
    optimize_horizon,
    post_discharge_reserve_soc_kwh,
    simulate_hour,
)
from src.simulation_config import (
    get_simulation_params,
    merge_battery_defaults,
    merge_simulation_defaults,
    plan_min_soc_kwh,
    plan_timer_discharge_power_kw,
)
from src.timer_plan import infer_discharge_timer_power_kw

# Actual UI table 2026-07-26 (end-of-hour SOC). Start 16:00 = H15 end 52.6%.
SOC15_END_PCT = 52.6
# Fact meter hours (PV, load, end-SOC%). Oversold H20–H23 kept for reference only.
FACT = {
    16: (3.16, 0.60, 56.6),
    17: (1.84, 0.84, 57.6),
    18: (0.99, 0.75, 57.2),
    19: (0.09, 0.58, 56.1),
    20: (0.00, 0.85, 40.5),
    21: (0.00, 1.21, 30.3),
    22: (0.00, 1.56, 26.6),
    23: (0.00, 0.92, 25.0),
}
# Alias used by idle physics (H16–H19 only).
ACT = {h: FACT[h] for h in range(16, 20)}

# Morning 27.07 forecast (from live plan that evening).
PV_TOM = [0.0, 0.0, 0.0, 0.0, 0.0, 0.042, 0.089, 0.383, 1.005] + [2.0] * 15
LOAD_TOM = [1.0, 0.84, 0.618, 0.561, 0.527, 0.49, 0.486, 0.553, 0.545] + [0.5] * 15
# Approximate evening RCE (all above export floor ~0.62).
RCE_TODAY = (
    [0.10] * 16
    + [0.10, 0.65, 0.73, 0.79, 0.83, 0.82, 0.82, 0.76]
)

# Locked correct plan from H20 @ fact 56.1% (end-of-hour SOC%).
PLAN_SOC_TODAY = {
    20: 39.4,
    21: 31.9,
    22: 28.3,
    23: 26.3,
}
PLAN_EXPORT = {
    20: 6.55,
    21: 2.15,
}
PLAN_BAT_DELTA = {
    20: -8.00,
    21: -3.64,
    22: -1.69,
    23: -0.99,
}
# 27.07 overnight walk after the locked evening plan (end-of-hour SOC%).
PLAN_SOC_TOMORROW = {
    0: 24.0,
    1: 22.1,
    2: 20.7,
    3: 19.5,
    4: 18.3,
    5: 17.3,
    6: 16.4,
    7: 16.0,
    8: 16.7,
}
PLAN_BAT_DELTA_TOMORROW = {
    0: -1.08,
    1: -0.91,
    2: -0.67,
    3: -0.61,
    4: -0.57,
    5: -0.48,
    6: -0.43,
    7: -0.18,
    8: 0.34,
}

SOC_TOL_PP = 0.15
EXPORT_TOL_KWH = 0.08
BAT_TOL_KWH = 0.08


def _cfg() -> dict:
    cfg = {
        "inverter": {"ac_capacity_kw": 8.0},
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 16,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_battery": 25.0,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
            },
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.6229,
            "g12": {
                "tariff_name": "G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_energy_only_pln_kwh": 0.7182,
                "offpeak_energy_only_pln_kwh": 0.4678,
                "peak_hours_weekday": [[6, 13], [15, 22]],
            },
        },
        "timer_schedule": {
            "min_block_minutes": 30,
            "min_hourly_transfer_kwh": 2.0,
        },
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    return cfg


def _pv_load_today() -> tuple[list[float], list[float]]:
    pv = [0.0] * 24
    load = [0.2] * 24
    for h, (p, l, _) in FACT.items():
        pv[h] = p
        load[h] = l
    return pv, load


def _sim_kwargs(cfg: dict, params: dict) -> dict:
    return dict(
        battery_cap=float(cfg["battery"]["capacity_kwh"]),
        min_kwh=plan_min_soc_kwh(cfg),
        ac_cap_kw=float(cfg["inverter"]["ac_capacity_kw"]) * 0.25,
        discharge_dc_cap_kwh=plan_timer_discharge_power_kw(cfg) * 0.25,
        eta_grid=float(params["eta_grid_battery"]),
        eta_out=float(params["eta_battery_out"]),
        eta_pv_load=float(params["eta_pv_load"]),
        eta_pv_grid=float(params["eta_pv_grid"]),
        eta_pv_battery=float(params["eta_pv_battery"]),
        epsilon=float(params["epsilon_kwh"]) * 0.25,
        reserve_soc_kwh=plan_min_soc_kwh(cfg),
    )


def test_jul26_idle_soc_16_to_19_tracks_meter():
    """Physics walk H16–H19 end SOC within ±0.8 pp of the UI table."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    cap = float(cfg["battery"]["capacity_kwh"])
    kw = _sim_kwargs(cfg, params)
    soc = SOC15_END_PCT / 100.0 * cap
    idle = HourControl(0.0, 0.0)
    for h in range(16, 20):
        pv, load, fact = ACT[h]
        for _ in range(4):
            phys = simulate_hour(soc, pv / 4, load / 4, idle, **kw)
            soc = phys.soc_end
        pct = 100.0 * soc / cap
        tol = 0.8 if h <= 17 else 1.5
        assert abs(pct - fact) <= tol, f"H{h}: sim {pct:.1f}% vs fact {fact}% (tol {tol})"


def test_jul26_discharge_from_20_lands_near_morning_min():
    """From fact SOC @20:00, Dis H20+H21 and locked hour SOC through 08:00."""
    cfg = _cfg()
    params = get_simulation_params(cfg)
    cap = float(cfg["battery"]["capacity_kwh"])
    min_kwh = plan_min_soc_kwh(cfg)
    eta_out = float(params["eta_battery_out"])
    eta_pv = float(params["eta_pv_load"])
    eps = float(params["epsilon_kwh"])
    dac = plan_timer_discharge_power_kw(cfg)
    kw = _sim_kwargs(cfg, params)

    assert dac == 8.0

    pv_t, load_t = _pv_load_today()
    from_hour = 20
    soc0 = FACT[19][2] / 100.0 * cap
    steps = (24 - from_hour) * 4
    offset = from_hour * 4
    pv_s, load_s, buy_s = [], [], []
    for h in range(from_hour, 24):
        for q in range(4):
            pv_s.append(pv_t[h] / 4)
            load_s.append(load_t[h] / 4)
            buy_s.append(float(get_buy_price(datetime(2026, 7, 26, h, q * 15), cfg)[0]))

    rce: list[float | None] = [None] * 192
    for h, p in enumerate(RCE_TODAY):
        for q in range(4):
            rce[h * 4 + q] = float(p)

    controls = optimize_horizon(
        steps=steps,
        pv_series=pv_s,
        load_series=load_s,
        buy_prices=buy_s,
        rce_series=rce,
        initial_soc_kwh=soc0,
        cfg=cfg,
        params=params,
        end_dt=datetime(2026, 7, 26, 23, 0),
        today_date=date(2026, 7, 26),
        rce_map={},
        forecast={
            "today": {"pv": pv_t, "load": load_t},
            "tomorrow": {"pv": PV_TOM, "load": LOAD_TOM},
        },
        step_scale=0.25,
        rce_step_offset=offset,
    )

    for h, exp_need in PLAN_EXPORT.items():
        i0 = (h - from_hour) * 4
        got = sum(c.battery_export_kwh for c in controls[i0:i0 + 4])
        assert abs(got - exp_need) <= EXPORT_TOL_KWH, (
            f"H{h} export {got:.2f} vs locked {exp_need:.2f}"
        )

    # Timer Dis power is DC (max 8.0), never the old AC-derated 7.4 label.
    active = [q for q in range(4) if controls[q].battery_export_kwh > 0.05]
    assert active
    duration_min = (active[-1] - active[0] + 1) * 15
    export_k = sum(controls[q].battery_export_kwh for q in active)
    load_k = sum(load_s[q] for q in active)
    pwr = infer_discharge_timer_power_kw(
        export_kwh=export_k,
        duration_min=duration_min,
        cfg=cfg,
        load_kwh=load_k,
        pv_kwh=0.0,
    )
    assert abs(pwr - 7.4) > 0.05
    assert 0.5 <= pwr <= 8.0

    ext_pv = [pv_t[h] / 4 for h in range(24) for _ in range(4)] + [
        PV_TOM[h] / 4 for h in range(24) for _ in range(4)
    ]
    ext_load = [load_t[h] / 4 for h in range(24) for _ in range(4)] + [
        LOAD_TOM[h] / 4 for h in range(24) for _ in range(4)
    ]
    f21 = post_discharge_reserve_soc_kwh(
        21, ext_pv, ext_load, min_kwh, eta_out, eta_pv, eps,
        slots_per_hour=4, global_step_offset=0,
    )

    soc = soc0
    last_export_hour = 19
    soc_after_last_dis = soc0
    for h in range(20, 24):
        bat_h = 0.0
        for q in range(4):
            i = (h - from_hour) * 4 + q
            phys = simulate_hour(soc, pv_s[i], load_s[i], controls[i], **kw)
            if controls[i].battery_export_kwh > 0.05:
                last_export_hour = h
            bat_h += phys.battery_delta
            soc = phys.soc_end
        pct = 100.0 * soc / cap
        assert abs(pct - PLAN_SOC_TODAY[h]) <= SOC_TOL_PP, (
            f"H{h} SOC {pct:.1f}% vs locked {PLAN_SOC_TODAY[h]:.1f}%"
        )
        assert abs(bat_h - PLAN_BAT_DELTA[h]) <= BAT_TOL_KWH, (
            f"H{h} batΔ {bat_h:.2f} vs locked {PLAN_BAT_DELTA[h]:.2f}"
        )
        if h == last_export_hour:
            soc_after_last_dis = soc

    assert last_export_hour >= 21
    assert abs(soc_after_last_dis - f21) <= 0.8, (
        f"after last Dis SOC {soc_after_last_dis:.2f} vs post_dis(21) {f21:.2f}"
    )

    idle = HourControl(0.0, 0.0)
    import_sum = 0.0
    for h in range(0, 9):
        bat_h = 0.0
        for _ in range(4):
            phys = simulate_hour(
                soc, PV_TOM[h] / 4, LOAD_TOM[h] / 4, idle, **kw,
            )
            import_sum += phys.grid_import
            bat_h += phys.battery_delta
            soc = phys.soc_end
        pct = 100.0 * soc / cap
        assert abs(pct - PLAN_SOC_TOMORROW[h]) <= SOC_TOL_PP, (
            f"27 H{h:02d} SOC {pct:.1f}% vs locked {PLAN_SOC_TOMORROW[h]:.1f}%"
        )
        assert abs(bat_h - PLAN_BAT_DELTA_TOMORROW[h]) <= BAT_TOL_KWH, (
            f"27 H{h:02d} batΔ {bat_h:.2f} vs locked {PLAN_BAT_DELTA_TOMORROW[h]:.2f}"
        )

    assert import_sum < 0.05
    # Locked landing: min at H07, slight PV→bat lift at H08.
    assert PLAN_SOC_TOMORROW[7] == 16.0
    assert PLAN_SOC_TOMORROW[8] == 16.7
