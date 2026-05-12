"""Event-loop backtester — spec §8.

Drives the Phase 5 scanner + Phase 6 sizing/exits over historical bars,
applying Zerodha-realistic costs. Same code path will run live in the
Phase 11 paper ledger.

Anti-bias guarantees (spec §8.6):

- Scanner inputs are sliced strictly to `d` inclusive — no peeking forward.
- Signals fire end-of-day `d`, orders fill at `d+1`'s open with slippage.
- Stops fire intra-bar at the stop price (not at low) — conservative fills.
- Same-bar stop+target tie resolves to stop (handled by `evaluate_exit`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

import pandas as pd

from trading.backtest.costs import (
    CostConfig,
    apply_slippage,
    buy_charges,
    sell_charges,
)
from trading.strategy.exits import Bar, TradeState, evaluate_exit
from trading.strategy.rules import (
    MIN_HISTORY_BARS,
    ScanContext,
    evaluate_symbol,
)
from trading.strategy.sizing import Regime, SizingInput, position_size

ExitReason = Literal["EXIT_STOP", "EXIT_TARGET", "EXIT_TIME", "OPEN_AT_END"]


@dataclass(frozen=True)
class Signal:
    """A pass-through signal emitted by a SignalProvider for date `d`.

    `close` and `atr` are the EOD-`d` values used for sizing; `stop_price` is
    the absolute stop level (already factoring in `atr_stop_multiple`).
    """

    symbol: str
    close: float
    atr: float
    stop_price: float


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 500_000.0
    risk_pct: float = 0.02
    atr_stop_multiple: float = 1.5
    max_per_stock_pct: float = 0.25
    max_per_sector_pct: float = 0.30
    regime: Regime = "RISK_ON"
    costs: CostConfig = field(default_factory=CostConfig)
    sector_of: Callable[[str], str] | None = None


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    initial_stop: float
    qty: int
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: ExitReason
    gross_pnl: float
    costs_paid: float
    net_pnl: float


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    trades: tuple[Trade, ...]
    equity_curve: pd.Series
    daily_returns: pd.Series
    total_costs: float
    final_cash: float


SignalProvider = Callable[
    [pd.Timestamp, Mapping[str, pd.DataFrame], ScanContext, BacktestConfig],
    list[Signal],
]


def rule_signal_provider(
    d: pd.Timestamp,
    enriched: Mapping[str, pd.DataFrame],
    ctx: ScanContext,
    config: BacktestConfig,
) -> list[Signal]:
    """Default provider — runs the Phase 5 rule scanner on each symbol."""
    signals: list[Signal] = []
    for sym, df in enriched.items():
        if d not in df.index:
            continue
        slice_df = df.loc[:d]
        if len(slice_df) < MIN_HISTORY_BARS:
            continue
        candidate = evaluate_symbol(sym, slice_df, ctx)
        if not candidate.all_passed:
            continue
        close_d = float(df.at[d, "close"])  # type: ignore[arg-type]
        atr = float(df.at[d, "atr_14"])  # type: ignore[arg-type]
        if math.isnan(atr) or atr <= 0:
            continue
        stop = close_d - config.atr_stop_multiple * atr
        if stop <= 0 or stop >= close_d:
            continue
        signals.append(Signal(symbol=sym, close=close_d, atr=atr, stop_price=stop))
    return signals


# Internal mutable bookkeeping types — kept private to the module.


@dataclass(frozen=True)
class _OpenPosition:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    initial_stop: float
    current_stop: float
    atr_at_entry: float
    days_held: int
    buy_costs_paid: float
    sector: str


@dataclass(frozen=True)
class _PendingOrder:
    symbol: str
    signal_date: pd.Timestamp
    planned_stop: float
    qty: int
    atr_at_entry: float
    sector: str


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_backtest(
    enriched: Mapping[str, pd.DataFrame],
    config: BacktestConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    ctx_for: Callable[[pd.Timestamp], ScanContext] | None = None,
    signal_provider: SignalProvider | None = None,
) -> BacktestResult:
    """Run the event-loop backtest over `enriched` (symbol → indicator-enriched OHLCV).

    `enriched[sym]` must have columns `open`, `high`, `low`, `close`, `volume`,
    plus the technical-indicator columns produced by `features.technicals.add_indicators`
    (`sma_20/50/200`, `rsi_14`, `atr_14`, etc.). Indexed by date.

    `signal_provider` lets Phase 16's ranker (or tests) inject signals directly;
    defaults to `rule_signal_provider` (Phase 5 Layer A scanner).
    """
    sector_of = config.sector_of or (lambda _sym: "UNKNOWN")
    provider = signal_provider or rule_signal_provider
    all_dates = _trading_dates(enriched, start, end)

    cash = config.initial_capital
    total_costs = 0.0
    completed_trades: list[Trade] = []
    open_positions: dict[str, _OpenPosition] = {}
    pending: list[_PendingOrder] = []
    equity_records: list[tuple[pd.Timestamp, float]] = []

    for d in all_dates:
        # 1. Open queued entries from yesterday's EOD signals.
        cash, pending = _execute_pending(
            d, pending, enriched, config, open_positions, cash_ref=cash
        )
        # _execute_pending mutates open_positions and accumulates costs via closure-side-effect.
        # Re-pull total_costs increment by re-summing — clean approach below.

        # 2. Evaluate exits on currently-open positions.
        cash, exits_costs, exits = _evaluate_exits(d, open_positions, enriched, config, cash)
        completed_trades.extend(exits)
        total_costs += exits_costs

        # 3. Mark-to-market today's close.
        equity_records.append((d, _mark_to_market(d, cash, open_positions, enriched)))

        # 4. EOD signals → queue for tomorrow.
        equity_now = equity_records[-1][1]
        ctx = ctx_for(d) if ctx_for else ScanContext(scan_date=d.date())
        signals = provider(d, enriched, ctx, config)
        _queue_signals(
            d=d,
            signals=signals,
            enriched=enriched,
            config=config,
            equity_now=equity_now,
            open_positions=open_positions,
            pending=pending,
            sector_of=sector_of,
        )

    # Force-close any still-open positions at the last in-window close.
    completed_trades.extend(_close_at_end(open_positions, enriched, end))

    equity_curve = pd.Series(dict(equity_records), name="equity", dtype=float).sort_index()
    daily_returns = equity_curve.pct_change().fillna(0.0)

    return BacktestResult(
        config=config,
        trades=tuple(completed_trades),
        equity_curve=equity_curve,
        daily_returns=daily_returns,
        total_costs=total_costs,
        final_cash=cash,
    )


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _trading_dates(
    enriched: Mapping[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp
) -> list[pd.Timestamp]:
    seen: set[pd.Timestamp] = set()
    for df in enriched.values():
        mask = (df.index >= start) & (df.index <= end)
        seen.update(df.index[mask])
    return sorted(seen)


def _execute_pending(
    d: pd.Timestamp,
    pending: list[_PendingOrder],
    enriched: Mapping[str, pd.DataFrame],
    config: BacktestConfig,
    open_positions: dict[str, _OpenPosition],
    *,
    cash_ref: float,
) -> tuple[float, list[_PendingOrder]]:
    cash = cash_ref
    carried: list[_PendingOrder] = []
    for order in pending:
        df = enriched.get(order.symbol)
        if df is None or d not in df.index:
            # Symbol skipped today — drop the order (single-day order TIF).
            continue
        if order.symbol in open_positions:
            continue
        bar_open = float(df.at[d, "open"])  # type: ignore[arg-type]
        fill_price = apply_slippage(bar_open, "buy", config.costs)
        value = fill_price * order.qty
        charges = buy_charges(value, config.costs)
        debit = value + charges.total
        if debit > cash:
            continue  # not enough cash after a gap
        cash -= debit
        open_positions[order.symbol] = _OpenPosition(
            symbol=order.symbol,
            entry_date=d,
            entry_price=fill_price,
            qty=order.qty,
            initial_stop=order.planned_stop,
            current_stop=order.planned_stop,
            atr_at_entry=order.atr_at_entry,
            days_held=0,
            buy_costs_paid=charges.total,
            sector=order.sector,
        )
    return cash, carried


def _evaluate_exits(
    d: pd.Timestamp,
    open_positions: dict[str, _OpenPosition],
    enriched: Mapping[str, pd.DataFrame],
    config: BacktestConfig,
    cash: float,
) -> tuple[float, float, list[Trade]]:
    exits: list[Trade] = []
    costs_total = 0.0
    for sym in list(open_positions.keys()):
        pos = open_positions[sym]
        df = enriched[sym]
        if d not in df.index:
            continue
        row = df.loc[d]
        bar = Bar(
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
        state = TradeState(
            entry=pos.entry_price,
            initial_stop=pos.initial_stop,
            current_stop=pos.current_stop,
            atr_at_entry=pos.atr_at_entry,
            days_held=pos.days_held,
        )
        decision = evaluate_exit(state, bar)
        if decision.action == "HOLD":
            open_positions[sym] = replace(
                pos, current_stop=decision.new_stop, days_held=pos.days_held + 1
            )
            continue

        assert decision.exit_price is not None  # guaranteed for EXIT_* branches
        exit_slipped = apply_slippage(decision.exit_price, "sell", config.costs)
        sell_value = exit_slipped * pos.qty
        sell_breakdown = sell_charges(sell_value, config.costs)
        cash += sell_value - sell_breakdown.total
        costs_total += sell_breakdown.total

        gross = (exit_slipped - pos.entry_price) * pos.qty
        costs_paid = pos.buy_costs_paid + sell_breakdown.total
        exits.append(
            Trade(
                symbol=sym,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                initial_stop=pos.initial_stop,
                qty=pos.qty,
                exit_date=d,
                exit_price=exit_slipped,
                exit_reason=decision.action,
                gross_pnl=gross,
                costs_paid=costs_paid,
                net_pnl=gross - costs_paid,
            )
        )
        del open_positions[sym]
    return cash, costs_total, exits


def _mark_to_market(
    d: pd.Timestamp,
    cash: float,
    open_positions: Mapping[str, _OpenPosition],
    enriched: Mapping[str, pd.DataFrame],
) -> float:
    equity = cash
    for sym, pos in open_positions.items():
        df = enriched[sym]
        if d in df.index:
            equity += pos.qty * float(df.at[d, "close"])  # type: ignore[arg-type]
        else:
            equity += pos.qty * pos.entry_price  # stale fallback; rare
    return equity


def _queue_signals(
    *,
    d: pd.Timestamp,
    signals: list[Signal],
    enriched: Mapping[str, pd.DataFrame],
    config: BacktestConfig,
    equity_now: float,
    open_positions: Mapping[str, _OpenPosition],
    pending: list[_PendingOrder],
    sector_of: Callable[[str], str],
) -> None:
    pending_symbols = {p.symbol for p in pending}
    for sig in signals:
        if sig.symbol in open_positions or sig.symbol in pending_symbols:
            continue
        sector = sector_of(sig.symbol)
        deployed_sector = 0.0
        for held in open_positions.values():
            if held.sector != sector:
                continue
            df_held = enriched[held.symbol]
            if d not in df_held.index:
                continue
            deployed_sector += held.qty * float(df_held.at[d, "close"])  # type: ignore[arg-type]
        sizing = position_size(
            SizingInput(
                capital=equity_now,
                entry=sig.close,
                stop=sig.stop_price,
                risk_pct=config.risk_pct,
                regime=config.regime,
                deployed_in_symbol=0.0,
                deployed_in_sector=deployed_sector,
                max_per_stock_pct=config.max_per_stock_pct,
                max_per_sector_pct=config.max_per_sector_pct,
            )
        )
        if sizing.qty <= 0:
            continue
        pending.append(
            _PendingOrder(
                symbol=sig.symbol,
                signal_date=d,
                planned_stop=sig.stop_price,
                qty=sizing.qty,
                atr_at_entry=sig.atr,
                sector=sector,
            )
        )


def _close_at_end(
    open_positions: Mapping[str, _OpenPosition],
    enriched: Mapping[str, pd.DataFrame],
    end: pd.Timestamp,
) -> list[Trade]:
    out: list[Trade] = []
    for sym, pos in open_positions.items():
        df = enriched[sym]
        in_window = df.loc[:end]
        if in_window.empty:
            continue
        last_date = in_window.index[-1]
        last_close = float(df.at[last_date, "close"])  # type: ignore[arg-type]
        gross = (last_close - pos.entry_price) * pos.qty
        out.append(
            Trade(
                symbol=sym,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                initial_stop=pos.initial_stop,
                qty=pos.qty,
                exit_date=last_date,
                exit_price=last_close,
                exit_reason="OPEN_AT_END",
                gross_pnl=gross,
                costs_paid=pos.buy_costs_paid,
                net_pnl=gross - pos.buy_costs_paid,
            )
        )
    return out
