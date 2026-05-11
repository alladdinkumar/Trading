"""Typed CRUD helpers for signals, paper_trades, and predictions.

Plain `@dataclass(frozen=True)` types — Pydantic is overkill here since we
control both sides of the boundary. Callers should always go through these
helpers rather than writing raw SQL, so column ordering and CHECK constraints
stay encapsulated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

Side = Literal["LONG", "SHORT"]
Conviction = Literal["HIGH", "MEDIUM", "LOW"]
ExitReason = Literal["TARGET", "STOP", "TIME", "MANUAL"]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """A strategy-generated trade candidate.

    `id` is None before insert, populated on read.
    """

    id: int | None
    ts: str
    symbol: str
    side: Side
    entry: float
    stop: float
    target: float
    horizon_days: int
    rules_passed_json: str | None = None
    ml_score: float | None = None
    conviction: Conviction | None = None
    rationale: str | None = None
    created_by: str = "auto"


@dataclass(frozen=True)
class PaperTrade:
    """A paper-traded position. `ts_exit` is None while the trade is open."""

    id: int | None
    signal_id: int
    ts_entry: str
    entry_price: float
    qty: int
    ts_exit: str | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    days_held: int | None = None


@dataclass(frozen=True)
class Prediction:
    """A return/horizon prediction logged at signal time, evaluated later."""

    id: int | None
    ts: str
    symbol: str
    predicted_return_pct: float
    predicted_horizon_days: int
    actual_return_at_horizon: float | None = None
    error_pct: float | None = None
    evaluated_at: str | None = None


# ---------------------------------------------------------------------------
# Row → dataclass helpers
# ---------------------------------------------------------------------------


def _row_to_signal(row: sqlite3.Row) -> Signal:
    return Signal(
        id=row["id"],
        ts=row["ts"],
        symbol=row["symbol"],
        side=row["side"],
        entry=row["entry"],
        stop=row["stop"],
        target=row["target"],
        horizon_days=row["horizon_days"],
        rules_passed_json=row["rules_passed_json"],
        ml_score=row["ml_score"],
        conviction=row["conviction"],
        rationale=row["rationale"],
        created_by=row["created_by"],
    )


def _row_to_paper_trade(row: sqlite3.Row) -> PaperTrade:
    return PaperTrade(
        id=row["id"],
        signal_id=row["signal_id"],
        ts_entry=row["ts_entry"],
        entry_price=row["entry_price"],
        qty=row["qty"],
        ts_exit=row["ts_exit"],
        exit_price=row["exit_price"],
        exit_reason=row["exit_reason"],
        pnl=row["pnl"],
        pnl_pct=row["pnl_pct"],
        days_held=row["days_held"],
    )


def _row_to_prediction(row: sqlite3.Row) -> Prediction:
    return Prediction(
        id=row["id"],
        ts=row["ts"],
        symbol=row["symbol"],
        predicted_return_pct=row["predicted_return_pct"],
        predicted_horizon_days=row["predicted_horizon_days"],
        actual_return_at_horizon=row["actual_return_at_horizon"],
        error_pct=row["error_pct"],
        evaluated_at=row["evaluated_at"],
    )


# ---------------------------------------------------------------------------
# Signal CRUD
# ---------------------------------------------------------------------------


def insert_signal(conn: sqlite3.Connection, sig: Signal) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
          ts, symbol, side, entry, stop, target, horizon_days,
          rules_passed_json, ml_score, conviction, rationale, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sig.ts,
            sig.symbol,
            sig.side,
            sig.entry,
            sig.stop,
            sig.target,
            sig.horizon_days,
            sig.rules_passed_json,
            sig.ml_score,
            sig.conviction,
            sig.rationale,
            sig.created_by,
        ),
    )
    rowid = cur.lastrowid
    assert rowid is not None
    return rowid


def get_signal(conn: sqlite3.Connection, signal_id: int) -> Signal | None:
    row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return _row_to_signal(row) if row else None


def list_signals_by_date(conn: sqlite3.Connection, date_iso: str) -> list[Signal]:
    """List signals whose `ts` starts with `date_iso` (YYYY-MM-DD)."""
    rows = conn.execute(
        "SELECT * FROM signals WHERE ts LIKE ? ORDER BY ts",
        (f"{date_iso}%",),
    ).fetchall()
    return [_row_to_signal(r) for r in rows]


# ---------------------------------------------------------------------------
# PaperTrade CRUD
# ---------------------------------------------------------------------------


def insert_paper_trade(conn: sqlite3.Connection, trade: PaperTrade) -> int:
    cur = conn.execute(
        """
        INSERT INTO paper_trades (
          signal_id, ts_entry, entry_price, qty,
          ts_exit, exit_price, exit_reason, pnl, pnl_pct, days_held
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade.signal_id,
            trade.ts_entry,
            trade.entry_price,
            trade.qty,
            trade.ts_exit,
            trade.exit_price,
            trade.exit_reason,
            trade.pnl,
            trade.pnl_pct,
            trade.days_held,
        ),
    )
    rowid = cur.lastrowid
    assert rowid is not None
    return rowid


def close_paper_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    *,
    ts_exit: str,
    exit_price: float,
    exit_reason: ExitReason,
    pnl: float,
    pnl_pct: float,
    days_held: int,
) -> None:
    conn.execute(
        """
        UPDATE paper_trades
           SET ts_exit = ?, exit_price = ?, exit_reason = ?,
               pnl = ?, pnl_pct = ?, days_held = ?
         WHERE id = ?
        """,
        (ts_exit, exit_price, exit_reason, pnl, pnl_pct, days_held, trade_id),
    )


def get_paper_trade(conn: sqlite3.Connection, trade_id: int) -> PaperTrade | None:
    row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
    return _row_to_paper_trade(row) if row else None


def list_open_paper_trades(conn: sqlite3.Connection) -> list[PaperTrade]:
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE ts_exit IS NULL ORDER BY ts_entry"
    ).fetchall()
    return [_row_to_paper_trade(r) for r in rows]


# ---------------------------------------------------------------------------
# Prediction CRUD
# ---------------------------------------------------------------------------


def insert_prediction(conn: sqlite3.Connection, pred: Prediction) -> int:
    cur = conn.execute(
        """
        INSERT INTO predictions (
          ts, symbol, predicted_return_pct, predicted_horizon_days,
          actual_return_at_horizon, error_pct, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pred.ts,
            pred.symbol,
            pred.predicted_return_pct,
            pred.predicted_horizon_days,
            pred.actual_return_at_horizon,
            pred.error_pct,
            pred.evaluated_at,
        ),
    )
    rowid = cur.lastrowid
    assert rowid is not None
    return rowid


def get_prediction(conn: sqlite3.Connection, pred_id: int) -> Prediction | None:
    row = conn.execute("SELECT * FROM predictions WHERE id = ?", (pred_id,)).fetchone()
    return _row_to_prediction(row) if row else None


def list_predictions_by_symbol(conn: sqlite3.Connection, symbol: str) -> list[Prediction]:
    rows = conn.execute(
        "SELECT * FROM predictions WHERE symbol = ? ORDER BY ts",
        (symbol,),
    ).fetchall()
    return [_row_to_prediction(r) for r in rows]
