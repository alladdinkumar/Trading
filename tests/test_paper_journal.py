"""Tests for trading.paper.journal — bought/target/deviation date helpers."""

from __future__ import annotations

from datetime import date

from trading.paper.journal import deviation_label, expected_target_date


def test_expected_target_date_adds_trading_days() -> None:
    # 2026-06-01 is a Monday; +5 trading days → Monday 2026-06-08.
    assert expected_target_date("2026-06-01T09:20:00", 5) == date(2026, 6, 8)


def test_expected_target_date_parses_date_only_string() -> None:
    assert expected_target_date("2026-06-01", 5) == date(2026, 6, 8)


def test_deviation_label_closed_early() -> None:
    target = date(2026, 6, 8)
    # Exit Friday 2026-06-05; the next trading day is the Monday target, so the
    # weekend collapses to a single trading-day step early.
    assert (
        deviation_label(target, exit_iso="2026-06-05T15:30:00", as_of=date(2026, 6, 19))
        == "-1d early"
    )


def test_deviation_label_closed_late() -> None:
    target = date(2026, 6, 8)
    # Exit Wednesday 2026-06-10, two trading days after target.
    assert (
        deviation_label(target, exit_iso="2026-06-10T15:30:00", as_of=date(2026, 6, 19))
        == "+2d late"
    )


def test_deviation_label_closed_on_time() -> None:
    target = date(2026, 6, 8)
    assert (
        deviation_label(target, exit_iso="2026-06-08T15:30:00", as_of=date(2026, 6, 19))
        == "on time"
    )


def test_deviation_label_open_remaining() -> None:
    target = date(2026, 6, 19)
    # as_of Monday 2026-06-15, four trading days before target.
    assert deviation_label(target, exit_iso=None, as_of=date(2026, 6, 15)) == "4d left"


def test_deviation_label_open_overdue() -> None:
    target = date(2026, 6, 8)
    # as_of Wednesday 2026-06-10, two trading days past target.
    assert deviation_label(target, exit_iso=None, as_of=date(2026, 6, 10)) == "+2d overdue"
