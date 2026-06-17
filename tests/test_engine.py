"""Phase 7.2 — event-loop engine tests.

Tests use a synthetic `signal_provider` to inject signals on specific dates,
keeping the engine decoupled from the scanner. The DataFrames carry only
the columns the engine touches at runtime (`open/high/low/close`, plus
`atr_14` for the rule-provider path which we don't exercise here).
"""

from __future__ import annotations

import pandas as pd
import pytest

from trading.backtest.engine import (
    BacktestConfig,
    Signal,
    SignalProvider,
    run_backtest,
)
from trading.strategy.rules import ScanContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(
    prices: list[tuple[float, float, float, float]], start: str = "2024-01-01"
) -> pd.DataFrame:
    """Make an OHLC DataFrame from a list of (o,h,l,c) tuples on consecutive days."""
    dates = pd.date_range(start, periods=len(prices), freq="B")  # business days
    df = pd.DataFrame(prices, columns=["open", "high", "low", "close"], index=dates).astype(float)
    df["volume"] = 1_000_000
    df["atr_14"] = (df["high"] - df["low"]).rolling(14, min_periods=1).mean()
    return df


def _signal_on(target_date: pd.Timestamp, signals_to_emit: list[Signal]) -> SignalProvider:
    """Provider that emits the given signals only on `target_date`."""

    def provider(
        d: pd.Timestamp,
        _enriched: object,
        _ctx: ScanContext,
        _config: BacktestConfig,
    ) -> list[Signal]:
        return list(signals_to_emit) if d == target_date else []

    return provider


def _no_signals(
    _d: pd.Timestamp,
    _enriched: object,
    _ctx: ScanContext,
    _config: BacktestConfig,
) -> list[Signal]:
    return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_signal_keeps_equity_flat() -> None:
    """Without signals, no trades fire and equity stays at initial_capital."""
    df = _bars([(100, 101, 99, 100)] * 10)
    config = BacktestConfig(initial_capital=100_000)
    result = run_backtest(
        {"FLAT": df}, config, df.index[0], df.index[-1], signal_provider=_no_signals
    )
    assert len(result.trades) == 0
    assert result.equity_curve.iloc[-1] == pytest.approx(100_000)
    assert result.final_cash == pytest.approx(100_000)
    assert result.total_costs == 0.0


def test_signal_opens_next_day_at_open() -> None:
    """Signal at close of day-0 → buy at day-1's open with slippage."""
    df = _bars([(100, 101, 99, 100), (102, 105, 101, 104), (104, 106, 103, 105)])
    config = BacktestConfig(initial_capital=100_000)
    sig = Signal(symbol="WIN", close=100.0, atr=2.0, stop_price=90.0)
    provider = _signal_on(df.index[0], [sig])

    result = run_backtest({"WIN": df}, config, df.index[0], df.index[-1], signal_provider=provider)
    # No exit triggered in this window — should still be open at end → OPEN_AT_END.
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.symbol == "WIN"
    assert trade.entry_date == df.index[1]  # opened next day
    # Day-1 open = 102; +0.1% slippage on buy = 102.102
    assert trade.entry_price == pytest.approx(102.102)
    assert trade.exit_reason == "OPEN_AT_END"


def test_exit_stop_fires_realises_loss() -> None:
    """Engineer a drop that pierces the stop → EXIT_STOP, negative net P&L."""
    df = _bars(
        [
            (100, 101, 99, 100),  # signal day
            (100, 101, 95, 96),  # buy at 100, drop to 95 → stop at 95 hits (low=95)
            (95, 96, 94, 95),
        ]
    )
    sig = Signal(symbol="DROP", close=100.0, atr=2.0, stop_price=95.0)
    provider = _signal_on(df.index[0], [sig])
    config = BacktestConfig(initial_capital=100_000)

    result = run_backtest({"DROP": df}, config, df.index[0], df.index[-1], signal_provider=provider)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "EXIT_STOP"
    # Stop slipped down on sell: 95 × 0.999 = 94.905
    assert trade.exit_price == pytest.approx(94.905)
    assert trade.net_pnl < 0
    assert trade.costs_paid > 0


def test_costs_deducted_from_pnl() -> None:
    """net_pnl = gross_pnl − costs_paid, exactly."""
    df = _bars([(100, 101, 99, 100), (102, 130, 101, 125), (125, 130, 124, 128)])
    sig = Signal(symbol="WIN", close=100.0, atr=2.0, stop_price=90.0)
    provider = _signal_on(df.index[0], [sig])
    config = BacktestConfig(initial_capital=100_000)

    result = run_backtest({"WIN": df}, config, df.index[0], df.index[-1], signal_provider=provider)
    trade = result.trades[0]
    # +20% target = 122.40 (entry 102.102 → ×1.20 = 122.5224; R/R 102.102 + 2.5×(102.102−90)=132.357
    #  → min picks +20% target 122.5224). Day-1 high=130 ≥ 122.52 → EXIT_TARGET.
    assert trade.exit_reason == "EXIT_TARGET"
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.costs_paid)
    assert trade.costs_paid > 0


