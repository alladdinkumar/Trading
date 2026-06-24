"""Path A — pure, point-in-time cross-sectional factor builders.

No I/O. Mirrors the purity of ``ranking/ranker_features.py``: the caller
supplies in-memory OHLCV frames and an ``as_of`` date. Every factor at date
``t`` uses only bars with index <= ``t`` (no look-ahead).
"""

from __future__ import annotations

from collections.abc import Mapping

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


def realized_vol(df: pd.DataFrame, as_of: pd.Timestamp, *, window: int = 90) -> float | None:
    """Sample stdev (ddof=1) of trailing daily log returns over ``window`` bars.

    Returns ``None`` when fewer than ``window + 1`` bars are available
    at/through ``as_of`` (need window+1 prices for window returns).
    """
    if as_of not in df.index:
        return None
    until = df.loc[:as_of]
    if len(until) < window + 1:
        return None
    log_ret = np.diff(np.log(until["close"].to_numpy()))
    return float(np.std(log_ret[-window:], ddof=1))


def _zscore(values: dict[str, float]) -> dict[str, float]:
    """Population z-score (ddof=0). Zero-stdev cross-section → all zeros."""
    arr = np.array(list(values.values()), dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=0))
    if std < 1e-12:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / std for k, v in values.items()}


def factor_score(
    panel: Mapping[str, pd.DataFrame],
    as_of: pd.Timestamp,
    *,
    vol_window: int = 90,
) -> dict[str, float]:
    """Cross-sectional equal-weight composite of 12-1 momentum and low vol.

    For each symbol compute both factors point-in-time; drop any symbol whose
    either factor is ``None``. Z-score each factor across the survivors
    (momentum: higher is better; volatility negated so lower is better).
    Composite = mean(z_momentum, z_lowvol). Returns ``{}`` if < 2 survive.
    """
    mom: dict[str, float] = {}
    vol: dict[str, float] = {}
    for sym, df in panel.items():
        m = momentum_12_1(df, as_of)
        v = realized_vol(df, as_of, window=vol_window)
        if m is None or v is None:
            continue
        mom[sym] = m
        vol[sym] = v
    if len(mom) < 2:
        return {}
    z_mom = _zscore(mom)
    z_vol = _zscore(vol)
    return {sym: (z_mom[sym] + (-z_vol[sym])) / 2.0 for sym in mom}
