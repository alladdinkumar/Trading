"""Assemble a `HoldingContext` from the live data sources (F-022).

`health.score_holding` is pure policy over a `HoldingContext`; this is the thin
glue that fills that context from real inputs — OHLCV technicals, the static
fundamentals CSV, and the latest `sentiment_daily` rollup. Centralised here so
`pre_open` and `monthly_sip` wire the same three axes identically (and so the
critical-news EXIT veto is live in both, not just in the scan gate).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from trading.portfolio.health import (
    FundamentalsSnapshot,
    HoldingContext,
    SentimentSnapshot,
    technicals_from_history,
)
from trading.store.news_store import get_latest_sentiment_daily


def build_holding_context(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    qty: int,
    avg_price: float,
    last_price: float,
    history: object,
    as_of: date,
    fundamentals_map: dict[str, FundamentalsSnapshot],
) -> HoldingContext:
    """Build a fully-wired `HoldingContext` for one holding.

    Sentiment is the freshest `sentiment_daily` rollup at or before `as_of`
    (carrying its `has_critical` flag, which drives the EXIT veto); fundamentals
    come from `fundamentals_map` (empty snapshot when the symbol is absent);
    technicals are derived from the OHLCV `history`.
    """
    row = get_latest_sentiment_daily(conn, symbol, on_or_before=as_of.isoformat())
    sentiment = (
        SentimentSnapshot(score_30d=row.score_30d, has_critical=row.has_critical)
        if row is not None
        else SentimentSnapshot()
    )
    return HoldingContext(
        symbol=symbol,
        qty=qty,
        avg_price=avg_price,
        last_price=last_price,
        technicals=technicals_from_history(history),
        fundamentals=fundamentals_map.get(symbol, FundamentalsSnapshot()),
        sentiment=sentiment,
    )
