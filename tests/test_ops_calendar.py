"""Tests for src/trading/ops/calendar.py."""

from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with a clean nse_holidays cache."""
    from trading.ops import calendar as cal

    cal.nse_holidays.cache_clear()
    yield
    cal.nse_holidays.cache_clear()


def test_weekend_is_not_trading_day(monkeypatch):
    from trading.ops import calendar as cal

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", lambda year: frozenset())
    assert cal.is_trading_day(date(2026, 5, 23)) is False  # Saturday
    assert cal.is_trading_day(date(2026, 5, 24)) is False  # Sunday


def test_weekday_non_holiday_is_trading_day(monkeypatch):
    from trading.ops import calendar as cal

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", lambda year: frozenset())
    assert cal.is_trading_day(date(2026, 5, 25)) is True  # Monday


def test_known_holiday_is_not_trading_day(monkeypatch):
    from trading.ops import calendar as cal

    monkeypatch.setattr(
        cal,
        "_fetch_holidays_from_nsepython",
        lambda year: frozenset({date(2026, 1, 26)}),
    )
    assert cal.is_trading_day(date(2026, 1, 26)) is False


def test_nsepython_failure_falls_back_to_bundled(monkeypatch):
    from trading.ops import calendar as cal

    def boom(year):
        raise RuntimeError("nsepython api down")

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", boom)
    # Republic Day 2026 is in bundled JSON
    assert cal.is_trading_day(date(2026, 1, 26)) is False


def test_missing_bundled_falls_back_to_weekday_only(tmp_path, monkeypatch):
    from trading.ops import calendar as cal

    def boom(year):
        raise RuntimeError("api down")

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", boom)
    monkeypatch.setattr(cal, "_bundled_holidays_path", lambda year: tmp_path / "missing.json")
    # Holiday day becomes "weekday → trading day" because we have no holiday data
    assert cal.is_trading_day(date(2026, 1, 26)) is True


def test_caching_avoids_repeat_fetch(monkeypatch):
    from trading.ops import calendar as cal

    calls = []

    def counted(year):
        calls.append(year)
        return frozenset()

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", counted)
    cal.is_trading_day(date(2026, 5, 25))
    cal.is_trading_day(date(2026, 5, 26))
    cal.is_trading_day(date(2026, 5, 27))
    assert calls == [2026]


def test_year_boundary(monkeypatch):
    from trading.ops import calendar as cal

    calls = []

    def counted(year):
        calls.append(year)
        return frozenset()

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", counted)
    cal.is_trading_day(date(2026, 12, 31))
    cal.is_trading_day(date(2027, 1, 1))
    assert calls == [2026, 2027]
