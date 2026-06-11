"""Shared loader for ranker training inputs (parquet + SQLite).

Extracted from the `train-ranker` CLI so weekly_train (Phase 18) reuses
the exact same wiring: enriched OHLCV per symbol with 200+ bars, macro
history frame, and a (date, symbol) → SentimentDailyRow lookup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

from trading.config import Paths
from trading.features.technicals import add_indicators
from trading.store.news_store import SentimentDailyRow
from trading.store.ohlcv import list_symbols, read_ohlcv

MIN_BARS = 200


@dataclass(frozen=True)
class TrainingInputs:
    enriched: dict[str, pd.DataFrame]
    macro_history: pd.DataFrame
    sentiment_lookup: dict[tuple[str, str], SentimentDailyRow]


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
        for r in conn.execute(
            "SELECT * FROM sentiment_daily WHERE symbol = ?", (s,)
        ).fetchall():
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
    )
