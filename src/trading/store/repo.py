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
    """A paper-traded position. `ts_exit` is None while the trade is open.

    `current_stop` and `atr_at_entry` (added in schema v2) carry the
    trailing-stop state across MTM runs; `current_stop` initialises to
    the signal's stop at trade open and ratchets up via exits.evaluate_exit.
    """

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
    current_stop: float | None = None
    atr_at_entry: float | None = None


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
    # F-040 entry-condition snapshot — attribute a matured outcome to *why* it opened.
    regime: str | None = None
    sector: str | None = None
    ml_score: float | None = None
    conviction: Conviction | None = None
    atr_pct: float | None = None
    neg_news_7d: int | None = None


@dataclass(frozen=True)
class EntryAttribution:
    """Entry-condition snapshot threaded into the prediction at trade-open (F-040).

    `ml_score`, `conviction` and `atr_pct` are derived from the signal/fill inside
    `log_signal_and_open_trade`; only the context fields the ledger can't see —
    market `regime`, the symbol's `sector`, and the trailing negative-news count —
    are supplied here.
    """

    regime: str | None = None
    sector: str | None = None
    neg_news_7d: int | None = None


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
    keys = row.keys()
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
        current_stop=row["current_stop"] if "current_stop" in keys else None,
        atr_at_entry=row["atr_at_entry"] if "atr_at_entry" in keys else None,
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
        # Always present post-migration-v6 (SELECT * callers); NULL for pre-v6 rows.
        regime=row["regime"],
        sector=row["sector"],
        ml_score=row["ml_score"],
        conviction=row["conviction"],
        atr_pct=row["atr_pct"],
        neg_news_7d=row["neg_news_7d"],
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
          ts_exit, exit_price, exit_reason, pnl, pnl_pct, days_held,
          current_stop, atr_at_entry
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            trade.current_stop,
            trade.atr_at_entry,
        ),
    )
    rowid = cur.lastrowid
    assert rowid is not None
    return rowid


def update_trailing_state(
    conn: sqlite3.Connection,
    trade_id: int,
    *,
    current_stop: float,
    days_held: int,
) -> None:
    """Bump trailing stop + days_held without closing the trade.

    Called by MTM when `evaluate_exit` returns HOLD with a ratcheted stop.
    """
    conn.execute(
        "UPDATE paper_trades SET current_stop = ?, days_held = ? WHERE id = ?",
        (current_stop, days_held, trade_id),
    )


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
          actual_return_at_horizon, error_pct, evaluated_at,
          regime, sector, ml_score, conviction, atr_pct, neg_news_7d
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pred.ts,
            pred.symbol,
            pred.predicted_return_pct,
            pred.predicted_horizon_days,
            pred.actual_return_at_horizon,
            pred.error_pct,
            pred.evaluated_at,
            pred.regime,
            pred.sector,
            pred.ml_score,
            pred.conviction,
            pred.atr_pct,
            pred.neg_news_7d,
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


def matured_score_outcomes(conn: sqlite3.Connection) -> list[tuple[float, bool]]:
    """`(ml_score, won)` pairs for every matured, ml-scored prediction (F-041).

    Feeds `strategy.calibration.build_score_calibration` so the planner can
    correct `p_win` with realised win-rate. `won` is `actual_return_at_horizon > 0`.
    """
    rows = conn.execute(
        "SELECT ml_score, actual_return_at_horizon FROM predictions "
        "WHERE evaluated_at IS NOT NULL AND ml_score IS NOT NULL "
        "AND actual_return_at_horizon IS NOT NULL"
    ).fetchall()
    return [(float(r["ml_score"]), float(r["actual_return_at_horizon"]) > 0) for r in rows]


# ---------------------------------------------------------------------------
# Ranker eval log (F-044/F-046)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankerEval:
    """A dated weekly verdict on whether the shadow ranker is usable yet."""

    as_of: str
    pooled_sharpe: float | None
    pooled_hit: float | None
    n_oos: int
    n_folds_pos: int
    n_folds_total: int
    usable: bool
    note: str | None
    created_at: str


def insert_ranker_eval(conn: sqlite3.Connection, ev: RankerEval) -> None:
    """Upsert this week's verdict (PK as_of → Sunday re-run is idempotent)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO ranker_eval_log (
          as_of, pooled_sharpe, pooled_hit, n_oos, n_folds_pos,
          n_folds_total, usable, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ev.as_of,
            ev.pooled_sharpe,
            ev.pooled_hit,
            ev.n_oos,
            ev.n_folds_pos,
            ev.n_folds_total,
            1 if ev.usable else 0,
            ev.note,
            ev.created_at,
        ),
    )


def latest_ranker_eval(conn: sqlite3.Connection) -> RankerEval | None:
    """The most recent weekly verdict, or None if the log is empty. Feeds the
    G3 persistence gate (promote only after 2 consecutive usable verdicts)."""
    row = conn.execute(
        "SELECT * FROM ranker_eval_log ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return RankerEval(
        as_of=row["as_of"],
        pooled_sharpe=row["pooled_sharpe"],
        pooled_hit=row["pooled_hit"],
        n_oos=int(row["n_oos"]),
        n_folds_pos=int(row["n_folds_pos"]),
        n_folds_total=int(row["n_folds_total"]),
        usable=bool(row["usable"]),
        note=row["note"],
        created_at=row["created_at"],
    )
