"""Phase 16 feature builders for the LightGBM ranker.

Pure functions only — no I/O. Caller supplies the enriched OHLCV slice,
the signal date, and a `LiveContext` with macro / sentiment slots already
fetched. `FEATURE_NAMES` is the single source of truth for column order;
training and inference both iterate it to assemble the matrix.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trading.data.macro import MacroSnapshot
from trading.store.news_store import SentimentDailyRow

FEATURE_NAMES: tuple[str, ...] = (
    # setup
    "rsi_14",
    "pullback_pct_20",
    "pullback_pct_50",
    "atr_pct",
    "dist_from_52w_high",
    # trend
    "sma_20_slope_5d",
    "sma_50_slope_10d",
    "sma_200_slope_20d",
    "adx_14",
    "dist_from_52w_low",
    # volume
    "volume_vs_20d_avg",
    "obv_slope_5d",
    # macro
    "vix",
    "vix_change_5d",
    "fii_flow_5d_sum",
    "usdinr_change_5d",
    "regime_ord",
    # sentiment
    "sentiment_7d",
    "sentiment_30d",
    "negative_news_count_7d",
)


@dataclass(frozen=True)
class LiveContext:
    """Per-candidate live context. Any slot may be None → NaN for those features.

    `macro_history` is a DataFrame indexed by ISO date with `vix`, `usdinr`,
    `fii_flow_cr` columns. Used to compute 5-day changes / sums centred on
    `as_of`. When unavailable, the macro change/sum features fall back to NaN.

    `negative_news_count_7d` is a precomputed scalar so this module stays
    DB-free; the caller (training loop / inference) pulls it from
    `news_items` once.
    """

    macro: MacroSnapshot | None = None
    sentiment: SentimentDailyRow | None = None
    macro_history: pd.DataFrame | None = None
    negative_news_count_7d: int | None = None


def build_feature_row(
    enriched_df: pd.DataFrame,
    signal_date: pd.Timestamp,
    live_ctx: LiveContext,
) -> dict[str, float]:
    """Build a single feature row keyed by FEATURE_NAMES.

    Returns a dict with NaN for any feature whose inputs are missing.
    Caller stacks rows into a DataFrame using FEATURE_NAMES as column order.
    """
    raise NotImplementedError
