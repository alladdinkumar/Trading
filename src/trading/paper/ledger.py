"""Paper-trade ledger orchestration — spec §9.

Thin convenience layer over `store.repo` that bundles the workflows the
rest of the system actually performs (log + open + predict atomically;
close + compute pnl from a single exit decision). Direct SQL stays in
repo.py so this module remains free of SQL strings and easy to test.

Lifecycle:
  log_signal_and_open_trade()  →  insert signals + paper_trades rows
                                  + a matching predictions row
  …time passes, MTM ratchets stop…
  close_with_exit()            →  close paper_trade with pnl/days_held
  evaluate_matured_prediction()→  fill predictions.actual_return_at_horizon
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading.store.repo import (
    ExitReason,
    PaperTrade,
    Prediction,
    Signal,
    close_paper_trade,
    get_paper_trade,
    insert_paper_trade,
    insert_prediction,
    insert_signal,
    list_open_paper_trades,
)


@dataclass(frozen=True)
class TradeOpenResult:
    """Returned by `log_signal_and_open_trade` so callers don't re-query."""

    signal_id: int
    paper_trade_id: int
    prediction_id: int


def _isoformat(ts: datetime | str) -> str:
    return ts.isoformat() if isinstance(ts, datetime) else ts


def log_signal_and_open_trade(
    conn: sqlite3.Connection,
    *,
    signal: Signal,
    entry_ts: datetime | str,
    entry_price: float,
    qty: int,
    atr_at_entry: float | None,
    predicted_return_pct: float | None = None,
) -> TradeOpenResult:
    """Atomic: log a signal, open the paper trade, record the prediction.

    `signal.id` is ignored on input (assigned by AUTOINCREMENT). The
    `current_stop` on the trade initialises to `signal.stop` — MTM
    ratchets it from there. `predicted_return_pct` defaults to the
    signal's implied target % so the ranker can later score
    actual-vs-expected.
    """
    signal_id = insert_signal(conn, signal)

    trade = PaperTrade(
        id=None,
        signal_id=signal_id,
        ts_entry=_isoformat(entry_ts),
        entry_price=entry_price,
        qty=qty,
        days_held=0,
        current_stop=signal.stop,
        atr_at_entry=atr_at_entry,
    )
    trade_id = insert_paper_trade(conn, trade)

    implied_return = (
        predicted_return_pct
        if predicted_return_pct is not None
        else ((signal.target - signal.entry) / signal.entry * 100.0)
    )
    prediction_id = insert_prediction(
        conn,
        Prediction(
            id=None,
            ts=_isoformat(entry_ts),
            symbol=signal.symbol,
            predicted_return_pct=implied_return,
            predicted_horizon_days=signal.horizon_days,
        ),
    )

    return TradeOpenResult(
        signal_id=signal_id, paper_trade_id=trade_id, prediction_id=prediction_id
    )


def compute_trade_pnl(
    *,
    entry_price: float,
    exit_price: float,
    qty: int,
) -> tuple[float, float]:
    """Return `(pnl_abs, pnl_pct)`. `pnl_pct` is in percent units."""
    pnl_abs = (exit_price - entry_price) * qty
    pnl_pct = (exit_price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
    return pnl_abs, pnl_pct


def close_with_exit(
    conn: sqlite3.Connection,
    trade_id: int,
    *,
    exit_ts: datetime | str,
    exit_price: float,
    exit_reason: ExitReason,
    days_held: int,
) -> PaperTrade:
    """Close a trade and persist computed P&L. Returns the closed row.

    Raises `LookupError` if `trade_id` doesn't exist — protects the caller
    from a silent UPDATE of zero rows when, e.g., MTM is run twice and the
    trade is already closed.
    """
    trade = get_paper_trade(conn, trade_id)
    if trade is None:
        raise LookupError(f"paper_trade id={trade_id} not found")
    if trade.ts_exit is not None:
        raise ValueError(
            f"paper_trade id={trade_id} already closed at {trade.ts_exit}"
        )

    pnl_abs, pnl_pct = compute_trade_pnl(
        entry_price=trade.entry_price, exit_price=exit_price, qty=trade.qty
    )

    close_paper_trade(
        conn,
        trade_id,
        ts_exit=_isoformat(exit_ts),
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl=pnl_abs,
        pnl_pct=pnl_pct,
        days_held=days_held,
    )
    closed = get_paper_trade(conn, trade_id)
    assert closed is not None
    return closed


def open_trades(conn: sqlite3.Connection) -> list[PaperTrade]:
    """All trades with `ts_exit IS NULL`. Pass-through to repo for ergonomics."""
    return list_open_paper_trades(conn)


def days_between(entry_ts: str, exit_ts: datetime | str) -> int:
    """Trading-day-agnostic day count between two ISO timestamps.

    Used by MTM to compute `days_held` at close. We use calendar days
    instead of trading days because the time-stop branch in
    `strategy.exits` is defined in trading days at the caller; this
    helper only matters for the closed-trade summary stat.
    """
    entry_dt = datetime.fromisoformat(entry_ts)
    exit_dt = exit_ts if isinstance(exit_ts, datetime) else datetime.fromisoformat(exit_ts)
    delta: timedelta = exit_dt - entry_dt
    return max(0, delta.days)
