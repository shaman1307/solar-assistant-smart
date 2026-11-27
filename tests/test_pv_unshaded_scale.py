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


def test_unshaded_ignores_horizon_mask():
    day = "2026-11-15"
    base = fm.pv_q15_from_radiation(_CFG, _RADIATION, day)
    raw = fm.pv_q15_from_radiation(_CFG, _RADIATION, day, apply_horizon_mask=False)
    assert sum(raw) > sum(base)
    assert fm.unshaded_pv_q15(_CFG, day, _RADIATION) == raw
