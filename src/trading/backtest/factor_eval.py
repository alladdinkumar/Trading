"""Path A — offline, read-only factor evaluation.

Measures whether the cross-sectional factor composite has edge:
Information Coefficient (Spearman corr of score vs forward return) and a
factor-gated vs rules-only per-trade backtest. Forward returns reuse
``ranker_labels.realized_return`` so they resolve exactly as trades would.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading.backtest.forward_return import realized_return
from trading.backtest.metrics import sharpe as _sharpe
from trading.strategy.factors import eligible_set, factor_score
from trading.strategy.rules import MIN_HISTORY_BARS, ScanContext, evaluate_symbol


def spearman_ic(
    scores: Mapping[str, float],
    fwd: Mapping[str, float],
    *,
    min_names: int = 5,
) -> float | None:
    """Spearman rank correlation between scores and forward returns.

    Uses only symbols present in both maps. Returns ``None`` when fewer than
    ``min_names`` overlap (too thin a cross-section to be meaningful).
    """
    common = sorted(set(scores) & set(fwd))
    if len(common) < min_names:
        return None
    s = pd.Series([scores[c] for c in common])
    f = pd.Series([fwd[c] for c in common])
    ic = s.corr(f, method="spearman")
    return None if pd.isna(ic) else float(ic)


@dataclass(frozen=True)
class ICResult:
    mean_ic: float
    ic_std: float
    ic_t_stat: float
    hit_rate_positive_days: float
    n_days: int


def aggregate_ic(ic_values: Sequence[float]) -> ICResult:
    """Aggregate per-day ICs into mean, stdev, t-stat and positive-day rate."""
    n = len(ic_values)
    if n == 0:
        return ICResult(0.0, 0.0, 0.0, 0.0, 0)
    arr = np.array(ic_values, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    t_stat = mean / (std / math.sqrt(n)) if std > 1e-12 else 0.0
    hit = float((arr > 0).mean())
    return ICResult(mean, std, t_stat, hit, n)


def forward_returns(
    panel: Mapping[str, pd.DataFrame],
    as_of: pd.Timestamp,
    *,
    max_days: int = 25,
) -> dict[str, float]:
    """Realized forward return per symbol from ``as_of`` (None entries dropped)."""
    out: dict[str, float] = {}
    for sym, df in panel.items():
        r = realized_return(df, as_of, max_days=max_days)
        if r is not None:
            out[sym] = r
    return out


@dataclass(frozen=True)
class TradeMetrics:
    n: int
    sharpe: float
    profit_factor: float
    hit_rate: float
    payoff: float


def per_trade_metrics(returns: Sequence[float]) -> TradeMetrics:
    """Honest per-trade metrics over fractional realized returns."""
    n = len(returns)
    if n == 0:
        return TradeMetrics(0, 0.0, 0.0, 0.0, 0.0)
    arr = np.array(returns, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else (math.inf if avg_win > 0 else 0.0)
    return TradeMetrics(
        n=n,
        sharpe=_sharpe(pd.Series(arr), periods_per_year=12),
        profit_factor=pf,
        hit_rate=float((arr > 0).mean()),
        payoff=payoff,
    )


def information_coefficient(
    panel: Mapping[str, pd.DataFrame],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    vol_window: int = 90,
    max_days: int = 25,
    min_names: int = 5,
) -> ICResult:
    """Mean/stdev/t-stat of per-day Spearman IC of composite vs forward return."""
    all_dates = sorted({d for df in panel.values() for d in df.index if start <= d <= end})
    ics: list[float] = []
    for sd in all_dates:
        scores = factor_score(panel, sd, vol_window=vol_window)
        if not scores:
            continue
        fwd = forward_returns(panel, sd, max_days=max_days)
        ic = spearman_ic(scores, fwd, min_names=min_names)
        if ic is not None:
            ics.append(ic)
    return aggregate_ic(ics)


@dataclass(frozen=True)
class GatedComparison:
    baseline: TradeMetrics
    gated: TradeMetrics


def factor_gated_metrics(
    panel: Mapping[str, pd.DataFrame],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    top_quantile: float = 0.30,
    vol_window: int = 90,
    max_days: int = 25,
) -> GatedComparison:
    """Per-trade metrics for every rules-passing candidate (baseline) vs only
    those also in that day's factor eligible set (gated), over [start, end]."""
    baseline: list[float] = []
    gated: list[float] = []
    # Iterate the union of trading dates within the window.
    all_dates = sorted({d for df in panel.values() for d in df.index if start <= d <= end})
    for sd in all_dates:
        scores = factor_score(panel, sd, vol_window=vol_window)
        eligible = eligible_set(scores, top_quantile=top_quantile)
        for sym, df in panel.items():
            if sd not in df.index:
                continue
            sub = df.loc[:sd]
            if len(sub) < MIN_HISTORY_BARS:
                continue
            if not evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date())).all_passed:
                continue
            r = realized_return(df, sd, max_days=max_days)
            if r is None:
                continue
            baseline.append(r)
            if sym in eligible:
                gated.append(r)
    return GatedComparison(baseline=per_trade_metrics(baseline), gated=per_trade_metrics(gated))
