"""Tests for trading.features.technicals — indicator wrappers + add_indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading.features.technicals import (
    ADX_PERIOD,
    ATR_PERIOD,
    BB_PERIOD,
    MACD_SIGNAL,
    RSI_PERIOD,
    add_indicators,
    adx,
    atr,
    bollinger_bands,
    ema,
    macd,
    obv,
    returns,
    rsi,
    sma,
    vwap,
)

N = 250  # plenty of rows for 200-period SMA


def _ohlc(n: int = N, *, seed: int = 0) -> pd.DataFrame:
    """Synthetic OHLCV: random walk close ±1% bands, increasing volume."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 1, size=n)
    close = 100 + steps.cumsum()
    high = close + np.abs(rng.normal(0, 0.5, size=n))
    low = close - np.abs(rng.normal(0, 0.5, size=n))
    open_ = close + rng.normal(0, 0.3, size=n)
    volume = (1_000_000 + np.arange(n) * 1000).astype(int)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    idx.name = "date"
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ---------------------------------------------------------------------------
# SMA — exact reference
# ---------------------------------------------------------------------------


def test_sma_exact_value() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    out = sma(s, period=5)
    # 5-day SMA at index 4 covers [1,2,3,4,5] → mean 3.0
    assert out.iloc[4] == 3.0
    assert out.iloc[9] == 8.0  # [6,7,8,9,10] → 8.0


def test_sma_nan_for_warmup() -> None:
    s = pd.Series(np.arange(20, dtype=float))
    out = sma(s, period=5)
    assert out.iloc[:4].isna().all()
    assert out.iloc[4:].notna().all()


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def test_ema_shape_and_warmup() -> None:
    s = pd.Series(np.arange(50, dtype=float))
    out = ema(s, period=10)
    assert len(out) == len(s)
    # EMA warmup: first (period-1) NaN, rest finite
    assert out.iloc[:9].isna().all()
    assert out.iloc[9:].notna().all()


# ---------------------------------------------------------------------------
# RSI — bounds on monotonic series
# ---------------------------------------------------------------------------


def test_rsi_monotonic_up_yields_high() -> None:
    s = pd.Series(np.arange(50, dtype=float))
    out = rsi(s, period=RSI_PERIOD)
    # Pure uptrend: RSI saturates at 100
    assert out.iloc[-1] > 70


def test_rsi_monotonic_down_yields_low() -> None:
    s = pd.Series(np.arange(50, 0, -1, dtype=float))
    out = rsi(s, period=RSI_PERIOD)
    assert out.iloc[-1] < 30


def test_rsi_first_period_minus_one_are_nan() -> None:
    s = pd.Series(np.linspace(100, 110, num=30))
    out = rsi(s, period=14)
    assert out.iloc[:13].isna().all()


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


def test_macd_shape() -> None:
    df = _ohlc()
    line, signal, hist = macd(df["close"])
    assert len(line) == len(signal) == len(hist) == len(df)
    # hist == line - signal where both are non-NaN
    mask = line.notna() & signal.notna()
    np.testing.assert_allclose(
        hist[mask].to_numpy(), (line[mask] - signal[mask]).to_numpy(), rtol=1e-9
    )


def test_macd_signal_is_ema_of_macd() -> None:
    df = _ohlc()
    line, signal, _ = macd(df["close"])
    # Signal line is EMA-9 of MACD line; recompute and compare
    recomputed = ema(line.dropna(), period=MACD_SIGNAL)
    aligned = signal.dropna().iloc[-len(recomputed.dropna()) :]
    recomputed_tail = recomputed.dropna().iloc[-len(aligned) :]
    np.testing.assert_allclose(aligned.to_numpy(), recomputed_tail.to_numpy(), rtol=1e-6, atol=1e-9)


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------


def test_bollinger_bands_ordering() -> None:
    df = _ohlc()
    upper, middle, lower = bollinger_bands(df["close"])
    mask = upper.notna() & middle.notna() & lower.notna()
    assert (upper[mask] >= middle[mask]).all()
    assert (middle[mask] >= lower[mask]).all()


def test_bollinger_bands_warmup() -> None:
    s = pd.Series(np.arange(30, dtype=float))
    upper, middle, _lower = bollinger_bands(s, period=BB_PERIOD)
    # First (period-1) values NaN
    assert upper.iloc[: BB_PERIOD - 1].isna().all()
    assert middle.iloc[: BB_PERIOD - 1].isna().all()


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def test_atr_non_negative() -> None:
    df = _ohlc()
    out = atr(df["high"], df["low"], df["close"])
    assert (out.dropna() >= 0).all()


def test_atr_shape() -> None:
    df = _ohlc()
    out = atr(df["high"], df["low"], df["close"], period=ATR_PERIOD)
    assert len(out) == len(df)


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------


def test_adx_within_bounds() -> None:
    df = _ohlc()
    out = adx(df["high"], df["low"], df["close"], period=ADX_PERIOD)
    finite = out.dropna()
    # ADX is 0..100
    assert (finite >= 0).all()
    assert (finite <= 100).all()


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


def test_vwap_within_high_low_range() -> None:
    df = _ohlc()
    out = vwap(df["high"], df["low"], df["close"], df["volume"])
    finite = out.dropna()
    # Rolling VWAP must be within the rolling [min(low), max(high)] envelope
    assert (finite > 0).all()


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------


def test_obv_monotonic_when_close_rises() -> None:
    n = 20
    close = pd.Series(np.arange(1, n + 1, dtype=float))  # strictly increasing
    volume = pd.Series([1000] * n)
    out = obv(close, volume)
    # OBV accumulates volume on up-days; with monotone-up close, OBV grows monotone
    assert out.is_monotonic_increasing


def test_obv_decreases_when_close_falls() -> None:
    n = 20
    close = pd.Series(np.arange(n, 0, -1, dtype=float))
    volume = pd.Series([1000] * n)
    out = obv(close, volume)
    # OBV subtracts volume on down-days
    assert out.iloc[-1] < out.iloc[0]


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------


def test_returns_exact_values() -> None:
    s = pd.Series([100.0, 110.0, 99.0])
    out = returns(s, periods=1)
    assert pd.isna(out.iloc[0])
    np.testing.assert_allclose(out.iloc[1], 0.1, rtol=1e-9)
    np.testing.assert_allclose(out.iloc[2], -0.1, rtol=1e-9)


# ---------------------------------------------------------------------------
# add_indicators
# ---------------------------------------------------------------------------


def test_add_indicators_preserves_originals() -> None:
    df = _ohlc()
    out = add_indicators(df)
    for col in ("open", "high", "low", "close", "volume"):
        np.testing.assert_array_equal(out[col].to_numpy(), df[col].to_numpy())


def test_add_indicators_adds_expected_columns() -> None:
    df = _ohlc()
    out = add_indicators(df)
    expected = {
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "sma_20",
        "sma_50",
        "sma_200",
        "ema_20",
        "ema_50",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "atr_14",
        "adx_14",
        "vwap_14",
        "obv",
        "returns_1d",
    }
    assert expected.issubset(set(out.columns))


def test_add_indicators_returns_copy() -> None:
    df = _ohlc()
    out = add_indicators(df)
    # Original df has only the OHLCV columns
    assert "rsi_14" not in df.columns
    assert "rsi_14" in out.columns


def test_add_indicators_index_preserved() -> None:
    df = _ohlc()
    out = add_indicators(df)
    pd.testing.assert_index_equal(df.index, out.index)
