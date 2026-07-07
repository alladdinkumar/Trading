"""Tests for trading.store.news_store — news_items + sentiment_daily DB."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from trading.domain import NewsItem
from trading.store.migrations import run_migrations
from trading.store.news_store import (
    SentimentDailyRow,
    get_latest_sentiment_daily,
    get_sentiment_daily,
    insert_news_items,
    list_critical_for_symbol,
    list_news_for_symbol,
    negative_news_count_7d,
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


# ---------------------------------------------------------------------------
# negative_news_count_7d — the shared train/serve feature (F-031)
# ---------------------------------------------------------------------------


def _neg_item(symbol: str, ts: str, sentiment: float | None) -> NewsItem:
    return NewsItem(
        ts=ts,
        symbol=symbol,
        source="mc",
        headline=f"{symbol} {ts} {sentiment}",
        url=f"https://x/{symbol}/{ts}",
        sentiment=sentiment,
    )


def test_negative_news_count_7d_counts_negatives_in_window(conn: sqlite3.Connection) -> None:
    insert_news_items(
        conn,
        [
            _neg_item("RELIANCE", "2026-05-18T10:00:00+00:00", -0.5),  # negative, in window
            _neg_item("RELIANCE", "2026-05-15T10:00:00+00:00", -0.30),  # negative, in window
            _neg_item("RELIANCE", "2026-05-19T10:00:00+00:00", 0.40),  # positive, in window
            _neg_item("RELIANCE", "2026-05-10T10:00:00+00:00", -0.9),  # negative, BEFORE window
        ],
    )
    assert negative_news_count_7d(conn, "RELIANCE", date(2026, 5, 20)) == 2


def test_negative_news_count_7d_none_when_no_news_in_window(conn: sqlite3.Connection) -> None:
    # News exists but only outside the trailing-7d window → None (→ NaN feature).
    insert_news_items(conn, [_neg_item("RELIANCE", "2026-05-10T10:00:00+00:00", -0.9)])
    assert negative_news_count_7d(conn, "RELIANCE", date(2026, 5, 20)) is None
    # A symbol with no news at all → None.
    assert negative_news_count_7d(conn, "TCS", date(2026, 5, 20)) is None


def test_negative_news_count_7d_zero_when_news_but_none_negative(conn: sqlite3.Connection) -> None:
    insert_news_items(
        conn,
        [
            _neg_item("RELIANCE", "2026-05-18T10:00:00+00:00", 0.4),
            _neg_item("RELIANCE", "2026-05-19T10:00:00+00:00", -0.10),  # above -0.20 threshold
        ],
    )
    assert negative_news_count_7d(conn, "RELIANCE", date(2026, 5, 20)) == 0


def test_negative_news_count_7d_includes_ist_early_morning_edge(
    conn: sqlite3.Connection,
) -> None:
    """F-065: a negative headline in the IST early morning of the window's start
    day is stored on the *previous* UTC date. With IST-derived UTC bounds it must
    still fall inside the trailing-7d window, not drop out.

    as_of = 2026-05-20 (IST). Window start = IST midnight 2026-05-13, which is
    2026-05-12T18:30:00+00:00 in UTC. A headline at 01:00 IST on 2026-05-13 is
    stored as 2026-05-12T19:30:00+00:00 — inside the IST window, but *before* a
    naive "2026-05-13" date-string bound.
    """
    insert_news_items(
        conn,
        [_neg_item("RELIANCE", "2026-05-12T19:30:00+00:00", -0.5)],
    )
    assert negative_news_count_7d(conn, "RELIANCE", date(2026, 5, 20)) == 1


def _news(headline: str, *, source: str = "mc", url: str | None = "https://x/1") -> NewsItem:
    return NewsItem(
        ts="2026-05-13T10:00:00+00:00",
        symbol=None,
        source=source,
        headline=headline,
        url=url,
    )


def test_insert_news_items_ignores_duplicate_across_runs(conn: sqlite3.Connection) -> None:
    """F-016: re-inserting an identical row (same source/headline/url) is a DB
    no-op, and the returned count reflects only the rows actually written."""
    item = _news("Reliance Q4 beat")
    assert insert_news_items(conn, [item]) == 1
    assert insert_news_items(conn, [item]) == 0  # second run: nothing new
    assert conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"] == 1


def test_insert_news_items_keeps_distinct_headlines_sharing_url(
    conn: sqlite3.Connection,
) -> None:
    """Dedup key includes the headline, so two events that share a URL both
    persist (F-016 — NSE-style rows)."""
    url = "https://nse?symbol=RVNL"
    n = insert_news_items(
        conn,
        [
            _news("RVNL: Board Meeting", source="nse_events", url=url),
            _news("RVNL: Dividend", source="nse_events", url=url),
        ],
    )
    assert n == 2
    assert conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"] == 2


def test_insert_news_items_dedups_null_url_by_headline(conn: sqlite3.Connection) -> None:
    """Null-URL rows dedupe on (source, headline) rather than collapsing every
    null-URL row into one (F-016)."""
    macro = _news("Macro: Nifty closes higher", url=None)
    other = _news("Macro: rupee weakens", url=None)
    assert insert_news_items(conn, [macro, other]) == 2  # distinct headlines kept
    assert insert_news_items(conn, [macro]) == 0  # exact repeat ignored
    assert conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"] == 2


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


def test_get_latest_sentiment_picks_most_recent_on_or_before(conn: sqlite3.Connection) -> None:
    upsert_sentiment_daily(conn, _sentiment("2026-05-10", "RVNL", score_30d=0.1))
    upsert_sentiment_daily(conn, _sentiment("2026-05-14", "RVNL", score_30d=0.3))
    upsert_sentiment_daily(conn, _sentiment("2026-05-20", "RVNL", score_30d=0.9))  # future

    row = get_latest_sentiment_daily(conn, "RVNL", on_or_before="2026-05-16")
    assert row is not None
    assert row.date == "2026-05-14"  # newest ≤ as_of, not the future row
    assert row.score_30d == pytest.approx(0.3)


def test_get_latest_sentiment_carries_critical_flag(conn: sqlite3.Connection) -> None:
    upsert_sentiment_daily(conn, _sentiment("2026-05-12", "RVNL", has_critical=True))
    row = get_latest_sentiment_daily(conn, "RVNL", on_or_before="2026-05-16")
    assert row is not None and row.has_critical is True


def test_get_latest_sentiment_none_when_only_future_rows(conn: sqlite3.Connection) -> None:
    upsert_sentiment_daily(conn, _sentiment("2026-05-20", "RVNL"))
    assert get_latest_sentiment_daily(conn, "RVNL", on_or_before="2026-05-16") is None
