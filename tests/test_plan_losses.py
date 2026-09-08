"""Transfer loss physics in plan simulation."""

import pytest

from src.plan_optimizer import HourControl, simulate_hour
from src.plan_spill import pv_load_energy_split
from src.simulation_config import (
    PLAN_HORIZON_HOURS,
    RESERVE_MIN_SOC_MARGIN,
    get_simulation_params,
    plan_min_soc_pct,
    plan_reserve_min_soc_pct,
)


def _cfg(losses: dict | None = None) -> dict:
    base_losses = {
        "grid_to_battery": 7.5,
        "battery_to_load_or_grid": 7.5,
        "pv_to_battery": 7.5,
        "pv_to_grid": 7.5,
        "pv_to_load": 7.5,
    }
    if losses:
        base_losses.update(losses)
    return {
        "battery": {"capacity_kwh": 43.0},
        "simulation": {"min_soc_pct": 15, "epsilon_kwh": 0.05, "losses_pct": base_losses},
    }


def _etas(params: dict, **overrides: float) -> dict[str, float]:
    out = {
        "eta_grid": float(params["eta_grid_battery"]),
        "eta_out": float(params["eta_battery_out"]),
        "eta_pv_load": float(params["eta_pv_load"]),
        "eta_pv_grid": float(params["eta_pv_grid"]),
        "eta_pv_battery": float(params["eta_pv_battery"]),
    }
    out.update(overrides)
    return out


def test_pv_load_split_is_ac_meter_arithmetic():
    """PV/load series are AC meters — no second eta_pv_load conversion."""
    deficit, surplus = pv_load_energy_split(2.0, 2.0, eta_pv_load=0.925)
    assert deficit == 0.0
    assert surplus == 0.0


def test_pv_load_split_exact_cover():
    deficit, surplus = pv_load_energy_split(2.0, 2.0, eta_pv_load=0.925)
    assert deficit == pytest.approx(0.0, abs=1e-9)
    assert surplus == pytest.approx(0.0, abs=1e-9)


def test_pv_load_split_surplus_and_deficit():
    deficit, surplus = pv_load_energy_split(3.0, 1.0, eta_pv_load=0.925)
    assert deficit == 0.0
    assert surplus == pytest.approx(2.0)
    deficit, surplus = pv_load_energy_split(0.5, 2.0, eta_pv_load=0.925)
    assert deficit == pytest.approx(1.5)
    assert surplus == 0.0


def test_simulate_hour_pv_to_load_balanced_when_equal():
    """Equal AC PV and load: no battery withdraw regardless of eta_pv_load."""
    params = get_simulation_params(_cfg())
    min_kwh = 43.0 * 0.15

    without_loss = simulate_hour(
        20.0, 2.0, 2.0, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        epsilon=0.05,
        **_etas(params, eta_pv_load=1.0),
    )
    with_loss = simulate_hour(
        20.0, 2.0, 2.0, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        epsilon=0.05,
        **_etas(params),
    )
    assert without_loss.grid_import == 0.0
    assert with_loss.grid_import == 0.0
    assert without_loss.battery_delta == pytest.approx(0.0, abs=1e-9)
    assert with_loss.battery_delta == pytest.approx(0.0, abs=1e-9)


def test_simulate_hour_pv_to_battery_applies_loss():
    """PV surplus into SOC is reduced by eta_pv_battery (second RT leg)."""
    params = get_simulation_params(_cfg())
    min_kwh = 43.0 * 0.15
    eta_bat = float(params["eta_pv_battery"])
    soc0 = 20.0
    pv_surplus = 10.0  # load=0, eta_pv_load irrelevant for surplus amount

    phys = simulate_hour(
        soc0, pv_surplus, 0.0, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        epsilon=0.05,
        **_etas(params),
    )
    assert phys.battery_delta == pytest.approx(pv_surplus * eta_bat, abs=1e-6)
    assert phys.soc_end == pytest.approx(soc0 + pv_surplus * eta_bat, abs=1e-6)
    # leftover surplus after charge exports via eta_pv_grid; with headroom for 10*0.925,
    # all PV is taken into battery — no export
    assert phys.grid_export == pytest.approx(0.0, abs=1e-6)

    # Round-trip PV→bat→load: both legs at 7.5% → ~14.4% loss
    stored = phys.battery_delta
    load_ac = stored * float(params["eta_battery_out"])
    phys2 = simulate_hour(
        phys.soc_end, 0.0, load_ac, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        epsilon=0.05,
        **_etas(params),
    )
    assert phys2.grid_import == pytest.approx(0.0, abs=1e-3)
    assert phys2.soc_end == pytest.approx(soc0, abs=1e-3)
    assert load_ac / pv_surplus == pytest.approx(
        eta_bat * float(params["eta_battery_out"]), abs=1e-6,
    )


def test_simulate_hour_ev_as_ac_load_no_double_loss():
    """EV kWh are AC load; battery covers deficit once via eta_out."""
    params = get_simulation_params(_cfg())
    min_kwh = 43.0 * 0.15
    total_load = 1.0 + 5.0

    phys = simulate_hour(
        30.0, 0.0, total_load, HourControl(0.0, 0.0),
        battery_cap=43.0, min_kwh=min_kwh, ac_cap_kw=8.0,
        epsilon=0.05,
        **_etas(params),
    )
    expected_withdraw = total_load / float(params["eta_battery_out"])
    assert round(-phys.battery_delta, 3) == round(expected_withdraw, 3)
    assert phys.grid_import == 0.0


def test_get_simulation_params_includes_pv_to_battery_and_fixed_horizon():
    params = get_simulation_params(_cfg())
    assert params["eta_pv_load"] == 0.925
    assert params["eta_pv_battery"] == 0.925
    assert params["horizon_hours"] == PLAN_HORIZON_HOURS == 24
    # Stale config horizon must be ignored
    stale = _cfg()
    stale["simulation"]["horizon_hours"] = 30
    assert get_simulation_params(stale)["horizon_hours"] == 24


def test_reserve_min_soc_margin():
    cfg = _cfg()
    assert plan_min_soc_pct(cfg) == 15.0
    assert plan_reserve_min_soc_pct(cfg) == 15.0 * RESERVE_MIN_SOC_MARGIN


def test_loss_defaults_single_source():
    """Code defaults, install template, and Configuration form share 7.5% losses."""
    from pathlib import Path

    from src.simulation_config import DEFAULT_SIMULATION, simulation_form_defaults

    losses = DEFAULT_SIMULATION["losses_pct"]
    assert losses == {
        "grid_to_battery": 7.5,
        "battery_to_load_or_grid": 7.5,
        "pv_to_battery": 7.5,
        "pv_to_grid": 7.5,
        "pv_to_load": 7.5,
    }
    form = simulation_form_defaults()
    for key, pct in losses.items():
        assert form[f"simulation.losses_pct.{key}"] == pct

    root = Path(__file__).resolve().parents[1]
    yaml_text = (root / "config-templates.yaml").read_text(encoding="utf-8")
    for key in losses:
        assert f"{key}: 7.5" in yaml_text
    html = (root / "src" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "simulation_form_defaults" in html
    assert "losses_pct.pv_to_battery'] }}" in html or "simulation_form_defaults | tojson" in html
    assert "pv_to_battery': 25" not in html
    assert 'value="25"' not in html.split("Energy arbitrage model")[1].split("Timer Schedule")[0]

