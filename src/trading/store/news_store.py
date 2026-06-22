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
from datetime import date, timedelta

from trading.domain import NewsItem

# Sentiment at or below this is "negative" for the news-veto / ranker feature.
NEGATIVE_SENTIMENT_THRESHOLD = -0.20


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
    """Insert a batch of news rows. Returns how many were *newly* inserted.

    `INSERT OR IGNORE` leans on the v3 `idx_news_dedup` unique index
    (`source, headline, COALESCE(url,'')`) so a daily re-fetch of the same RSS
    article or NSE event is a no-op rather than a duplicate row (F-016). The
    return value is the count actually written (re-runs of identical batches
    return 0), measured via `conn.total_changes`.
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
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO news_items (
          ts, symbol, source, headline, url, sentiment, category, is_critical
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return conn.total_changes - before


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


def negative_news_count_7d(
    conn: sqlite3.Connection, symbol: str, as_of: date
) -> int | None:
    """Count negative-sentiment news (sentiment < -0.20) in the trailing 7 days.

    The single source of truth for the `negative_news_count_7d` ranker feature,
    used by **both** inference (`ranking.ranker`) and training-input assembly
    (`ranking.ranker_io`) so the two paths compute it identically (F-031).

    Returns ``None`` when no news rows fall in the window (caller propagates as
    NaN), and ``0`` when news rows exist but none are negative.
    """
    start = (as_of - timedelta(days=7)).isoformat()
    end_ts = f"{as_of.isoformat()}T23:59:59"
    row = conn.execute(
        "SELECT COUNT(*) AS c, "
        "       SUM(CASE WHEN sentiment < ? THEN 1 ELSE 0 END) AS n "
        "FROM news_items "
        "WHERE symbol = ? AND ts >= ? AND ts <= ?",
        (NEGATIVE_SENTIMENT_THRESHOLD, symbol, start, end_ts),
    ).fetchone()
    if row is None or row["c"] == 0:
        return None
    return int(row["n"] or 0)


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


def list_critical_symbols(conn: sqlite3.Connection, date_iso: str) -> set[str]:
    """Symbols whose `sentiment_daily` rollup flags a critical event on `date_iso`.

    Feeds Layer A's `no_critical_event` veto (the hard sentiment gate) via the
    scan context — see `jobs/pre_open.build_scan_context`.
    """
    rows = conn.execute(
        "SELECT symbol FROM sentiment_daily WHERE date = ? AND has_critical = 1",
        (date_iso,),
    ).fetchall()
    return {r["symbol"] for r in rows}


def _row_to_sentiment_daily(row: sqlite3.Row) -> SentimentDailyRow:
    return SentimentDailyRow(
        date=row["date"],
        symbol=row["symbol"],
        score_7d=row["score_7d"],
        score_30d=row["score_30d"],
        news_count=row["news_count"],
        negative_news_count=row["negative_news_count"],
        has_critical=bool(row["has_critical"]),
    )


def get_sentiment_daily(
    conn: sqlite3.Connection, date_iso: str, symbol: str
) -> SentimentDailyRow | None:
    row = conn.execute(
        "SELECT * FROM sentiment_daily WHERE date = ? AND symbol = ?",
        (date_iso, symbol),
    ).fetchone()
    return _row_to_sentiment_daily(row) if row is not None else None


def get_latest_sentiment_daily(
    conn: sqlite3.Connection, symbol: str, *, on_or_before: str
) -> SentimentDailyRow | None:
    """Most recent `sentiment_daily` rollup for `symbol` dated ≤ `on_or_before`.

    Rollups are written per-day only when news flows, so the as-of date often
    has no row (weekends, quiet days). The health scorer wants the freshest
    available 30d view — including its `has_critical` flag — so it reads the
    latest row at or before the run date rather than requiring an exact match.
    """
    row = conn.execute(
        "SELECT * FROM sentiment_daily WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (symbol, on_or_before),
    ).fetchone()
    return _row_to_sentiment_daily(row) if row is not None else None
