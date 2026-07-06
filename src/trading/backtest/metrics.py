"""Backtest metrics — spec §8.4.

Pure functions over an equity series (`pd.Series` indexed by date) and a
sequence of `Trade`-shaped objects. The `Trade` type is defined in
`engine.py`; we use a `Protocol` here to avoid an import cycle and to keep
the metrics module testable in isolation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from trading.backtest.engine import BacktestResult

TRADING_DAYS_PER_YEAR = 252


class TradeLike(Protocol):
    @property
    def net_pnl(self) -> float: ...
    @property
    def gross_pnl(self) -> float: ...
    @property
    def entry_price(self) -> float: ...
    @property
    def initial_stop(self) -> float: ...
    @property
    def qty(self) -> int: ...


@dataclass(frozen=True)
class MetricsBundle:
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    hit_rate: float
    profit_factor: float
    expectancy: float
    avg_r_multiple: float
    total_trades: int
    total_costs: float
    alpha_annualized: float | None
    beta: float | None


# ---------------------------------------------------------------------------
# Equity-curve metrics
# ---------------------------------------------------------------------------


def cagr(equity: pd.Series) -> float:
    """Compound annual growth rate. Returns 0.0 for <2 points or zero starting equity."""
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    start = float(equity.iloc[0])
    end = float(equity.iloc[-1])
    days = (equity.index[-1] - equity.index[0]).days
    if days <= 0:
        return 0.0
    years = days / 365.25
    if end <= 0:
        return -1.0
    return float((end / start) ** (1.0 / years) - 1.0)


def sharpe(returns: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised Sharpe with rf=0. Zero-variance returns → 0.0 (not NaN)."""
    if len(returns) == 0:
        return 0.0
    mean = float(returns.mean())
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    # Float noise on identical inputs can leave std as ~1e-18 instead of exactly 0.
    if std < 1e-12:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def gate_sharpe(equity: pd.Series) -> float | None:
    """Daily-annualised Sharpe of an equity curve — the go-live gate metric (F-061).

    The go-live bar ("≥3 months OOS Sharpe > 1.0") is defined on daily-annualised
    returns. This is the *only* function that measures it: convert `equity`
    (indexed by date, values = account value, e.g. `portfolio_snapshots.equity`)
    to daily pct-change returns and delegate to `sharpe(..., periods_per_year=
    TRADING_DAYS_PER_YEAR)` — it does not re-derive the Sharpe math.

    Unlike `sharpe()`, which returns 0.0 for zero-variance/empty input (a
    genuinely-measured flat Sharpe), this returns `None` when there isn't
    enough data to measure anything at all: fewer than 2 daily returns, or a
    return series with ~zero variance. Callers must render that as "n/a", not
    as a 0.0 that looks like a measurement.
    """
    if len(equity) < 2:
        return None
    returns = equity.pct_change().dropna()
    if len(returns) < 2:
        return None
    if float(returns.std(ddof=1)) < 1e-12:
        return None
    return sharpe(returns, periods_per_year=TRADING_DAYS_PER_YEAR)


def sortino(returns: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised Sortino — downside-stdev denominator."""
    if len(returns) == 0:
        return 0.0
    mean = float(returns.mean())
    downside = returns[returns < 0]
    if len(downside) < 2:
        return 0.0
    dstd = float(downside.std(ddof=1))
    if dstd < 1e-12:
        return 0.0
    return (mean / dstd) * math.sqrt(periods_per_year)


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough loss as a negative fraction (e.g. -0.142)."""
    if len(equity) < 2:
        return 0.0
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    return float(drawdown.min())


# ---------------------------------------------------------------------------
# Trade-list metrics
# ---------------------------------------------------------------------------


def hit_rate(trades: Sequence[TradeLike]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.net_pnl > 0)
    return wins / len(trades)


def profit_factor(trades: Sequence[TradeLike]) -> float:
    if not trades:
        return 0.0
    gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def expectancy(trades: Sequence[TradeLike]) -> float:
    if not trades:
        return 0.0
    return sum(t.net_pnl for t in trades) / len(trades)


def avg_r_multiple(trades: Sequence[TradeLike]) -> float:
    """Mean (net_pnl / initial_risk) per trade. R = qty × (entry − initial_stop)."""
    if not trades:
        return 0.0
    r_multiples: list[float] = []
    for t in trades:
        risk = t.qty * (t.entry_price - t.initial_stop)
        if risk > 0:
            r_multiples.append(t.net_pnl / risk)
    if not r_multiples:
        return 0.0
    return sum(r_multiples) / len(r_multiples)


# ---------------------------------------------------------------------------
# Benchmark-relative
# ---------------------------------------------------------------------------


def alpha_beta(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> tuple[float, float]:
    """OLS regression: strategy_r = α + β × benchmark_r. Returns (annualised α, β)."""
    aligned = pd.concat([strategy_returns, benchmark_returns], axis=1, join="inner").dropna()
    if len(aligned) < 2:
        return 0.0, 0.0
    x = aligned.iloc[:, 1].to_numpy()
    y = aligned.iloc[:, 0].to_numpy()
    var_x = float(np.var(x, ddof=1))
    if var_x == 0.0:
        return 0.0, 0.0
    cov_xy = float(np.cov(x, y, ddof=1)[0, 1])
    beta_val = cov_xy / var_x
    alpha_daily = float(np.mean(y)) - beta_val * float(np.mean(x))
    return alpha_daily * periods_per_year, beta_val


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def compute_metrics(
    result: BacktestResult,
    benchmark_returns: pd.Series | None = None,
) -> MetricsBundle:
    alpha_val: float | None = None
    beta_val: float | None = None
    if benchmark_returns is not None and len(benchmark_returns) > 1:
        alpha_val, beta_val = alpha_beta(result.daily_returns, benchmark_returns)

    return MetricsBundle(
        cagr=cagr(result.equity_curve),
        sharpe=sharpe(result.daily_returns),
        sortino=sortino(result.daily_returns),
        max_drawdown=max_drawdown(result.equity_curve),
        hit_rate=hit_rate(result.trades),
        profit_factor=profit_factor(result.trades),
        expectancy=expectancy(result.trades),
        avg_r_multiple=avg_r_multiple(result.trades),
        total_trades=len(result.trades),
        total_costs=result.total_costs,
        alpha_annualized=alpha_val,
        beta=beta_val,
    )
