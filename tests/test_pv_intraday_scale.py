from src.forecast import PV_SCALE_MAX, pv_scale_factors


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
