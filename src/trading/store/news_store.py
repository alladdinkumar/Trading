"""Persistence helpers for `news_items` and `sentiment_daily`.

Kept separate from `repo.py` because (a) different lifecycle — repo.py
tracks signal/trade state mutated by jobs, while news rows are accreted
and rarely updated — and (b) news rows are bulk-inserted in batches of
dozens, so a vectorised `executemany` path makes more sense than the
per-row helpers in repo.py.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from trading.data.news import NewsItem


@dataclass(frozen=True)
class SentimentDailyRow:
    """One row of `sentiment_daily` (per-stock per-day rollup)."""

    date: str  # YYYY-MM-DD
    symbol: str
    score_7d: float | None
    score_30d: float | None
    news_count: int
    negative_news_count: int
    has_critical: bool


# ---------------------------------------------------------------------------
# news_items
# ---------------------------------------------------------------------------


def insert_news_items(conn: sqlite3.Connection, items: Iterable[NewsItem]) -> int:
    """Insert a batch of news rows. Returns how many were inserted.

    No `INSERT OR IGNORE` here — callers dedupe by URL before this point
    (see `news.fetch_all_news`). If we ever pull from overlapping batches,
    add a UNIQUE(url) index in a v2 migration first.
    """
    rows = [
        (
            i.ts,
            i.symbol,
            i.source,
            i.headline,
            i.url,
            i.sentiment,
            i.category,
            int(i.is_critical),
        )
        for i in items
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO news_items (
          ts, symbol, source, headline, url, sentiment, category, is_critical
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def _row_to_news_item(row: sqlite3.Row) -> NewsItem:
    return NewsItem(
        ts=row["ts"],
        symbol=row["symbol"],
        source=row["source"],
        headline=row["headline"],
        url=row["url"],
        sentiment=row["sentiment"],
        category=row["category"],
        is_critical=bool(row["is_critical"]),
    )


def list_news_for_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    since_ts: str | None = None,
) -> list[NewsItem]:
    """All news for a symbol, optionally restricted to `ts >= since_ts`."""
    if since_ts is None:
        rows = conn.execute(
            "SELECT * FROM news_items WHERE symbol = ? ORDER BY ts",
            (symbol,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM news_items WHERE symbol = ? AND ts >= ? ORDER BY ts",
            (symbol, since_ts),
        ).fetchall()
    return [_row_to_news_item(r) for r in rows]


def list_critical_for_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    since_ts: str,
) -> list[NewsItem]:
    """Critical-flagged news only — the hard-veto input for Layer A §4.1."""
    rows = conn.execute(
        """SELECT * FROM news_items
           WHERE symbol = ? AND is_critical = 1 AND ts >= ?
           ORDER BY ts""",
        (symbol, since_ts),
    ).fetchall()
    return [_row_to_news_item(r) for r in rows]


# ---------------------------------------------------------------------------
# sentiment_daily
# ---------------------------------------------------------------------------


def upsert_sentiment_daily(conn: sqlite3.Connection, row: SentimentDailyRow) -> None:
    """Insert-or-replace one (date, symbol) rollup row."""
    conn.execute(
        """
        INSERT INTO sentiment_daily (
          date, symbol, score_7d, score_30d, news_count,
          negative_news_count, has_critical
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, symbol) DO UPDATE SET
          score_7d            = excluded.score_7d,
          score_30d           = excluded.score_30d,
          news_count          = excluded.news_count,
          negative_news_count = excluded.negative_news_count,
          has_critical        = excluded.has_critical
        """,
        (
            row.date,
            row.symbol,
            row.score_7d,
            row.score_30d,
            row.news_count,
            row.negative_news_count,
            int(row.has_critical),
        ),
    )


def get_sentiment_daily(
    conn: sqlite3.Connection, date_iso: str, symbol: str
) -> SentimentDailyRow | None:
    row = conn.execute(
        "SELECT * FROM sentiment_daily WHERE date = ? AND symbol = ?",
        (date_iso, symbol),
    ).fetchone()
    if row is None:
        return None
    return SentimentDailyRow(
        date=row["date"],
        symbol=row["symbol"],
        score_7d=row["score_7d"],
        score_30d=row["score_30d"],
        news_count=row["news_count"],
        negative_news_count=row["negative_news_count"],
        has_critical=bool(row["has_critical"]),
    )
