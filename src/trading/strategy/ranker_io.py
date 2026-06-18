"""Shared loader for ranker training inputs (parquet + SQLite).

Extracted from the `train-ranker` CLI so weekly_train (Phase 18) reuses
the exact same wiring: enriched OHLCV per symbol with 200+ bars, macro
history frame, and a (date, symbol) → SentimentDailyRow lookup.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from trading.config import Paths
from trading.features.technicals import add_indicators
from trading.store.news_store import SentimentDailyRow, negative_news_count_7d
from trading.store.ohlcv import list_symbols, read_ohlcv

MIN_BARS = 200


@dataclass(frozen=True)
class TrainingInputs:
    enriched: dict[str, pd.DataFrame]
    macro_history: pd.DataFrame
    sentiment_lookup: dict[tuple[str, str], SentimentDailyRow]
    negative_news_lookup: dict[tuple[str, str], int]


def build_negative_news_lookup(
    conn: sqlite3.Connection, enriched: Mapping[str, pd.DataFrame]
) -> dict[tuple[str, str], int]:
    """Precompute the `negative_news_count_7d` feature for the training window.

    Keyed ``(date_iso, symbol)`` to mirror `sentiment_lookup`, computed via the
    **same** `news_store.negative_news_count_7d` that inference uses, so training
    and serving feed the feature identically (F-031). Only entries with news in
    the trailing-7d window are stored; a missing key resolves to NaN in
    `_build_xy_for_window`, exactly as `None` does at inference.

    Work is bounded to each symbol's news-active span (min news date →
    max news date + 7d) intersected with its trading-date index — so symbols
    with no news contribute nothing.
    """
    lookup: dict[tuple[str, str], int] = {}
    for sym, df in enriched.items():
        news_dates = conn.execute(
            "SELECT MIN(substr(ts, 1, 10)) AS lo, MAX(substr(ts, 1, 10)) AS hi "
            "FROM news_items WHERE symbol = ?",
            (sym,),
        ).fetchone()
        if news_dates is None or news_dates["lo"] is None:
            continue
        lo = pd.Timestamp(news_dates["lo"])
        hi = pd.Timestamp(news_dates["hi"]) + pd.Timedelta(days=7)
        in_span = df.index[(df.index >= lo) & (df.index <= hi)]
        for sd in in_span:
            n = negative_news_count_7d(conn, sym, sd.date())
            if n is not None:
                lookup[(sd.strftime("%Y-%m-%d"), sym)] = n
    return lookup


def load_training_inputs(paths: Paths, conn: sqlite3.Connection) -> TrainingInputs:
    """Load every input `train_walkforward` needs from parquet + SQLite."""
    enriched: dict[str, pd.DataFrame] = {}
    for s in list_symbols(paths):
        try:
            df = read_ohlcv(s, paths)
        except FileNotFoundError:
            continue
        if len(df) < MIN_BARS:
            continue
        enriched[s] = add_indicators(df)

    macro_rows = conn.execute(
        "SELECT date, vix, usdinr, fii_flow_cr FROM macro_snapshot ORDER BY date"
    ).fetchall()
    macro_history = pd.DataFrame(
        {
            "vix": [r["vix"] for r in macro_rows],
            "usdinr": [r["usdinr"] for r in macro_rows],
            "fii_flow_cr": [r["fii_flow_cr"] for r in macro_rows],
        },
        index=[r["date"] for r in macro_rows],
    )

    sentiment_lookup: dict[tuple[str, str], SentimentDailyRow] = {}
    for s in enriched:
        for r in conn.execute("SELECT * FROM sentiment_daily WHERE symbol = ?", (s,)).fetchall():
            sentiment_lookup[(r["date"], s)] = SentimentDailyRow(
                date=r["date"],
                symbol=s,
                score_7d=r["score_7d"],
                score_30d=r["score_30d"],
                news_count=r["news_count"],
                negative_news_count=r["negative_news_count"],
                has_critical=bool(r["has_critical"]),
            )
    return TrainingInputs(
        enriched=enriched,
        macro_history=macro_history,
        sentiment_lookup=sentiment_lookup,
        negative_news_lookup=build_negative_news_lookup(conn, enriched),
    )
