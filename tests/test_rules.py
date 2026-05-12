"""Tests for trading.strategy.rules — Layer A filters + orchestrator."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.config import get_paths
from trading.features.technicals import add_indicators
from trading.store.ohlcv import write_ohlcv
from trading.strategy.rules import (
    Candidate,
    RuleResult,
    ScanContext,
    evaluate_symbol,
    passes_liquidity,
    passes_no_critical_event,
    passes_no_recent_breakdown,
    passes_not_fno_banned,
    passes_not_t2t,
    passes_pullback,
    passes_regime,
    passes_rsi_band,
    passes_uptrend,
    passes_volume_exhaustion,
    passing,
    scan,
)

# ---------------------------------------------------------------------------
# Helpers — synthetic OHLCV that we can shape to trip / pass any rule
# ---------------------------------------------------------------------------


def _df(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    volumes: list[int] | None = None,
) -> pd.DataFrame:
    n = len(closes)
    closes_arr = np.asarray(closes, dtype=float)
    highs_arr = closes_arr + 0.5 if highs is None else np.asarray(highs, dtype=float)
    lows_arr = closes_arr - 0.5 if lows is None else np.asarray(lows, dtype=float)
    opens_arr = closes_arr.copy() if opens is None else np.asarray(opens, dtype=float)
    volumes_arr = (
        np.full(n, 1_000_000, dtype=int) if volumes is None else np.asarray(volumes, dtype=int)
    )
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    idx.name = "date"
    df = pd.DataFrame(
        {
            "open": opens_arr,
            "high": highs_arr,
            "low": lows_arr,
            "close": closes_arr,
            "volume": volumes_arr,
        },
        index=idx,
    )
    return add_indicators(df)


def _uptrend_df(n: int = 250) -> pd.DataFrame:
    """Steadily rising closes — passes uptrend filter."""
    return _df([100.0 + 0.5 * i for i in range(n)])


def _downtrend_df(n: int = 250) -> pd.DataFrame:
    return _df([250.0 - 0.5 * i for i in range(n)])


# ---------------------------------------------------------------------------
# Uptrend
# ---------------------------------------------------------------------------


def test_uptrend_passes_on_rising_series() -> None:
    df = _uptrend_df()
    r = passes_uptrend(df)
    assert isinstance(r, RuleResult)
    assert r.name == "uptrend"
    assert r.passed


def test_uptrend_fails_on_downtrend() -> None:
    df = _downtrend_df()
    r = passes_uptrend(df)
    assert not r.passed


def test_uptrend_fails_when_history_insufficient() -> None:
    # 100 bars: not enough for sma_200
    df = _df([100.0 + 0.1 * i for i in range(100)])
    r = passes_uptrend(df)
    assert not r.passed
    assert "history" in r.reason.lower()


# ---------------------------------------------------------------------------
# Pullback
# ---------------------------------------------------------------------------


def test_pullback_passes_when_close_near_sma() -> None:
    df = _uptrend_df()
    r = passes_pullback(df, max_pct=0.10)  # generous band for the synthetic data
    assert r.passed


def test_pullback_fails_when_far_from_sma() -> None:
    df = _uptrend_df()
    # Push the last close far above the 20-SMA
    df.loc[df.index[-1], "close"] = df["sma_20"].iloc[-1] * 1.5
    r = passes_pullback(df, max_pct=0.03)
    assert not r.passed


# ---------------------------------------------------------------------------
# RSI band
# ---------------------------------------------------------------------------


def test_rsi_band_passes_in_range() -> None:
    df = _uptrend_df()
    # Synthesise RSI in [30, 45] by overwriting (we trust the wrapper from Phase 4)
    df.loc[df.index[-1], "rsi_14"] = 35.0
    r = passes_rsi_band(df)
    assert r.passed


def test_rsi_band_fails_above_range() -> None:
    df = _uptrend_df()
    df.loc[df.index[-1], "rsi_14"] = 70.0
    r = passes_rsi_band(df)
    assert not r.passed


def test_rsi_band_fails_when_nan() -> None:
    df = _uptrend_df()
    df.loc[df.index[-1], "rsi_14"] = float("nan")
    r = passes_rsi_band(df)
    assert not r.passed


# ---------------------------------------------------------------------------
# Volume exhaustion
# ---------------------------------------------------------------------------


def test_volume_exhaustion_passes_when_not_selling_day() -> None:
    df = _uptrend_df()
    # Force close > open on the last bar
    df.loc[df.index[-1], "open"] = 100.0
    df.loc[df.index[-1], "close"] = 105.0
    r = passes_volume_exhaustion(df)
    assert r.passed
    assert "not a selling day" in r.reason


def test_volume_exhaustion_passes_when_selling_day_below_avg() -> None:
    n = 30
    closes = [100.0] * n
    opens = closes.copy()
    opens[-1] = 105.0  # selling day: close < open
    closes[-1] = 100.0
    volumes = [2_000_000] * (n - 1) + [500_000]  # last is BELOW avg
    df = _df(closes, opens=opens, volumes=volumes)
    r = passes_volume_exhaustion(df)
    assert r.passed


def test_volume_exhaustion_fails_when_selling_day_above_avg() -> None:
    n = 30
    closes = [100.0] * n
    opens = closes.copy()
    opens[-1] = 105.0  # selling day
    closes[-1] = 100.0
    volumes = [1_000_000] * (n - 1) + [5_000_000]  # last is ABOVE avg
    df = _df(closes, opens=opens, volumes=volumes)
    r = passes_volume_exhaustion(df)
    assert not r.passed


# ---------------------------------------------------------------------------
# Liquidity (₹10cr threshold)
# ---------------------------------------------------------------------------


def test_liquidity_passes_above_threshold() -> None:
    # close × volume = 100 × 1,500,000 = ₹15cr per day → above 10cr
    df = _df([100.0] * 30, volumes=[1_500_000] * 30)
    r = passes_liquidity(df)
    assert r.passed


def test_liquidity_fails_below_threshold() -> None:
    # close × volume = 100 × 50,000 = ₹0.5cr per day → below 10cr
    df = _df([100.0] * 30, volumes=[50_000] * 30)
    r = passes_liquidity(df)
    assert not r.passed


# ---------------------------------------------------------------------------
# No recent breakdown
# ---------------------------------------------------------------------------


def test_no_recent_breakdown_passes_mild_dip() -> None:
    closes = [100.0] * 40
    closes[-1] = 95.0  # 5% dip — well within 15% threshold
    df = _df(closes)
    r = passes_no_recent_breakdown(df)
    assert r.passed


def test_no_recent_breakdown_fails_large_drop() -> None:
    closes = [100.0] * 39 + [70.0]  # 30% drop
    df = _df(closes)
    r = passes_no_recent_breakdown(df)
    assert not r.passed


# ---------------------------------------------------------------------------
# Regime (context-based)
# ---------------------------------------------------------------------------


def test_regime_passes_when_context_empty() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11))
    r = passes_regime(ctx)
    assert r.passed


def test_regime_fails_on_high_vix() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11), india_vix=28.0)
    r = passes_regime(ctx)
    assert not r.passed
    assert "VIX" in r.reason


def test_regime_fails_on_index_drawdown() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11), nifty200_drawdown_5d_pct=-7.0)
    r = passes_regime(ctx)
    assert not r.passed


def test_regime_passes_when_below_thresholds() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11), india_vix=18.0, nifty200_drawdown_5d_pct=-1.0)
    r = passes_regime(ctx)
    assert r.passed


# ---------------------------------------------------------------------------
# F&O ban / T2T / critical event (symbol-based, context-driven)
# ---------------------------------------------------------------------------


def test_not_fno_banned_passes_default() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11))
    assert passes_not_fno_banned("RVNL", ctx).passed


def test_not_fno_banned_fails_when_listed() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11), fno_ban_symbols=frozenset({"RVNL"}))
    assert not passes_not_fno_banned("RVNL", ctx).passed


def test_not_t2t_fails_when_listed() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11), t2t_symbols=frozenset({"BADCO"}))
    assert not passes_not_t2t("BADCO", ctx).passed


def test_no_critical_event_fails_when_listed() -> None:
    ctx = ScanContext(scan_date=date(2026, 5, 11), critical_event_symbols=frozenset({"ABC"}))
    assert not passes_no_critical_event("ABC", ctx).passed


# ---------------------------------------------------------------------------
# evaluate_symbol + scan
# ---------------------------------------------------------------------------


def test_evaluate_symbol_returns_full_candidate() -> None:
    df = _uptrend_df()
    ctx = ScanContext(scan_date=date(2024, 12, 1))
    cand = evaluate_symbol("RVNL", df, ctx)
    assert isinstance(cand, Candidate)
    assert cand.symbol == "RVNL"
    assert len(cand.rules) == 10  # 6 indicator + 4 context rules
    # The rule names are unique and known
    names = {r.name for r in cand.rules}
    assert names == {
        "uptrend",
        "pullback",
        "rsi_band",
        "volume_exhaustion",
        "liquidity",
        "no_recent_breakdown",
        "regime",
        "not_fno_banned",
        "not_t2t",
        "no_critical_event",
    }


def test_candidate_all_passed_property() -> None:
    df = _uptrend_df()
    ctx = ScanContext(scan_date=date(2024, 12, 1))
    cand = evaluate_symbol("RVNL", df, ctx)
    # An uptrend candidate likely won't pass rsi_band (RSI saturates ~100)
    assert not cand.all_passed  # some rule will fail


def test_passing_filter_excludes_failures() -> None:
    pass_cand = Candidate(
        symbol="A",
        scan_date=date(2024, 1, 1),
        close=100.0,
        rsi_14=40.0,
        sma_20=99.0,
        sma_50=98.0,
        sma_200=95.0,
        atr_14=2.0,
        rules=(RuleResult("uptrend", True), RuleResult("rsi_band", True)),
    )
    fail_cand = Candidate(
        symbol="B",
        scan_date=date(2024, 1, 1),
        close=100.0,
        rsi_14=80.0,
        sma_20=99.0,
        sma_50=98.0,
        sma_200=95.0,
        atr_14=2.0,
        rules=(RuleResult("uptrend", True), RuleResult("rsi_band", False, "RSI=80")),
    )
    assert passing([pass_cand, fail_cand]) == [pass_cand]


def test_scan_skips_symbols_with_insufficient_history(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    # Symbol A has 250 bars (enough), Symbol B has 50 bars (skip)
    df_long = pd.DataFrame(
        {
            "open": [100.0] * 250,
            "high": [101.0] * 250,
            "low": [99.0] * 250,
            "close": [100.0] * 250,
            "volume": [1_000_000] * 250,
        },
        index=pd.date_range("2024-01-01", periods=250, freq="B", name="date"),
    )
    df_short = df_long.iloc[:50].copy()
    df_short.index = pd.date_range("2024-01-01", periods=50, freq="B", name="date")
    write_ohlcv(df_long, "ALONG", paths)
    write_ohlcv(df_short, "BSHORT", paths)

    cands = scan(paths, scan_date=date(2024, 12, 31), symbols=["ALONG", "BSHORT"])
    symbols = {c.symbol for c in cands}
    assert "ALONG" in symbols
    assert "BSHORT" not in symbols


def test_scan_with_explicit_symbols(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = pd.DataFrame(
        {
            "open": [100.0] * 250,
            "high": [101.0] * 250,
            "low": [99.0] * 250,
            "close": [100.0] * 250,
            "volume": [1_000_000] * 250,
        },
        index=pd.date_range("2024-01-01", periods=250, freq="B", name="date"),
    )
    write_ohlcv(df, "FOO", paths)
    cands = scan(paths, scan_date=date(2024, 12, 31), symbols=["FOO"])
    assert len(cands) == 1
    assert cands[0].symbol == "FOO"


def test_scan_returns_empty_when_no_parquet(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    cands = scan(paths, scan_date=date(2024, 1, 1), symbols=["NOPE"])
    assert cands == []


@pytest.fixture
def scan_fixture_paths(tmp_path: Path) -> Path:
    """Write a single 250-bar parquet for end-to-end scan tests."""
    paths = get_paths(root=tmp_path)
    df = pd.DataFrame(
        {
            "open": [100.0 + 0.1 * i for i in range(250)],
            "high": [100.5 + 0.1 * i for i in range(250)],
            "low": [99.5 + 0.1 * i for i in range(250)],
            "close": [100.0 + 0.1 * i for i in range(250)],
            "volume": [2_000_000] * 250,
        },
        index=pd.date_range("2024-01-01", periods=250, freq="B", name="date"),
    )
    write_ohlcv(df, "TEST", paths)
    return tmp_path
