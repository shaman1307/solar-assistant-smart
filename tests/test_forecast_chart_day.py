"""Power Management chart dates: stored history through today + 6."""

from datetime import date, datetime, timedelta

from src import forecast as fm
from src.forecast_cache import _weekday_sample_dates


def test_plus_six_load_sample_starts_on_yesterday():
    today = date(2026, 11, 28)
    target = today + timedelta(days=6)
    assert _weekday_sample_dates(target)[0] == "2026-11-27"


def test_chart_bounds_span_archive_through_six_days(monkeypatch):
    monkeypatch.setattr(fm, "now_warsaw", lambda: datetime(2026, 11, 28, 8, 0))
    monkeypatch.setattr(
        "src.sqlite_store.list_plan_day_archives",
        lambda limit=None: ["2026-11-27", "2026-06-01"],
    )
    assert fm.forecast_chart_bounds() == {
        "min_date": "2026-06-01",
        "max_date": "2026-12-04",
        "today": "2026-11-28",
    }


def test_chart_day_rejects_past_the_six_day_limit(monkeypatch):
    monkeypatch.setattr(
        fm,
        "forecast_chart_bounds",
        lambda now=None: {"min_date": "2026-06-01", "max_date": "2026-12-04", "today": "2026-11-28"},
    )
    try:
        fm.build_forecast_chart_day({}, "2026-12-05")
        assert False
    except ValueError:
        pass


def test_future_chart_day_uses_existing_pv_and_weekday_load(monkeypatch):
    calls: list[tuple[str, str]] = []

    def pv(date_str, _cfg):
        calls.append(("pv", date_str))
        return [1.0] * 24, [0.25] * 96, {"time": ["t"]}

    def load(date_str, _cfg):
        calls.append(("load", date_str))
        return [0.1] * 96, [0.4] * 24

    monkeypatch.setattr("src.forecast_cache.compute_pv_profiles_for_date", pv)
    monkeypatch.setattr("src.forecast_cache.compute_weekday_load_profile", load)
    monkeypatch.setattr(fm, "unshaded_pv_q15", lambda *a, **k: [0.3] * 96)
    monkeypatch.setattr(
        fm,
        "forecast_chart_bounds",
        lambda now=None: {"min_date": "2026-06-01", "max_date": "2026-12-04", "today": "2026-11-28"},
    )
    day = fm.build_forecast_chart_day({}, "2026-12-04")
    assert calls == [("pv", "2026-12-04"), ("load", "2026-12-04")]
    assert day["past"] is False
    assert len(day["pv_forecast_q15"]) == 96
    assert day["load_q15"][0] == 0.1


def test_past_chart_day_reads_the_stored_snapshot(monkeypatch):
    monkeypatch.setattr(
        fm,
        "forecast_chart_bounds",
        lambda now=None: {"min_date": "2026-06-01", "max_date": "2026-12-04", "today": "2026-11-28"},
    )
    monkeypatch.setattr(
        "src.sqlite_store.load_plan_day_archive",
        lambda date_str: {
            "forecast": {"pv_q15": [1.5] * 96, "load_q15": [0.2] * 96},
            "plan_soc_q15": {"today": [40.0] * 96},
            "history_rows": [{"plan_date": date_str, "hour": 0}],
        },
    )
    day = fm.build_forecast_chart_day({}, "2026-11-20")
    assert day["past"] is True
    assert day["pv_forecast_q15"][0] == 1.5
    assert day["load_q15"][0] == 0.2
    assert day["plan_soc_q15"][0] == 40.0
    assert day["history_rows"][0]["hour"] == 0
