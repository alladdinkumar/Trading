import numpy as np
import pandas as pd

from trading.features.technicals import add_indicators
from trading.strategy.ranker_labels import label_candidate


def _trend_df(close_path: list[float], atr: float = 2.0) -> pd.DataFrame:
    """Build a deterministic OHLCV DataFrame from a list of closes.

    Each bar has high = close + atr/2, low = close - atr/2, open = prev close.
    First bar's open = close - 0.5. Volume is a constant. add_indicators is
    called so atr_14 is populated (we still need the first 14 bars for warm-up).
    """
    n = len(close_path)
    dates = pd.bdate_range("2024-01-02", periods=n)
    closes = np.array(close_path, dtype=float)
    opens = np.concatenate([[closes[0] - 0.5], closes[:-1]])
    highs = closes + atr / 2
    lows = closes - atr / 2
    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.full(n, 100_000, dtype=int),
        },
        index=dates,
    )
    return add_indicators(df)


def test_label_returns_1_when_target_hits_in_forward_window() -> None:
    """Flat warmup + signal_date close=100, then a forward window that
    rallies past +20% (target) before any drawdown to the 1.5xATR stop."""
    warmup = [100.0] * 50
    forward = [101, 103, 108, 115, 125, 130, 122, 121, 120, 119] + [120] * 20
    df = _trend_df(warmup + forward)
    signal_date = df.index[49]   # last bar of warmup
    label = label_candidate(df, signal_date)
    assert label == 1


def test_label_returns_0_when_stop_hits_before_target() -> None:
    """Forward window opens above signal close but immediately drops through
    the ATR-stop (1.5 x atr_14 below entry). Net P&L is strongly negative."""
    warmup = [100.0] * 50
    forward = [100, 95, 90, 88, 85, 83, 82, 80, 80, 80] + [80] * 20
    df = _trend_df(warmup + forward, atr=2.0)
    signal_date = df.index[49]
    label = label_candidate(df, signal_date)
    assert label == 0


def test_label_returns_none_if_forward_window_too_short() -> None:
    """Less than max_days+1 bars after signal_date → cannot resolve."""
    df = _trend_df([100.0] * 60)
    signal_date = df.index[-1]  # nothing after
    label = label_candidate(df, signal_date)
    assert label is None


def test_label_time_exit_negative_returns_0() -> None:
    """25 forward bars, mostly flat → time stop closes at small loss with costs."""
    warmup = [100.0] * 50
    forward = [99.5] * 30
    df = _trend_df(warmup + forward, atr=2.0)
    signal_date = df.index[49]
    label = label_candidate(df, signal_date)
    assert label == 0


def test_label_time_exit_positive_returns_1() -> None:
    """25 forward bars, gentle drift up, never hits target or stop."""
    warmup = [100.0] * 50
    forward = [100 + 0.3 * i for i in range(1, 31)]  # ends ~109, below +20% target
    df = _trend_df(warmup + forward, atr=2.0)
    signal_date = df.index[49]
    label = label_candidate(df, signal_date)
    assert label == 1
