"""Daily mark-to-market for open paper trades (spec §9.3).

`mtm_open_trades` is a pure function over a `bars: dict[symbol, Bar]`
map. The caller picks the source — Kite quotes at mid-day, today's
official close at post-close, parquet history when replaying. The MTM
function reads each open `paper_trade`, reconstructs the `TradeState`
exits.evaluate_exit needs, runs the exit logic, and either:

  - closes the trade (with the decision's `exit_price` and `reason`); or
  - persists the ratcheted `current_stop` + bumped `days_held` for the
    next MTM run.

Trades missing a bar in `bars` are skipped (and surfaced via the
`MtmResult.note`) rather than mis-marked.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from trading.paper.ledger import close_with_exit, open_trades
from trading.store.repo import (
    ExitReason,
    Signal,
    get_signal,
    update_trailing_state,
)
from trading.strategy.exits import (
    Bar,
    ExitDecision,
    TradeState,
    evaluate_exit,
)


@dataclass(frozen=True)
class MtmResult:
    """Per-trade outcome of one MTM run — what the CLI table renders."""

    paper_trade_id: int
    symbol: str
    action: str  # 'HOLD' | 'EXIT_STOP' | 'EXIT_TARGET' | 'EXIT_TIME' | 'SKIP'
    new_stop: float | None
    exit_price: float | None
    reason: str
    note: str | None = None  # set on SKIP


def _signal_symbol(conn: sqlite3.Connection, signal_id: int) -> tuple[str, Signal | None]:
    """Look up the symbol + Signal row for a given paper_trade.signal_id."""
    sig = get_signal(conn, signal_id)
    return (sig.symbol if sig else ""), sig


def mtm_open_trades(
    conn: sqlite3.Connection,
    bars: dict[str, Bar],
    *,
    as_of: datetime,
) -> list[MtmResult]:
    """Run one MTM pass over every open paper trade.

    Persists side-effects: closes trades that hit stop/target/time, and
    updates `current_stop` + `days_held` for those that HOLD. Returns
    one `MtmResult` per trade evaluated (including SKIPs).
    """
    results: list[MtmResult] = []
    for trade in open_trades(conn):
        symbol, sig = _signal_symbol(conn, trade.signal_id)
        if sig is None:
            results.append(
                MtmResult(
                    paper_trade_id=trade.id or 0,
                    symbol=symbol,
                    action="SKIP",
                    new_stop=None,
                    exit_price=None,
                    reason="orphan trade (signal row missing)",
                    note="data integrity issue",
                )
            )
            continue

        bar = bars.get(symbol)
        if bar is None:
            results.append(
                MtmResult(
                    paper_trade_id=trade.id or 0,
                    symbol=symbol,
                    action="SKIP",
                    new_stop=trade.current_stop,
                    exit_price=None,
                    reason="no bar for symbol",
                    note=f"missing bar for {symbol}",
                )
            )
            continue

        current_stop = trade.current_stop if trade.current_stop is not None else sig.stop
        atr_at_entry = trade.atr_at_entry if trade.atr_at_entry is not None else 0.0
        days_held = (trade.days_held or 0) + 1  # MTM bumps days_held per call

        state = TradeState(
            entry=trade.entry_price,
            initial_stop=sig.stop,
            current_stop=current_stop,
            atr_at_entry=atr_at_entry,
            days_held=days_held,
        )
        decision: ExitDecision = evaluate_exit(state, bar)

        trade_id = trade.id
        assert trade_id is not None, "open trades always have an id"

        if decision.action == "HOLD":
            update_trailing_state(
                conn,
                trade_id,
                current_stop=decision.new_stop,
                days_held=days_held,
            )
            results.append(
                MtmResult(
                    paper_trade_id=trade_id,
                    symbol=symbol,
                    action="HOLD",
                    new_stop=decision.new_stop,
                    exit_price=None,
                    reason=decision.reason,
                )
            )
            continue

        # Exit branches — close with computed pnl
        assert decision.exit_price is not None, "non-HOLD decision must carry exit_price"
        exit_reason = _exit_reason_from_action(decision.action)
        close_with_exit(
            conn,
            trade_id,
            exit_ts=as_of,
            exit_price=decision.exit_price,
            exit_reason=exit_reason,
            days_held=days_held,
        )
        results.append(
            MtmResult(
                paper_trade_id=trade_id,
                symbol=symbol,
                action=decision.action,
                new_stop=decision.new_stop,
                exit_price=decision.exit_price,
                reason=decision.reason,
            )
        )
    return results


def _exit_reason_from_action(action: str) -> ExitReason:
    """Map exits.ExitAction → paper_trades.exit_reason CHECK enum."""
    mapping: dict[str, ExitReason] = {
        "EXIT_STOP": "STOP",
        "EXIT_TARGET": "TARGET",
        "EXIT_TIME": "TIME",
    }
    return mapping[action]


def build_bars_from_history(
    histories: Mapping[str, object],
    as_of: datetime,
) -> dict[str, Bar]:
    """Translate `dict[symbol, OHLCV DataFrame]` → `dict[symbol, Bar]` at `as_of`.

    Picks the row whose index date == `as_of.date()` (or the latest one
    strictly before it). Skips symbols whose history doesn't reach
    `as_of` — those flow through MTM as SKIP entries.
    """
    import pandas as pd

    target = pd.Timestamp(as_of.date())
    out: dict[str, Bar] = {}
    for symbol, df in histories.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        valid = df.dropna(subset=["close"]) if "close" in df.columns else df
        if valid.empty:
            continue
        # Last row ≤ target
        idx = valid.index
        try:
            position = idx.searchsorted(target, side="right") - 1
        except Exception:
            continue
        if position < 0:
            continue
        row = valid.iloc[position]
        out[symbol] = Bar(
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
    return out
