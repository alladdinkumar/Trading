"""Phase 7.3 — walk-forward harness tests."""

from __future__ import annotations

import pandas as pd
import pytest

from trading.backtest.engine import BacktestConfig, Signal
from trading.backtest.walkforward import WalkForwardConfig, run_walkforward, windows
from trading.strategy.rules import ScanContext


def test_windows_basic_count() -> None:
    """Jan 2020 → Jan 2027, 3y/6mo/3mo. First test starts 2023-01-01; last test
    must end ≤ 2027-01-01. Step is 3mo, so test_start sequence is
    2023-01, 2023-04, 2023-07, ..., 2026-07. That's 15 windows.
    """
    cfg = WalkForwardConfig(train_years=3, test_months=6, step_months=3)
    wins = windows(pd.Timestamp("2020-01-01"), pd.Timestamp("2027-01-01"), cfg)
    assert len(wins) == 15
    assert wins[0].train_start == pd.Timestamp("2020-01-01")
    assert wins[0].test_start == pd.Timestamp("2023-01-01")
    assert wins[0].test_end == pd.Timestamp("2023-07-01")
    assert wins[-1].test_end <= pd.Timestamp("2027-01-01")


def test_windows_drops_partial_last() -> None:
    """If end falls before a full test window would close, drop it."""
    cfg = WalkForwardConfig(train_years=3, test_months=6, step_months=3)
    wins = windows(pd.Timestamp("2020-01-01"), pd.Timestamp("2023-05-01"), cfg)
    # First test_end would be 2023-07-01 > 2023-05-01 → no folds.
    assert wins == []


def test_windows_test_segments_no_overlap_but_step_overlaps_train() -> None:
    """Adjacent train windows overlap (step < train_years) — that's fine —
    but adjacent TEST windows partially overlap when step < test_months.
    Verify the step is honoured."""
    cfg = WalkForwardConfig(train_years=3, test_months=6, step_months=3)
    wins = windows(pd.Timestamp("2020-01-01"), pd.Timestamp("2024-01-01"), cfg)
    assert len(wins) >= 2
    # Step is 3 months → test_start advances by 3 months
    months_between = (wins[1].test_start.year - wins[0].test_start.year) * 12 + (
        wins[1].test_start.month - wins[0].test_start.month
    )
    assert months_between == 3


def test_run_walkforward_concatenates_trades() -> None:
    """Run a tiny backtest across two non-overlapping test folds. The provider
    always emits — engine takes the first one and skips re-signals while
    long. Each fold therefore produces exactly one OPEN_AT_END trade."""
    dates = pd.date_range("2020-01-01", "2024-06-30", freq="B")
    df = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000_000,
            "atr_14": 1.0,
        },
        index=dates,
    )

    def provider(
        _d: pd.Timestamp,
        _enriched: object,
        _ctx: ScanContext,
        _cfg: BacktestConfig,
    ) -> list[Signal]:
        return [Signal(symbol="X", close=100.0, atr=1.0, stop_price=95.0)]

    bt = BacktestConfig(initial_capital=100_000)
    # step_months=6 == test_months → no overlap between adjacent test windows.
    wf = WalkForwardConfig(train_years=3, test_months=6, step_months=6)
    agg, folds = run_walkforward(
        {"X": df},
        bt,
        wf,
        pd.Timestamp("2020-01-01"),
        pd.Timestamp("2024-06-30"),
        signal_provider=provider,
    )
    assert len(folds) >= 2
    # Trades fire in every fold. Verify both folds contributed.
    assert len(agg.trades) >= len(folds)
    trade_dates = [t.entry_date for t in agg.trades]
    fold_1_test_range = (folds[0].test_start, folds[0].test_end)
    fold_2_test_range = (folds[1].test_start, folds[1].test_end)
    assert any(fold_1_test_range[0] <= d <= fold_1_test_range[1] for d in trade_dates)
    assert any(fold_2_test_range[0] <= d <= fold_2_test_range[1] for d in trade_dates)


def test_run_walkforward_empty_when_no_folds() -> None:
    """End-date earlier than first possible test_end → 0 folds, empty result."""
    df = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1_000_000],
            "atr_14": [1.0],
        },
        index=pd.date_range("2020-01-01", periods=1, freq="B"),
    )
    cfg = BacktestConfig()
    wf = WalkForwardConfig()
    agg, folds = run_walkforward(
        {"X": df},
        cfg,
        wf,
        pd.Timestamp("2020-01-01"),
        pd.Timestamp("2020-02-01"),
    )
    assert folds == []
    assert agg.trades == ()
    assert len(agg.equity_curve) == 0
    assert agg.total_costs == pytest.approx(0.0)
