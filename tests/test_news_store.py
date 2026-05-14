"""Tests for trading.store.news_store — news_items + sentiment_daily DB."""

from __future__ import annotations

import sqlite3

import pytest

from trading.data.news import NewsItem
from trading.store.migrations import run_migrations
from trading.store.news_store import (
    SentimentDailyRow,
    get_sentiment_daily,
    insert_news_items,
    list_critical_for_symbol,
    list_news_for_symbol,
    upsert_sentiment_daily,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


# ---------------------------------------------------------------------------
# news_items
# ---------------------------------------------------------------------------


def test_insert_news_items_returns_count(conn: sqlite3.Connection) -> None:
    items = [
        NewsItem(
            ts=f"2026-05-13T10:0{i}:00+00:00",
            symbol="RELIANCE" if i % 2 == 0 else None,
            source="mc",
            headline=f"headline {i}",
            url=f"https://x/{i}",
            sentiment=0.1 * i,
            category="results",
            is_critical=(i == 3),
        )
        for i in range(5)
    ]
    n = insert_news_items(conn, items)
    assert n == 5
    rows = conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()
    assert rows["c"] == 5


def test_insert_news_items_empty_is_noop(conn: sqlite3.Connection) -> None:
    assert insert_news_items(conn, []) == 0


def test_list_news_for_symbol_filters_by_symbol(conn: sqlite3.Connection) -> None:
    items = [
        NewsItem(
            ts="2026-05-13T10:00:00+00:00",
            symbol="RELIANCE",
            source="mc",
            headline="reliance news",
            url="https://x/1",
        ),
        NewsItem(
            ts="2026-05-13T11:00:00+00:00",
            symbol="NTPC",
            source="mc",
            headline="ntpc news",
            url="https://x/2",
        ),
        NewsItem(
            ts="2026-05-13T12:00:00+00:00",
            symbol=None,
            source="mc",
            headline="general news",
            url="https://x/3",
        ),
    ]
    insert_news_items(conn, items)
    out = list_news_for_symbol(conn, "RELIANCE")
    assert [i.headline for i in out] == ["reliance news"]


def test_list_news_for_symbol_since_filter(conn: sqlite3.Connection) -> None:
    items = [
        NewsItem(
            ts=f"2026-05-{day:02d}T10:00:00+00:00",
            symbol="R",
            source="mc",
            headline=f"day {day}",
            url=f"https://x/{day}",
        )
        for day in (10, 11, 12, 13)
    ]
    insert_news_items(conn, items)
    out = list_news_for_symbol(conn, "R", since_ts="2026-05-12T00:00:00+00:00")
    assert [i.headline for i in out] == ["day 12", "day 13"]


def test_list_critical_for_symbol(conn: sqlite3.Connection) -> None:
    items = [
        NewsItem(
            ts="2026-05-13T10:00:00+00:00",
            symbol="X",
            source="mc",
            headline="normal",
            url="https://x/1",
            is_critical=False,
        ),
        NewsItem(
            ts="2026-05-13T11:00:00+00:00",
            symbol="X",
            source="mc",
            headline="SEBI orders probe",
            url="https://x/2",
            is_critical=True,
        ),
        NewsItem(
            ts="2026-04-01T10:00:00+00:00",
            symbol="X",
            source="mc",
            headline="old critical (out of window)",
            url="https://x/3",
            is_critical=True,
        ),
    ]
    insert_news_items(conn, items)
    out = list_critical_for_symbol(conn, "X", since_ts="2026-05-01T00:00:00+00:00")
    assert [i.headline for i in out] == ["SEBI orders probe"]


def test_news_item_roundtrip_preserves_fields(conn: sqlite3.Connection) -> None:
    item = NewsItem(
        ts="2026-05-13T10:00:00+00:00",
        symbol="RELIANCE",
        source="mc",
        headline="Reliance Q4 beat",
        url="https://x/abc",
        sentiment=0.42,
        category="results",
        is_critical=False,
    )
    insert_news_items(conn, [item])
    out = list_news_for_symbol(conn, "RELIANCE")
    assert len(out) == 1
    got = out[0]
    assert got.headline == item.headline
    assert got.sentiment == pytest.approx(0.42)
    assert got.category == "results"
    assert got.is_critical is False


# ---------------------------------------------------------------------------
# sentiment_daily
# ---------------------------------------------------------------------------


def test_upsert_sentiment_daily_inserts_then_replaces(conn: sqlite3.Connection) -> None:
    row = SentimentDailyRow(
        date="2026-05-13",
        symbol="RELIANCE",
        score_7d=0.2,
        score_30d=0.1,
        news_count=4,
        negative_news_count=1,
        has_critical=False,
    )
    upsert_sentiment_daily(conn, row)
    got = get_sentiment_daily(conn, "2026-05-13", "RELIANCE")
    assert got is not None
    assert got.score_7d == pytest.approx(0.2)
    assert got.news_count == 4

    # Replace
    upsert_sentiment_daily(
        conn,
        SentimentDailyRow(
            date="2026-05-13",
            symbol="RELIANCE",
            score_7d=-0.3,
            score_30d=-0.1,
            news_count=10,
            negative_news_count=5,
            has_critical=True,
        ),
    )
    got2 = get_sentiment_daily(conn, "2026-05-13", "RELIANCE")
    assert got2 is not None
    assert got2.score_7d == pytest.approx(-0.3)
    assert got2.news_count == 10
    assert got2.has_critical is True


def test_get_sentiment_daily_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert get_sentiment_daily(conn, "2026-05-13", "NOPE") is None
