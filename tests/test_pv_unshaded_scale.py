from src import forecast as fm

_CFG = {
    "location": {"latitude": 54.5, "longitude": 18.5},
    "solar": {
        "azimuth": 187,
        "system_loss_factor": 0.93,
        "blocks": [{
            "power_kwp": 5.0, "tilt": 25, "mppt": 1,
            "horizon_mask": [90.0] * 72,
        }],
    },
}
_RADIATION = {
    "time": [f"2026-11-15T{h:02d}:{m:02d}" for h in range(8, 16) for m in (0, 15, 30, 45)],
    "direct": [300.0] * 32,
    "diffuse": [60.0] * 32,
}


def test_unshaded_ignores_mask_and_scales_only_by_given_effective():
    day = "2026-11-15"
    base = fm.pv_q15_from_radiation(_CFG, _RADIATION, day)
    raw = fm.pv_q15_from_radiation(_CFG, _RADIATION, day, apply_horizon_mask=False)
    assert sum(raw) > sum(base)
    plain = fm.unshaded_pv_q15(_CFG, day, _RADIATION, shaded_effective=base, shaded_base=base)
    assert plain == raw
    slider = [v * 0.5 for v in base]
    halved = fm.unshaded_pv_q15(_CFG, day, _RADIATION, shaded_effective=slider, shaded_base=base)
    assert abs(sum(halved) - sum(raw) * 0.5) < 1e-3


def test_unshaded_freezes_on_first_refresh_and_follows_slider_only():
    from src import forecast_cache as fc

    day = "2026-11-15"
    base = fm.pv_q15_from_radiation(_CFG, _RADIATION, day)
    metric: dict = {}
    fc._set_metric_effective(metric, fc.q15_to_hourly(base), base)
    metric["radiation"] = _RADIATION
    fc._freeze_unshaded_pv(_CFG, day, metric, None)
    frozen = list(metric["unshaded_q15"])
    assert frozen == fm.pv_q15_from_radiation(_CFG, _RADIATION, day, apply_horizon_mask=False)

    cloudy = dict(_RADIATION, direct=[30.0] * 32, diffuse=[20.0] * 32)
    metric["radiation"] = cloudy
    fresh = fm.pv_q15_from_radiation(_CFG, cloudy, day)
    fc._set_metric_effective(metric, fc.q15_to_hourly(fresh), fresh)
    fc._freeze_unshaded_pv(_CFG, day, metric, None)
    assert metric["unshaded_q15"] == frozen

    half = round(sum(fc.q15_to_hourly(base)), 2) * 0.5
    fc._freeze_unshaded_pv(_CFG, day, metric, half)
    assert abs(sum(metric["unshaded_q15"]) - sum(frozen) * 0.5) < 1e-2

    bigger = {"solar": dict(_CFG["solar"], blocks=[dict(_CFG["solar"]["blocks"][0], power_kwp=10.0)])}
    cfg2 = dict(_CFG, **bigger)
    fc._freeze_unshaded_pv(cfg2, day, metric, None)
    assert abs(sum(metric["unshaded_q15"]) - sum(frozen) * 2.0) < 1e-2
