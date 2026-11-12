"""Forecast chart: SOC plan (simulator) vs actual (Energy arbitrage rows)."""

from datetime import timedelta
from unittest.mock import patch

from src.influxdb import now_warsaw
from src.plan_simulation import (
    extract_actual_soc_q15,
    extract_plan_soc_hourly,
    normalize_plan_soc_q15,
)


def test_actual_soc_q15_clips_future_plan_rows():
    now = now_warsaw().replace(hour=14, minute=20, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    plan = {
        "plan_soc_q15": {
            "today": [40.0] * 96,
            "tomorrow": [40.0] * 96,
        },
        "history_rows": [
            {
                "plan_date": today,
                "hour": 10,
                "soc": 62.0,
                "q15": [
                    {"quarter": q, "soc": 60.0 + q * 0.5}
                    for q in range(4)
                ],
            },
        ],
        "rows": [
            {
                "plan_date": today,
                "hour": 14,
                "soc": 71.5,
                "q15": [
                    {"quarter": q, "soc": 70.0 + q * 0.5}
                    for q in range(4)
                ],
            },
            {
                "plan_date": today,
                "hour": 18,
                "soc": 80.0,
                "q15": [
                    {"quarter": q, "soc": 80.0}
                    for q in range(4)
                ],
            },
            {"plan_date": tomorrow, "hour": 3, "soc": 55.0},
        ],
    }
    hourly = extract_plan_soc_hourly(plan)
    assert hourly["today"][10] == 62.0
    assert hourly["today"][14] == 71.5
    assert hourly["tomorrow"][3] == 55.0

    with patch("src.plan_simulation.now_warsaw", return_value=now):
        actual = extract_actual_soc_q15(plan)

    assert actual["today"][10 * 4] == 60.0
    assert actual["today"][10 * 4 + 3] == 61.5
    # Current hour 14, minute 20 → quarter 1 is last included (14:15).
    assert actual["today"][14 * 4] == 70.0
    assert actual["today"][14 * 4 + 1] == 70.5
    assert actual["today"][14 * 4 + 2] is None
    assert actual["today"][14 * 4 + 3] is None
    # Future hour projections must not appear as actual.
    assert actual["today"][18 * 4] is None
    assert all(v is None for v in actual["tomorrow"])
    # Plan line stays the simulator series — not overwritten by EA.
    assert plan["plan_soc_q15"]["today"][10 * 4] == 40.0


def test_normalize_plan_soc_q15_pads_without_stitching():
    """Simulator plan is kept as-is; missing slots stay None (no EA fill)."""
    plan = {
        "plan_soc_q15": {
            "today": [20.0] * 40 + [None] * 10 + [45.0] * 46,
            "tomorrow": [30.0] * 10,
        },
    }
    out = normalize_plan_soc_q15(plan)
    assert out["today"][0] == 20.0
    assert out["today"][39] == 20.0
    assert out["today"][40] is None
    assert out["today"][50] == 45.0
    assert len(out["today"]) == 96
    assert out["tomorrow"][0] == 30.0
    assert out["tomorrow"][10] is None
    assert len(out["tomorrow"]) == 96


def test_normalize_plan_soc_q15_empty():
    out = normalize_plan_soc_q15(None)
    assert out["today"] == [None] * 96
    assert out["tomorrow"] == [None] * 96


def test_compose_plan_soc_freezes_entire_locked_day():
    from src.plan_simulation import compose_plan_soc_q15

    existing = {
        "today_date": "2026-07-20",
        "plan_soc_day_locked": True,
        "plan_soc_q15": {
            "today": [10.0 + i * 0.1 for i in range(96)],
            "tomorrow": [20.0] * 96,
        },
    }
    fresh = {
        "today_date": "2026-07-20",
        "plan_soc_q15": {
            "today": [99.0] * 96,
            "tomorrow": [30.0] * 96,
        },
    }
    out = compose_plan_soc_q15(existing, fresh)
    assert out["today"][0] == 10.0
    assert out["today"][40] == round(10.0 + 40 * 0.1, 1)
    assert out["today"][95] == round(10.0 + 95 * 0.1, 1)
    assert out["tomorrow"][0] == 30.0  # tomorrow still follows fresh


def test_compose_plan_soc_refresh_on_overrides():
    """Forecast Overrides must replace the frozen solid SOC plan curve."""
    from src.plan_simulation import compose_plan_soc_q15

    existing = {
        "today_date": "2026-07-20",
        "plan_soc_day_locked": True,
        "plan_soc_q15": {
            "today": [10.0] * 96,
            "tomorrow": [20.0] * 96,
        },
    }
    fresh = {
        "today_date": "2026-07-20",
        "plan_soc_q15": {
            "today": [55.0] * 96,
            "tomorrow": [33.0] * 96,
        },
    }
    out = compose_plan_soc_q15(existing, fresh, refresh=True)
    assert out["today"][0] == 55.0
    assert out["today"][95] == 55.0
    assert out["tomorrow"][0] == 33.0


def test_hourly_plan_refresh_unlock_plan_soc_passes_refresh_true():
    """Config/overrides path must unlock solid SOC via compose(..., refresh=True)."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from src import plan_simulation as ps

    calls: list[bool] = []

    def _capture_compose(existing, fresh, *, refresh=False):
        calls.append(bool(refresh))
        return {"today": [1.0] * 96, "tomorrow": [2.0] * 96}

    with (
        patch.object(ps, "_run_fresh_simulation", new_callable=AsyncMock) as run,
        patch.object(ps, "_wrap_sim_result", return_value={
            "today_date": "2026-07-21",
            "plan_from_hour": 4,
            "rows": [],
            "history_rows": [],
            "delta_kwh": 0.0,
            "plan_export_hours": [],
            "computed_at": "x",
            "plan_soc_q15": {"today": [9.0] * 96, "tomorrow": [8.0] * 96},
        }),
        patch.object(ps, "read_plan", return_value={
            "today_date": "2026-07-21",
            "plan_from_hour": 4,
            "plan_soc_day_locked": True,
            "plan_soc_q15": {"today": [10.0] * 96, "tomorrow": [20.0] * 96},
            "rows": [],
            "history_rows": [],
        }),
        patch.object(ps, "plan_needs_full_rebuild", return_value=True),
        patch.object(ps, "compose_plan_soc_q15", side_effect=_capture_compose),
        patch.object(ps, "extract_actual_soc_q15", return_value={"today": [None] * 96}),
        patch.object(ps, "write_plan"),
        patch.object(ps, "build_hourly_schedule", return_value={}),
        patch.object(ps, "apply_locked_hour_labels_from_plan"),
        patch.object(ps, "attach_immutable_history"),
        patch.object(ps, "now_warsaw") as now_mock,
    ):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_mock.return_value = datetime(2026, 7, 21, 4, 20, tzinfo=ZoneInfo("Europe/Warsaw"))
        run.return_value = ({}, {}, {}, {}, {})
        asyncio.run(ps.hourly_plan_refresh({"battery": {"capacity_kwh": 48}}, unlock_plan_soc=True))
        asyncio.run(ps.hourly_plan_refresh({"battery": {"capacity_kwh": 48}}, unlock_plan_soc=False))

    assert calls == [True, False]


def test_compose_plan_soc_refresh_false_keeps_lock():
    from src.plan_simulation import compose_plan_soc_q15

    existing = {
        "today_date": "2026-07-20",
        "plan_soc_day_locked": True,
        "plan_soc_q15": {"today": [10.0] * 96, "tomorrow": [20.0] * 96},
    }
    fresh = {
        "today_date": "2026-07-20",
        "plan_soc_q15": {"today": [55.0] * 96, "tomorrow": [33.0] * 96},
    }
    out = compose_plan_soc_q15(existing, fresh, refresh=False)
    assert out["today"][0] == 10.0
    assert out["tomorrow"][0] == 33.0


def test_compose_plan_soc_seeds_when_unlocked():
    from src.plan_simulation import compose_plan_soc_q15

    existing = {
        "today_date": "2026-07-20",
        "plan_soc_day_locked": False,
        "plan_soc_q15": {
            "today": [1.0] * 96,
            "tomorrow": [None] * 96,
        },
    }
    fresh = {
        "today_date": "2026-07-20",
        "plan_soc_q15": {
            "today": [40.0] * 96,
            "tomorrow": [30.0] * 96,
        },
    }
    out = compose_plan_soc_q15(existing, fresh)
    assert out["today"][0] == 40.0
    assert out["today"][95] == 40.0
    assert out["tomorrow"][0] == 30.0


def test_actual_soc_independent_of_plan_series():
    """Dashed EA actual is separate from solid simulator plan_soc."""
    now = now_warsaw().replace(hour=5, minute=10, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    plan = {
        "plan_soc_q15": {
            "today": [40.0] * 96,
            "tomorrow": [None] * 96,
        },
        "history_rows": [
            {
                "plan_date": today,
                "hour": 2,
                "soc": 17.0,
                "q15": [{"quarter": q, "soc": 17.0} for q in range(4)],
            },
            {
                "plan_date": today,
                "hour": 4,
                "soc": 16.0,
                "q15": [{"quarter": q, "soc": 16.0} for q in range(4)],
            },
        ],
        "rows": [],
    }
    with patch("src.plan_simulation.now_warsaw", return_value=now):
        actual = extract_actual_soc_q15(plan)
    assert actual["today"][2 * 4] == 17.0
    assert actual["today"][4 * 4] == 16.0
    assert plan["plan_soc_q15"]["today"][2 * 4] == 40.0
    assert plan["plan_soc_q15"]["today"][5 * 4] == 40.0


def test_plan_row_soc_keeps_sale_on_the_planned_hour():
    """Hour-7 discharge on the plan row drops the solid line at hour 7."""
    from src.plan_orchestrator import overlay_plan_row_soc_q15

    slots: list[float | None] = [None] * 96
    rows = [
        {
            "plan_date": "2026-11-12",
            "hour": 6,
            "q15": [{"quarter": q, "soc": 88.0} for q in range(4)],
        },
        {
            "plan_date": "2026-11-12",
            "hour": 7,
            "q15": [
                {"quarter": 0, "soc": 84.0},
                {"quarter": 1, "soc": 80.0},
                {"quarter": 2, "soc": 76.0},
                {"quarter": 3, "soc": 72.0},
            ],
        },
    ]
    overlay_plan_row_soc_q15(slots, rows, "2026-11-12")
    assert slots[6 * 4] == 88.0
    assert slots[7 * 4] == 84.0
    assert slots[7 * 4 + 3] == 72.0
    assert slots[8 * 4] is None
    assert slots[9 * 4] is None


def test_compose_replaces_locked_curve_once_rows_are_the_source():
    from src.plan_simulation import compose_plan_soc_q15

    existing = {
        "today_date": "2026-11-12",
        "plan_soc_day_locked": True,
        "plan_soc_q15": {"today": [80.0] * 96, "tomorrow": [20.0] * 96},
    }
    fresh = {
        "today_date": "2026-11-12",
        "plan_soc_from_plan_rows": True,
        "plan_soc_q15": {"today": [72.0] * 96, "tomorrow": [30.0] * 96},
    }
    out = compose_plan_soc_q15(existing, fresh)
    assert out["today"][7 * 4] == 72.0

    existing["plan_soc_from_plan_rows"] = True
    existing["plan_soc_q15"] = {"today": [72.0] * 96, "tomorrow": [20.0] * 96}
    fresh["plan_soc_q15"] = {"today": [10.0] * 96, "tomorrow": [30.0] * 96}
    out = compose_plan_soc_q15(existing, fresh)
    assert out["today"][7 * 4] == 72.0
