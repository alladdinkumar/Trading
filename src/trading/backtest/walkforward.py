"""Walk-forward harness — spec §8.3.

Rolling 3y train / 6mo test / 3mo step. For Phase 7's rules-only baseline
the `train_*` window isn't consumed (no model to fit); the structure is in
place for Phase 16 (LightGBM ranker), which will refit on each train slice.

`run_walkforward` runs the engine on each out-of-sample test window with
the same initial capital and concatenates the per-fold trades. Because each
fold restarts at `initial_capital`, the aggregated equity curve is rebuilt as
a single *compounding* series — fold N's growth chained onto fold N-1's end —
rather than a naive concat of levels that would sawtooth at every reset (F-066).
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

    Each fold restarts at `bt_config.initial_capital`, so the aggregated equity
    curve is *not* a raw concat of levels (that sawtooths at every reset and
    breaks `cagr()`/`max_drawdown()`). Instead the per-fold day-over-day returns
    are stitched — dropping each fold's fabricated leading zero and, on
    overlapping windows, keeping the later fold's version of a shared day — and
    compounded off a single `initial_capital` base (F-066). Phase 16 will
    retrain a model on each train slice before running its test slice.
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

    # Fold windows with no trading dates yield empty per-fold curves; if *every*
    # fold is empty there is nothing to compound (and `min()` over an empty
    # sequence would crash), so fall through to the same no-data result.
    non_empty_equity = [piece for piece in equity_pieces if len(piece)]
    if non_empty_equity:
        # Each fold's `daily_returns` opens with a fabricated `fillna(0.0)` base
        # row (pct_change of the first bar). Drop it so no zero return is spliced
        # in at a fold boundary; `keep="last"` then lets a later, overlapping
        # fold win a shared day (i.e. the earlier fold's overlap is dropped).
        daily_returns = pd.concat([piece.iloc[1:] for piece in daily_pieces])
        daily_returns = daily_returns[~daily_returns.index.duplicated(keep="last")].sort_index()

        # Rebuild ONE compounding equity curve off a single `initial_capital`
        # base, anchored at the earliest fold's first date.
        first_date = min(piece.index[0] for piece in non_empty_equity)
        base = float(bt_config.initial_capital)
        body = base * (1.0 + daily_returns).cumprod()
        anchor = pd.Series({first_date: base}, dtype=float)
        equity_curve = pd.concat([anchor, body])
        equity_curve = equity_curve[~equity_curve.index.duplicated(keep="first")].sort_index()
        equity_curve.name = "equity"
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
