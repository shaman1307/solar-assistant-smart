"""Tail start hour — no double-count with last DP step."""
from datetime import datetime

from src.plan_optimizer import tail_start_hour


def test_tail_empty_when_plan_ends_2345():
    end = datetime(2026, 6, 25, 23, 45)
    # from_hour=0 → steps=96, offset=0 → last global 95 → hour 23 → tail from 24
    start = tail_start_hour(
        steps=96, rce_step_offset=0, step_scale=0.25, end_dt=end,
    )
    assert start == 24


def test_tail_from_hour_after_partial_day():
    end = datetime(2026, 6, 25, 23, 45)
    # from_hour=14 → 40 steps, offset=56 → last global 95 → hour 23
    start = tail_start_hour(
        steps=40, rce_step_offset=56, step_scale=0.25, end_dt=end,
    )
    assert start == 24


def test_tail_legacy_hourly_when_no_steps():
    end = datetime(2026, 6, 25, 22, 0)
    start = tail_start_hour(steps=0, rce_step_offset=0, step_scale=1.0, end_dt=end)
    assert start == 22
