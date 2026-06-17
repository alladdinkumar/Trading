"""Tests for trading.portfolio.holding_context — wiring sentiment + fundamentals (F-022)."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from trading.portfolio.health import FundamentalsSnapshot, score_holding
from trading.portfolio.holding_context import build_holding_context
from trading.store.migrations import run_migrations
from trading.store.news_store import SentimentDailyRow, upsert_sentiment_daily


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _sentiment(date_iso: str, symbol: str, *, score_30d=0.0, has_critical=False):
    return SentimentDailyRow(
        date=date_iso,
        symbol=symbol,
        score_7d=0.0,
        score_30d=score_30d,
        news_count=1,
        negative_news_count=0,
        has_critical=has_critical,
    )


def test_context_pulls_latest_sentiment(conn: sqlite3.Connection) -> None:
    upsert_sentiment_daily(conn, _sentiment("2026-05-14", "RVNL", score_30d=0.42))
    ctx = build_holding_context(
        conn,
        symbol="RVNL",
        qty=10,
        avg_price=100.0,
        last_price=110.0,
        history=None,
        as_of=date(2026, 5, 16),
        fundamentals_map={},
    )
    assert ctx.sentiment.score_30d == pytest.approx(0.42)
    assert ctx.sentiment.has_critical is False


def test_context_critical_flag_drives_exit_veto(conn: sqlite3.Connection) -> None:
    """A holding with a critical sentiment rollup → EXIT verdict (the veto F-022
    revives by actually wiring sentiment into the context)."""
    upsert_sentiment_daily(conn, _sentiment("2026-05-12", "RVNL", has_critical=True))
    ctx = build_holding_context(
        conn,
        symbol="RVNL",
        qty=10,
        avg_price=100.0,
        last_price=110.0,
        history=None,
        as_of=date(2026, 5, 16),
        fundamentals_map={},
    )
    assert ctx.sentiment.has_critical is True
    assert score_holding(ctx).verdict == "EXIT"


def test_context_uses_fundamentals_from_map(conn: sqlite3.Connection) -> None:
    fmap = {"RVNL": FundamentalsSnapshot(roe=0.22, debt_to_equity=0.3)}
    ctx = build_holding_context(
        conn,
        symbol="RVNL",
        qty=10,
        avg_price=100.0,
        last_price=110.0,
        history=None,
        as_of=date(2026, 5, 16),
        fundamentals_map=fmap,
    )
    assert ctx.fundamentals.roe == pytest.approx(0.22)


def test_context_empty_when_no_data(conn: sqlite3.Connection) -> None:
    ctx = build_holding_context(
        conn,
        symbol="UNKNOWN",
        qty=1,
        avg_price=10.0,
        last_price=10.0,
        history=None,
        as_of=date(2026, 5, 16),
        fundamentals_map={},
    )
    assert ctx.sentiment.score_30d is None
    assert ctx.sentiment.has_critical is False
    assert ctx.fundamentals == FundamentalsSnapshot()
