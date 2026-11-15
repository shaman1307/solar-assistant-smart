from src import forecast as fm
from src.forecast import PV_SCALE_MAX, intraday_scaled_pv_q15, pv_scale_factors

_RADIATION = {"time": ["2026-11-15T12:00"], "direct": [100.0], "diffuse": [50.0]}


def _cfg(*flags: bool) -> dict:
    blocks = [
        {"power_kwp": 1.5, "tilt": 35, "mppt": 2, "scale_reference": flags[0]},
        {"power_kwp": 5.0, "tilt": 25, "mppt": 1, "scale_reference": flags[1]},
    ]
    return {"solar": {"blocks": blocks}}


def test_reference_mppt_follows_flagged_block(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(fm, "pv_q15_from_radiation", lambda cfg, *a, **k: [0.25] * 96)
    monkeypatch.setattr(fm.pvh, "mppt_actual_kw", lambda mppt, d: seen.append(mppt) or [])
    monkeypatch.setattr(fm.fc, "ten_min_kw_to_q15_kw", lambda s: [0.5] * 96)
    q15 = [0.1] * 96
    scaled, k = intraday_scaled_pv_q15(_cfg(True, False), _RADIATION, "2026-11-15", q15, 60)
    assert seen == [2]
    assert k == 0.5
    assert scaled[70] == 0.05
    scaled, k = intraday_scaled_pv_q15(_cfg(False, False), _RADIATION, "2026-11-15", q15, 60)
    assert (scaled, k) == (q15, 1.0)
    assert seen == [2]


def _series(start: int, end: int, value: float) -> list[float]:
    return [value if start <= i < end else 0.0 for i in range(96)]


def test_scale_uses_window_before_slot_and_holds_after_until_slot():
    ref = _series(32, 64, 1.0)
    actual = [v * 0.8 for v in ref]
    factors = pv_scale_factors(ref, actual, until_slot=44)
    assert factors[33] == 1.0
    assert factors[40] == 0.8
    assert factors[44] == 0.8
    assert factors[80] == 0.8


def test_scale_ignores_missing_and_weak_slots():
    ref = _series(32, 64, 1.0)
    actual: list[float | None] = [None] * 96
    for i in range(36, 40):
        actual[i] = 3.0
    ref[36] = 0.1
    factors = pv_scale_factors(ref, actual, until_slot=96)
    assert factors[40] == 1.0
    actual[35] = 3.0
    assert pv_scale_factors(ref, actual, until_slot=96)[40] == PV_SCALE_MAX
