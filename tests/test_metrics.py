"""Phase 7.4 — metrics tests.

Pin each metric to a hand-computed value on a tiny series. Catches sign and
annualisation errors immediately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from trading.backtest.metrics import (
    alpha_beta,
    avg_r_multiple,
    cagr,
    expectancy,
    gate_sharpe,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
)


@dataclass(frozen=True)
class _FakeTrade:
    """Lightweight stand-in matching the TradeLike Protocol — keeps these tests
    decoupled from engine.py."""

    net_pnl: float
    gross_pnl: float = 0.0
    entry_price: float = 100.0
    initial_stop: float = 90.0
    qty: int = 10


# ---------------------------------------------------------------------------
# CAGR
# ---------------------------------------------------------------------------


def test_cagr_known_doubling() -> None:
    """Equity doubles over 365 days → CAGR ≈ 1.0."""
    dates = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    equity = pd.Series(np.linspace(100_000, 200_000, len(dates)), index=dates)
    assert cagr(equity) == pytest.approx(1.0, abs=0.02)


def test_cagr_short_series_returns_zero() -> None:
    s = pd.Series([100_000], index=pd.to_datetime(["2024-01-01"]))
    assert cagr(s) == 0.0


def test_cagr_flat_curve_is_zero() -> None:
    dates = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    equity = pd.Series([100_000] * len(dates), index=dates)
    assert cagr(equity) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Sharpe / Sortino
# ---------------------------------------------------------------------------


def test_sharpe_constant_return_is_zero() -> None:
    """Zero-variance returns must yield 0.0, not NaN/inf."""
    returns = pd.Series([0.01] * 100)
    assert sharpe(returns) == 0.0


def test_sharpe_known_value() -> None:
    """Hand-computed: mean=0.001, std=0.01, periods=252.
    Sharpe = 0.001/0.01 × √252 ≈ 1.587"""
    returns = pd.Series([0.011, -0.009, 0.011, -0.009] * 25)
    s = sharpe(returns)
    expected = 0.001 / returns.std(ddof=1) * math.sqrt(252)
    assert s == pytest.approx(expected)


def test_sortino_only_penalizes_downside() -> None:
    """Sortino > Sharpe when upside dispersion exceeds downside dispersion."""
    # Big positive spikes (0.05, 0.04) widen the full stdev; the two negative
    # bars are tightly clustered, so the downside stdev is smaller.
    returns = pd.Series([0.05, 0.04, 0.02, -0.010, -0.005])
    assert sortino(returns) > sharpe(returns)


def test_sortino_no_losses_returns_zero() -> None:
    """No negative bars → can't compute downside stdev with ddof=1; return 0."""
    returns = pd.Series([0.01, 0.02, 0.005])
    assert sortino(returns) == 0.0


# ---------------------------------------------------------------------------
# Gate Sharpe (F-061) — daily-annualised Sharpe of an equity curve
# ---------------------------------------------------------------------------


def test_gate_sharpe_known_value() -> None:
    """Equity curve → daily pct-change returns → mean/std × √252 via `sharpe()`."""
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    equity = pd.Series([100_000.0, 101_000.0, 100_500.0, 102_000.0, 103_000.0], index=dates)
    returns = equity.pct_change().dropna()
    expected = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252))
    assert gate_sharpe(equity) == pytest.approx(expected)


def test_gate_sharpe_matches_sharpe_on_same_returns() -> None:
    """Must delegate to `sharpe(..., periods_per_year=252)`, not re-derive it."""
    dates = pd.date_range("2024-01-01", periods=6, freq="D")
    equity = pd.Series([50_000.0, 50_500.0, 50_100.0, 51_000.0, 50_800.0, 51_500.0], index=dates)
    returns = equity.pct_change().dropna()
    assert gate_sharpe(equity) == pytest.approx(sharpe(returns, periods_per_year=252))


def test_gate_sharpe_empty_series_is_none() -> None:
    assert gate_sharpe(pd.Series(dtype=float)) is None


def test_gate_sharpe_single_point_is_none() -> None:
    dates = pd.date_range("2024-01-01", periods=1, freq="D")
    assert gate_sharpe(pd.Series([100_000.0], index=dates)) is None


