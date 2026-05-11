"""Technical indicator wrappers over the `ta` library.

Each function returns a `pd.Series` (or tuple of Series). No mutation. The
single exception is `add_indicators(df)`, which returns a copy enriched with
the default indicator suite that Phase 5 (rule scanner) consumes.
"""

from __future__ import annotations

from typing import cast

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, ADXIndicator, EMAIndicator, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import OnBalanceVolumeIndicator, VolumeWeightedAveragePrice

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
SMA_DEFAULT_PERIODS = (20, 50, 200)
EMA_DEFAULT_PERIODS = (20, 50)
BB_PERIOD = 20
BB_STDDEV = 2
ATR_PERIOD = 14
ADX_PERIOD = 14
VWAP_PERIOD = 14


# ---------------------------------------------------------------------------
# Single-series indicators
# ---------------------------------------------------------------------------


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Relative Strength Index (Wilder). Range: 0..100."""
    return cast(pd.Series, RSIIndicator(close=close, window=period, fillna=False).rsi())


def sma(close: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return cast(pd.Series, SMAIndicator(close=close, window=period, fillna=False).sma_indicator())


def ema(close: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return cast(pd.Series, EMAIndicator(close=close, window=period, fillna=False).ema_indicator())


def macd(
    close: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (MACD line, signal line, histogram)."""
    ind = MACD(
        close=close,
        window_slow=slow,
        window_fast=fast,
        window_sign=signal,
        fillna=False,
    )
    return (
        cast(pd.Series, ind.macd()),
        cast(pd.Series, ind.macd_signal()),
        cast(pd.Series, ind.macd_diff()),
    )


def bollinger_bands(
    close: pd.Series,
    period: int = BB_PERIOD,
    n_std: float = BB_STDDEV,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (upper, middle, lower) Bollinger Bands."""
    ind = BollingerBands(close=close, window=period, window_dev=n_std, fillna=False)
    return (
        cast(pd.Series, ind.bollinger_hband()),
        cast(pd.Series, ind.bollinger_mavg()),
        cast(pd.Series, ind.bollinger_lband()),
    )


def returns(close: pd.Series, periods: int = 1) -> pd.Series:
    """Simple percent change over `periods` bars."""
    return close.pct_change(periods=periods)


# ---------------------------------------------------------------------------
# Multi-series (OHLC / OHLCV) indicators
# ---------------------------------------------------------------------------


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = ATR_PERIOD,
) -> pd.Series:
    """Average True Range (Wilder)."""
    return cast(
        pd.Series,
        AverageTrueRange(
            high=high, low=low, close=close, window=period, fillna=False
        ).average_true_range(),
    )


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = ADX_PERIOD,
) -> pd.Series:
    """Average Directional Index. Range: 0..100."""
    return cast(
        pd.Series,
        ADXIndicator(high=high, low=low, close=close, window=period, fillna=False).adx(),
    )


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = VWAP_PERIOD,
) -> pd.Series:
    """Rolling Volume Weighted Average Price (period-windowed, not daily-reset)."""
    return cast(
        pd.Series,
        VolumeWeightedAveragePrice(
            high=high, low=low, close=close, volume=volume, window=period, fillna=False
        ).volume_weighted_average_price(),
    )


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume — cumulative volume signed by close direction."""
    return cast(
        pd.Series,
        OnBalanceVolumeIndicator(close=close, volume=volume, fillna=False).on_balance_volume(),
    )


# ---------------------------------------------------------------------------
# add_indicators — bulk decorator for an OHLCV DataFrame
# ---------------------------------------------------------------------------


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of `df` enriched with the default indicator suite.

    Expects `df` to have columns: open, high, low, close, volume. Adds:
        rsi_14, macd, macd_signal, macd_hist,
        sma_20, sma_50, sma_200, ema_20, ema_50,
        bb_upper, bb_middle, bb_lower,
        atr_14, adx_14, vwap_14, obv, returns_1d
    """
    out = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    out["rsi_14"] = rsi(close, period=RSI_PERIOD)
    macd_line, macd_signal, macd_hist = macd(close)
    out["macd"] = macd_line
    out["macd_signal"] = macd_signal
    out["macd_hist"] = macd_hist
    for p in SMA_DEFAULT_PERIODS:
        out[f"sma_{p}"] = sma(close, period=p)
    for p in EMA_DEFAULT_PERIODS:
        out[f"ema_{p}"] = ema(close, period=p)
    bb_upper, bb_middle, bb_lower = bollinger_bands(close)
    out["bb_upper"] = bb_upper
    out["bb_middle"] = bb_middle
    out["bb_lower"] = bb_lower
    out["atr_14"] = atr(high, low, close, period=ATR_PERIOD)
    out["adx_14"] = adx(high, low, close, period=ADX_PERIOD)
    out["vwap_14"] = vwap(high, low, close, volume, period=VWAP_PERIOD)
    out["obv"] = obv(close, volume)
    out["returns_1d"] = returns(close, periods=1)
    return out
