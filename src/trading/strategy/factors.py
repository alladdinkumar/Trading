"""Path A — pure, point-in-time cross-sectional factor builders.

No I/O. Mirrors the purity of ``ranking/ranker_features.py``: the caller
supplies in-memory OHLCV frames and an ``as_of`` date. Every factor at date
``t`` uses only bars with index <= ``t`` (no look-ahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MOMENTUM_MIN_BARS = 273  # 252-day lookback + 21-day skip buffer
MOMENTUM_SKIP = 21
MOMENTUM_LOOKBACK = 252


def momentum_12_1(df: pd.DataFrame, as_of: pd.Timestamp) -> float | None:
    """12-1 momentum: ``close[t-21] / close[t-252] - 1``.

    Skips the most recent ~21 trading days to avoid short-term reversal.
    Returns ``None`` when fewer than ``MOMENTUM_MIN_BARS`` bars are available
    at/through ``as_of``.
    """
    if as_of not in df.index:
        return None
    until = df.loc[:as_of]
    if len(until) < MOMENTUM_MIN_BARS:
        return None
    recent = float(until["close"].iloc[-MOMENTUM_SKIP - 1])
    base = float(until["close"].iloc[-MOMENTUM_LOOKBACK - 1])
    if base == 0:
        return None
    return recent / base - 1.0
