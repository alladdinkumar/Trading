"""Daily reconciliation — predictions accuracy + portfolio snapshot (spec §9.4).

Two things happen at post-close each day:

  1. Predictions whose horizon has matured (or whose corresponding trade
     has closed) are filled in with the actual return and error vs
     prediction.
  2. The paper portfolio's cash + open-position values are snapshotted
     into `portfolio_snapshots` for the equity curve and drawdown plot.

Both are pure-ish: they read state and write summary rows. No external
data fetching here — the caller provides today's bar map (same one MTM
uses) so equity is computed against a single consistent quote source.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from trading.paper.funds import total_funds_added
from trading.paper.ledger import buy_side_cost, open_trades, sell_side_cost
from trading.strategy.exits import Bar

# Starting paper capital. The *live* cash balance is not this constant — it is
# derived from the trade ledger (see `compute_paper_cash`); this is only the
# t=0 seed before any trade is opened.
INITIAL_CAPITAL = 100_000.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    """One row written to `portfolio_snapshots`."""

    date: str
    cash: float
    equity: float
    drawdown_pct: float | None
    holdings_json: str


@dataclass(frozen=True)
class PredictionUpdate:
    """One matured-prediction update for the daily log."""

    prediction_id: int
    symbol: str
    predicted_return_pct: float
    actual_return_pct: float
    error_pct: float


# ---------------------------------------------------------------------------
# Predictions — evaluate matured horizons
# ---------------------------------------------------------------------------


def evaluate_matured_predictions(
    conn: sqlite3.Connection,
    *,
    as_of: date,
    bars: dict[str, Bar],
) -> list[PredictionUpdate]:
    """Fill `actual_return_at_horizon` on predictions whose horizon has elapsed.

    A prediction `matures` when either:
      - `as_of` is ≥ `ts + horizon_days` (so we have enough days), OR
      - the paired paper_trade has closed (use its exit_price / pnl).

    For the bar-based path we use `bars[symbol].close` as the realised
    price at horizon. Predictions with no bar in `bars` and no closed
    trade are left untouched for the next day's reconcile.
    """
    rows = conn.execute(
        """SELECT id, ts, symbol, predicted_return_pct, predicted_horizon_days
           FROM predictions
           WHERE actual_return_at_horizon IS NULL"""
    ).fetchall()

    updates: list[PredictionUpdate] = []
    now_iso = datetime.combine(as_of, datetime.min.time()).isoformat()

    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        horizon_end = ts.date() + timedelta(days=int(r["predicted_horizon_days"]))

        # Find the matching paper_trade by joining via signals.symbol + ts.
        # Predictions are emitted at trade-open so signals.ts == predictions.ts
        # for auto-logged signals.
        trade_row = conn.execute(
            """SELECT pt.entry_price, pt.exit_price, pt.ts_exit
               FROM paper_trades pt
               JOIN signals s ON s.id = pt.signal_id
               WHERE s.symbol = ? AND pt.ts_entry = ?
               ORDER BY pt.id DESC LIMIT 1""",
            (r["symbol"], r["ts"]),
        ).fetchone()

        actual_pct: float | None = None
        if trade_row and trade_row["exit_price"] is not None:
            actual_pct = (
                (trade_row["exit_price"] - trade_row["entry_price"])
                / trade_row["entry_price"]
                * 100.0
                if trade_row["entry_price"] > 0
                else 0.0
            )
        elif as_of >= horizon_end and r["symbol"] in bars:
            bar = bars[r["symbol"]]
            if trade_row and trade_row["entry_price"]:
                entry = trade_row["entry_price"]
            else:
                continue  # no entry price to compare against
            actual_pct = (bar.close - entry) / entry * 100.0
        else:
            continue  # not yet matured

        error_pct = actual_pct - float(r["predicted_return_pct"])
        conn.execute(
            """UPDATE predictions
               SET actual_return_at_horizon = ?, error_pct = ?, evaluated_at = ?
               WHERE id = ?""",
            (actual_pct, error_pct, now_iso, r["id"]),
        )
        updates.append(
            PredictionUpdate(
                prediction_id=int(r["id"]),
                symbol=str(r["symbol"]),
                predicted_return_pct=float(r["predicted_return_pct"]),
                actual_return_pct=actual_pct,
                error_pct=error_pct,
            )
        )
    return updates


# ---------------------------------------------------------------------------
# Paper-cash ledger — cash derived from the trade history (F-023)
# ---------------------------------------------------------------------------


def compute_paper_cash(
    conn: sqlite3.Connection,
    *,
    as_of: date,
    initial_capital: float = INITIAL_CAPITAL,
) -> float:
    """Paper-cash balance derived from the trade ledger as of `as_of`.

    The seed is `initial_capital` plus any capital top-ups recorded in
    `cash_ledger` with `date <= as_of` (see `trading.paper.funds`); an empty
    ledger contributes 0.0, so behaviour is unchanged from before funds
    tracking existed.

    Mirrors the backtest engine's cash handling: opening a trade debits
    `entry_price × qty`, closing it credits `exit_price × qty`. Cash is a
    pure function of `paper_trades`, so realised P&L compounds into the
    equity curve instead of vanishing (the F-023 bug, where `cash` was a
    caller-constant and closed-trade gains never reached the snapshot).

    Trades are filtered by date so re-running an *old* `as_of` reproduces
    the balance as it stood that day (future opens/closes are excluded).

    Buy/sell costs (Zerodha charges + slippage) are applied per fill via the
    backtest cost model (F-025): opening debits `entry_value + buy_side_cost`,
    closing credits `exit_value − sell_side_cost`, so the equity curve carries
    the same friction as the backtest. Costs are per-row (the ₹20 brokerage cap
    and GST make them non-linear), so we iterate rather than SUM in SQL.
    """
    as_of_iso = as_of.isoformat()
    cash = initial_capital + total_funds_added(conn, as_of=as_of)

    open_rows = conn.execute(
        """SELECT entry_price, qty FROM paper_trades
           WHERE date(ts_entry) <= ?""",
        (as_of_iso,),
    ).fetchall()
    for row in open_rows:
        entry_value = float(row["entry_price"]) * row["qty"]
        cash -= entry_value + buy_side_cost(entry_value)

    closed_rows = conn.execute(
        """SELECT exit_price, qty FROM paper_trades
           WHERE ts_exit IS NOT NULL AND exit_price IS NOT NULL
             AND date(ts_exit) <= ?""",
        (as_of_iso,),
    ).fetchall()
    for row in closed_rows:
        exit_value = float(row["exit_price"]) * row["qty"]
        cash += exit_value - sell_side_cost(exit_value)

    return cash


# ---------------------------------------------------------------------------
# Portfolio snapshot — cash + open-position MTM value
# ---------------------------------------------------------------------------


def compute_portfolio_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of: date,
    bars: dict[str, Bar],
    initial_capital: float = INITIAL_CAPITAL,
) -> PortfolioSnapshot:
    """Build today's `portfolio_snapshots` row.

    Cash is derived from the trade ledger via `compute_paper_cash`
    (`initial_capital` minus net deployed capital, plus realised proceeds),
    so closing a winner raises cash and the equity curve compounds (F-023).
    Equity = cash + sum(qty * close) over open positions; symbols missing
    from `bars` use last_traded entry_price as a fallback so equity is never
    NULL.

    Drawdown computed by comparing today's equity to the peak in
    `portfolio_snapshots` so far. Returns None on the very first snapshot.
    """
    cash = compute_paper_cash(conn, as_of=as_of, initial_capital=initial_capital)
    open_pts = open_trades(conn)
    by_symbol: dict[str, dict[str, float]] = {}
    for trade in open_pts:
        sym_row = conn.execute(
            "SELECT symbol FROM signals WHERE id = ?", (trade.signal_id,)
        ).fetchone()
        if sym_row is None:
            continue
        symbol = sym_row["symbol"]
        mark_price = bars[symbol].close if symbol in bars else trade.entry_price
        value = mark_price * trade.qty
        h = by_symbol.setdefault(symbol, {"qty": 0.0, "value": 0.0})
        h["qty"] += trade.qty
        h["value"] += value

    equity = cash + sum(h["value"] for h in by_symbol.values())

    prior_peak = conn.execute("SELECT MAX(equity) AS peak FROM portfolio_snapshots").fetchone()
    peak = float(prior_peak["peak"]) if prior_peak and prior_peak["peak"] is not None else None
    if peak is None or equity > peak:
        drawdown_pct: float | None = 0.0 if peak is not None else None
    else:
        drawdown_pct = (peak - equity) / peak * 100.0 if peak > 0 else 0.0

    return PortfolioSnapshot(
        date=as_of.isoformat(),
        cash=cash,
        equity=equity,
        drawdown_pct=drawdown_pct,
        holdings_json=json.dumps(by_symbol, sort_keys=True),
    )


def upsert_portfolio_snapshot(conn: sqlite3.Connection, snap: PortfolioSnapshot) -> None:
    """Insert-or-replace one daily snapshot (`date` is the primary key)."""
    conn.execute(
        """
        INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity, drawdown_pct)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
          cash = excluded.cash,
          holdings_json = excluded.holdings_json,
          equity = excluded.equity,
          drawdown_pct = excluded.drawdown_pct
        """,
        (snap.date, snap.cash, snap.holdings_json, snap.equity, snap.drawdown_pct),
    )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileResult:
    """Aggregate output of a daily reconcile pass — what the CLI surfaces."""

    snapshot: PortfolioSnapshot
    prediction_updates: list[PredictionUpdate]


def reconcile_day(
    conn: sqlite3.Connection,
    *,
    as_of: date,
    bars: dict[str, Bar],
    initial_capital: float = INITIAL_CAPITAL,
) -> ReconcileResult:
    """Run the matured-predictions update and write the portfolio snapshot.

    `initial_capital` seeds the paper-cash ledger; the snapshot's actual cash
    and equity are derived from the trade history (F-023), not this constant.
    """
    updates = evaluate_matured_predictions(conn, as_of=as_of, bars=bars)
    snap = compute_portfolio_snapshot(conn, as_of=as_of, bars=bars, initial_capital=initial_capital)
    upsert_portfolio_snapshot(conn, snap)
    return ReconcileResult(snapshot=snap, prediction_updates=updates)
