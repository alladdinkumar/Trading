"""Walk-forward harness — spec §8.3.

Rolling 3y train / 6mo test / 3mo step. For Phase 7's rules-only baseline
the `train_*` window isn't consumed (no model to fit); the structure is in
place for Phase 16 (LightGBM ranker), which will refit on each train slice.

`run_walkforward` runs the engine on each out-of-sample test window with
the same initial capital and concatenates the per-fold trades. The equity
curve is stitched fold-by-fold (each fold restarts at `initial_capital`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pandas as pd
from dateutil.relativedelta import relativedelta

from trading.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    SignalProvider,
    Trade,
    run_backtest,
)
from trading.strategy.rules import ScanContext


@dataclass(frozen=True)
class WalkForwardConfig:
    train_years: float = 3.0
    test_months: float = 6.0
    step_months: float = 3.0


@dataclass(frozen=True)
class Window:
    train_start: pd.Timestamp
    train_end: pd.Timestamp  # exclusive of test_start
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def windows(start: pd.Timestamp, end: pd.Timestamp, cfg: WalkForwardConfig) -> list[Window]:
    """Enumerate (train, test) windows from `start` to `end`.

    First window's train period is [start, start + train_years), test period
    is [train_end, train_end + test_months). Each subsequent window advances
    by `step_months`. Last window must have test_end ≤ end; partial folds
    at the right edge are dropped.
    """
    out: list[Window] = []
    train_delta = relativedelta(months=round(cfg.train_years * 12))
    test_delta = relativedelta(months=round(cfg.test_months))
    step_delta = relativedelta(months=round(cfg.step_months))

    train_start = start
    while True:
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_end > end:
            break
        out.append(
            Window(
                train_start=pd.Timestamp(train_start),
                train_end=pd.Timestamp(train_end),
                test_start=pd.Timestamp(test_start),
                test_end=pd.Timestamp(test_end),
            )
        )
        train_start = train_start + step_delta
    return out


def run_walkforward(
    enriched: Mapping[str, pd.DataFrame],
    bt_config: BacktestConfig,
    wf_config: WalkForwardConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    ctx_for: Callable[[pd.Timestamp], ScanContext] | None = None,
    signal_provider: SignalProvider | None = None,
) -> tuple[BacktestResult, list[Window]]:
    """Run the engine on each test window, concatenate results.

    Each fold restarts at `bt_config.initial_capital`. The aggregated equity
    curve is the per-fold curves joined end-to-end (gaps from train periods
    are dropped). Phase 16 will retrain a model on each train slice before
    running its test slice.
    """
    folds = windows(start, end, wf_config)
    all_trades: list[Trade] = []
    equity_pieces: list[pd.Series] = []
    daily_pieces: list[pd.Series] = []
    total_costs = 0.0
    final_cash = bt_config.initial_capital

    for win in folds:
        fold_result = run_backtest(
            enriched,
            bt_config,
            win.test_start,
            win.test_end,
            ctx_for=ctx_for,
            signal_provider=signal_provider,
        )
        all_trades.extend(fold_result.trades)
        equity_pieces.append(fold_result.equity_curve)
        daily_pieces.append(fold_result.daily_returns)
        total_costs += fold_result.total_costs
        final_cash = fold_result.final_cash  # last fold wins

    if equity_pieces:
        equity_curve = pd.concat(equity_pieces)
        equity_curve = equity_curve[~equity_curve.index.duplicated(keep="last")].sort_index()
        daily_returns = pd.concat(daily_pieces)
        daily_returns = daily_returns[~daily_returns.index.duplicated(keep="last")].sort_index()
    else:
        equity_curve = pd.Series([], dtype=float, name="equity")
        daily_returns = pd.Series([], dtype=float)

    aggregated = BacktestResult(
        config=bt_config,
        trades=tuple(all_trades),
        equity_curve=equity_curve,
        daily_returns=daily_returns,
        total_costs=total_costs,
        final_cash=final_cash,
    )
    return aggregated, folds
