import math

import numpy as np
import pandas as pd
import pytest

from trading.features.technicals import add_indicators
from trading.strategy.ranker_features import (
    FEATURE_NAMES,
    LiveContext,
    build_feature_row,
)


def test_feature_names_is_a_tuple_of_20_unique_strings() -> None:
    assert isinstance(FEATURE_NAMES, tuple)
    assert len(FEATURE_NAMES) == 20
    assert len(set(FEATURE_NAMES)) == 20
    assert all(isinstance(n, str) and n for n in FEATURE_NAMES)


def _synthetic_uptrend(n: int = 300, seed: int = 0) -> pd.DataFrame:
    """Synthetic OHLCV: clean uptrend with mild noise so SMA slopes are
    reliably positive across short and long horizons."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n)
    # Drift dominates noise so 5-bar SMA slope tests don't flake.
    close = 100 + np.cumsum(rng.normal(0.20, 0.20, size=n))
    high = close + rng.uniform(0.1, 1.0, size=n)
    low = close - rng.uniform(0.1, 1.0, size=n)
    open_ = close + rng.uniform(-0.5, 0.5, size=n)
    vol = rng.integers(80_000, 120_000, size=n).astype(int)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=dates,
    )
    return add_indicators(df)


def test_setup_features_present_on_uptrend_input() -> None:
    df = _synthetic_uptrend()
    signal_date = df.index[-1]
    row = build_feature_row(df, signal_date, LiveContext())
    for k in (
        "rsi_14",
        "pullback_pct_20",
        "pullback_pct_50",
        "atr_pct",
        "dist_from_52w_high",
    ):
        assert k in row
        assert not math.isnan(row[k])


def test_atr_pct_is_positive_and_small() -> None:
    df = _synthetic_uptrend()
    row = build_feature_row(df, df.index[-1], LiveContext())
    assert row["atr_pct"] > 0
    assert row["atr_pct"] < 0.5


def test_dist_from_52w_high_is_non_positive() -> None:
    df = _synthetic_uptrend()
    row = build_feature_row(df, df.index[-1], LiveContext())
    assert row["dist_from_52w_high"] <= 0


def test_pullback_pcts_match_manual_calculation() -> None:
    df = _synthetic_uptrend()
    sd = df.index[-1]
    row = build_feature_row(df, sd, LiveContext())
    close = float(df.at[sd, "close"])
    sma20 = float(df.at[sd, "sma_20"])
    sma50 = float(df.at[sd, "sma_50"])
    assert row["pullback_pct_20"] == pytest.approx((close - sma20) / sma20, rel=1e-9)
    assert row["pullback_pct_50"] == pytest.approx((close - sma50) / sma50, rel=1e-9)


def test_trend_features_present() -> None:
    df = _synthetic_uptrend()
    row = build_feature_row(df, df.index[-1], LiveContext())
    for k in (
        "sma_20_slope_5d",
        "sma_50_slope_10d",
        "sma_200_slope_20d",
        "adx_14",
        "dist_from_52w_low",
    ):
        assert k in row


def test_sma_slopes_are_positive_on_uptrend() -> None:
    df = _synthetic_uptrend()
    row = build_feature_row(df, df.index[-1], LiveContext())
    assert row["sma_20_slope_5d"] > 0
    assert row["sma_50_slope_10d"] > 0


def test_volume_features_present() -> None:
    df = _synthetic_uptrend()
    row = build_feature_row(df, df.index[-1], LiveContext())
    for k in ("volume_vs_20d_avg", "obv_slope_5d"):
        assert k in row
    assert row["volume_vs_20d_avg"] > 0


def test_short_history_yields_nan_for_long_lookbacks() -> None:
    """If df < 252 bars, dist_from_52w_* should use whatever's available
    (rolling.max/min with min_periods=1). Builder must not raise."""
    df = _synthetic_uptrend(n=60)
    row = build_feature_row(df, df.index[-1], LiveContext())
    # sma_200 not defined at bar 60 → slope should be NaN, not raise
    assert math.isnan(row["sma_200_slope_20d"])