def test_gate_sharpe_two_points_one_return_is_none() -> None:
    """One daily return can't produce a stdev — n/a, not a crash or a 0.0."""
    dates = pd.date_range("2024-01-01", periods=2, freq="D")
    equity = pd.Series([100_000.0, 101_000.0], index=dates)
    assert gate_sharpe(equity) is None


def test_gate_sharpe_zero_variance_is_none_not_zero() -> None:
    """Flat equity curve → constant (zero) daily returns → n/a, never a
    measured-looking 0.0 (F-061 guard rail)."""
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    equity = pd.Series([100_000.0] * 5, index=dates)
    assert gate_sharpe(equity) is None


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_monotonic_curve_is_zero() -> None:
    dates = pd.date_range("2024-01-01", periods=10, freq="D")
    equity = pd.Series(range(100, 110), index=dates)
    assert max_drawdown(equity) == pytest.approx(0.0)


def test_max_drawdown_known_curve() -> None:
    """100 → 120 → 80 → −33.3% (peak 120, trough 80)."""
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    equity = pd.Series([100.0, 120.0, 80.0], index=dates)
    assert max_drawdown(equity) == pytest.approx(-1 / 3)


def test_max_drawdown_recovers_then_makes_new_high() -> None:
    """Peak resets after recovery."""
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    equity = pd.Series([100.0, 80.0, 120.0, 100.0, 140.0], index=dates)
    # First DD: -0.20; second: -100/120 = -0.167. Max = -0.20.
    assert max_drawdown(equity) == pytest.approx(-0.2)


# ---------------------------------------------------------------------------
# Trade-list metrics
# ---------------------------------------------------------------------------


def test_hit_rate_empty_zero() -> None:
    assert hit_rate([]) == 0.0


def test_hit_rate_three_of_four() -> None:
    trades = [_FakeTrade(100), _FakeTrade(50), _FakeTrade(-30), _FakeTrade(10)]
    assert hit_rate(trades) == 0.75


def test_profit_factor_all_wins_returns_inf() -> None:
    trades = [_FakeTrade(100), _FakeTrade(50)]
    assert profit_factor(trades) == float("inf")


def test_profit_factor_known() -> None:
    """Profits 150, losses 50 → PF=3.0."""
    trades = [_FakeTrade(100), _FakeTrade(50), _FakeTrade(-30), _FakeTrade(-20)]
    assert profit_factor(trades) == pytest.approx(3.0)


def test_expectancy_is_mean_net_pnl() -> None:
    trades = [_FakeTrade(100), _FakeTrade(-50)]
    assert expectancy(trades) == 25.0


def test_avg_r_multiple_known() -> None:
    """qty=10, entry=100, stop=90 → R=100. net_pnl 200 → 2R; net_pnl −100 → −1R."""
    trades = [_FakeTrade(net_pnl=200), _FakeTrade(net_pnl=-100)]
    assert avg_r_multiple(trades) == pytest.approx(0.5)


def test_avg_r_multiple_skips_zero_risk_trades() -> None:
    """Edge case: entry==stop trade shouldn't blow up the average."""
    trades = [_FakeTrade(net_pnl=100, entry_price=100, initial_stop=100), _FakeTrade(net_pnl=200)]
    # Only the valid-risk trade contributes: 200 / (10×10) = 2.0
    assert avg_r_multiple(trades) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Alpha / beta
# ---------------------------------------------------------------------------


def test_alpha_beta_self_is_one_and_zero() -> None:
    """Strategy = benchmark → β=1.0, α=0.0."""
    returns = pd.Series([0.01, -0.005, 0.02, -0.01, 0.015])
    alpha, beta = alpha_beta(returns, returns.copy())
    assert beta == pytest.approx(1.0)
    assert alpha == pytest.approx(0.0)


def test_alpha_beta_uncorrelated_returns_zero_beta() -> None:
    """Constant strategy returns vs varying benchmark → β ≈ 0."""
    strategy = pd.Series([0.001] * 10)
    benchmark = pd.Series(np.linspace(-0.02, 0.02, 10))
    _alpha, beta = alpha_beta(strategy, benchmark)
    assert beta == pytest.approx(0.0, abs=1e-9)