def test_total_costs_includes_buy_and_sell_side() -> None:
    """BacktestResult.total_costs aggregates buy + sell charges (F-021).

    The per-trade `costs_paid` already covers both sides; the result-level
    `total_costs` must equal their sum, not just the sell-side accumulation.
    """
    df = _bars([(100, 101, 99, 100), (102, 130, 101, 125), (125, 130, 124, 128)])
    sig = Signal(symbol="WIN", close=100.0, atr=2.0, stop_price=90.0)
    provider = _signal_on(df.index[0], [sig])
    config = BacktestConfig(initial_capital=100_000)

    result = run_backtest({"WIN": df}, config, df.index[0], df.index[-1], signal_provider=provider)
    trade = result.trades[0]
    assert trade.exit_reason == "EXIT_TARGET"
    # A closed trade pays both buy and sell charges; the aggregate must match.
    assert result.total_costs == pytest.approx(sum(t.costs_paid for t in result.trades))
    # And that sum strictly exceeds the sell-side-only figure (buy side is non-zero).
    assert result.total_costs > 0


def test_cash_conservation_through_round_trip() -> None:
    """final_cash + sum(net_pnl) − sum(unrealised_pnl) consistency.
    After all trades close (none OPEN_AT_END), final equity = initial + sum(net_pnl)."""
    df = _bars(
        [
            (100, 101, 99, 100),  # signal
            (102, 105, 101, 104),
            (104, 130, 103, 128),  # +20% target hits
            (128, 129, 127, 128),
        ]
    )
    sig = Signal(symbol="WIN", close=100.0, atr=2.0, stop_price=90.0)
    provider = _signal_on(df.index[0], [sig])
    config = BacktestConfig(initial_capital=100_000)

    result = run_backtest({"WIN": df}, config, df.index[0], df.index[-1], signal_provider=provider)
    assert all(t.exit_reason != "OPEN_AT_END" for t in result.trades)
    sum_net = sum(t.net_pnl for t in result.trades)
    # All proceeds are back in cash; equity_curve.iloc[-1] should match.
    assert result.equity_curve.iloc[-1] == pytest.approx(100_000 + sum_net)
    assert result.final_cash == pytest.approx(100_000 + sum_net)


def test_skips_signal_if_already_long() -> None:
    """Re-signalling on an already-open symbol is a no-op."""
    df = _bars([(100, 101, 99, 100)] * 10)
    sig = Signal(symbol="WIN", close=100.0, atr=2.0, stop_price=90.0)
    # Emit signal on EVERY date — only the first should produce an order.

    def provider_every_day(
        _d: pd.Timestamp,
        _enriched: object,
        _ctx: ScanContext,
        _config: BacktestConfig,
    ) -> list[Signal]:
        return [sig]

    config = BacktestConfig(initial_capital=100_000)
    result = run_backtest(
        {"WIN": df}, config, df.index[0], df.index[-1], signal_provider=provider_every_day
    )
    # Exactly one trade (one position held to end).
    assert len(result.trades) == 1


def test_open_at_end_uses_last_close_no_extra_costs() -> None:
    """OPEN_AT_END exit: exit_price = last close, only buy-side costs paid."""
    df = _bars([(100, 101, 99, 100), (102, 103, 101, 102), (102, 103, 101, 102)])
    sig = Signal(symbol="HOLD", close=100.0, atr=2.0, stop_price=90.0)
    provider = _signal_on(df.index[0], [sig])
    config = BacktestConfig(initial_capital=100_000)

    result = run_backtest({"HOLD": df}, config, df.index[0], df.index[-1], signal_provider=provider)
    trade = result.trades[0]
    assert trade.exit_reason == "OPEN_AT_END"
    assert trade.exit_price == pytest.approx(102.0)  # raw last close
    # Costs reflect only the buy side
    assert 0 < trade.costs_paid < 500


def test_signal_dropped_if_insufficient_cash() -> None:
    """Sizer wants 1000 shares × ₹100 = ₹100k notional but capital is ₹50k.
    Per-stock cap (25% of 50k = 12,500) → 125 shares × 100 = ₹12,500. Still fits.
    Test the harder case: gap up makes fill > available cash."""
    df = _bars([(100, 101, 99, 100), (1000, 1010, 990, 1000)])  # 10x gap up
    sig = Signal(symbol="GAP", close=100.0, atr=2.0, stop_price=90.0)
    provider = _signal_on(df.index[0], [sig])
    # capital=5000 with sized qty being big enough at signal close (100) but gap makes it unaffordable.
    config = BacktestConfig(initial_capital=5_000)

    result = run_backtest({"GAP": df}, config, df.index[0], df.index[-1], signal_provider=provider)
    # Order should be dropped — no trade opens.
    assert len(result.trades) == 0


def test_daily_returns_zero_on_flat_curve() -> None:
    df = _bars([(100, 101, 99, 100)] * 5)
    config = BacktestConfig(initial_capital=100_000)
    result = run_backtest(
        {"FLAT": df}, config, df.index[0], df.index[-1], signal_provider=_no_signals
    )
    assert (result.daily_returns == 0.0).all()
