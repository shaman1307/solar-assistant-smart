"""Horizon masks cut beam; the chart series without masks stays higher."""

from src.forecast import pv_q15_from_radiation
from src.pv_horizon import MASK_BINS


def _cfg() -> dict:
    return {
        "location": {"latitude": 54.5, "longitude": 18.5},
        "solar": {
            "azimuth": 180,
            "system_loss_factor": 1.0,
            "blocks": [{
                "power_kwp": 5.0,
                "tilt": 25,
                "horizon_mask": [90.0] * MASK_BINS,
            }],
        },
    }


def test_full_mask_cuts_beam_but_unshaded_keeps_it():
    radiation = {
        "time": ["2026-11-10T12:00"],
        "direct": [400.0],
        "diffuse": [80.0],
    }
    shaded = pv_q15_from_radiation(_cfg(), radiation, "2026-11-10")
    open_sky = pv_q15_from_radiation(
        _cfg(), radiation, "2026-11-10", apply_horizon_mask=False,
    )
    assert sum(open_sky) > sum(shaded) + 0.05
