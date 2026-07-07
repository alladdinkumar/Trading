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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from trading.backtest.metrics import gate_sharpe
from trading.costs import buy_side_cost, sell_side_cost
from trading.paper.funds import total_funds_added
from trading.paper.ledger import compute_trade_pnl, open_trades
from trading.strategy.exits import Bar

# Starting paper capital. The *live* cash balance is not this constant — it is
# derived from the trade ledger (see `compute_paper_cash`); this is only the
# t=0 seed before any trade is opened.
INITIAL_CAPITAL = 100_000.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    """One row written to `portfolio_snapshots`.

    `warnings` is transient diagnostics only — it is *not* persisted
    (`upsert_portfolio_snapshot` writes explicit columns) and is excluded from
    equality/repr. It carries F-052 mark-fallback notes (a symbol whose quote
    was missing, marked at its prior close or entry) up to the caller so they
    surface in `post_close_summary.md` rather than distorting the equity/
    drawdown series silently.
    """

    date: str
    cash: float
    equity: float
    drawdown_pct: float | None
    holdings_json: str
    warnings: list[str] = field(default_factory=list, compare=False, repr=False)


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

    `actual_pct` is net of round-trip costs (F-051): it is `compute_trade_pnl`'s
    `pnl_pct`, the same costed yardstick the paper ledger uses for realised P&L,
    rather than the raw price return. This keeps the calibration "won" label
    (`matured_score_outcomes`, `actual_return_at_horizon > 0`) and the weekly
    review hit-rate honest — a gross +0.3% move that round-trip costs
    (~0.4-0.5%) turn into a net loss is no longer counted as a win.
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
            """SELECT pt.entry_price, pt.exit_price, pt.qty, pt.ts_exit
               FROM paper_trades pt
               JOIN signals s ON s.id = pt.signal_id
               WHERE s.symbol = ? AND pt.ts_entry = ?
               ORDER BY pt.id DESC LIMIT 1""",
            (r["symbol"], r["ts"]),
        ).fetchone()

        actual_pct: float | None = None
        if trade_row and trade_row["exit_price"] is not None:
            # F-051: net of round-trip costs, same yardstick as the ledger's
            # realised pnl_pct — not the raw (exit-entry)/entry price move.
            _, actual_pct = compute_trade_pnl(
                entry_price=trade_row["entry_price"],
                exit_price=trade_row["exit_price"],
                qty=trade_row["qty"],
            )
        elif as_of >= horizon_end and r["symbol"] in bars:
            bar = bars[r["symbol"]]
            if trade_row and trade_row["entry_price"]:
                entry = trade_row["entry_price"]
            else:
                continue  # no entry price to compare against
            # F-051: net of round-trip costs (same yardstick as a closed trade),
            # using bar.close as the synthetic exit price at horizon.
            _, actual_pct = compute_trade_pnl(
                entry_price=entry, exit_price=bar.close, qty=trade_row["qty"]
            )
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

    # F-058 follow-up: substr(col, 1, 10), never SQLite date() — date() converts
    # offset-aware strings to UTC first, shifting IST 00:00–05:29 stamps back a
    # day. The leading YYYY-MM-DD is the local IST date for aware and naive
    # (legacy) rows alike, and compares correctly as a string.
    open_rows = conn.execute(
        """SELECT entry_price, qty FROM paper_trades
           WHERE substr(ts_entry, 1, 10) <= ?""",
        (as_of_iso,),
    ).fetchall()
    for row in open_rows:
        entry_value = float(row["entry_price"]) * row["qty"]
        cash -= entry_value + buy_side_cost(entry_value)

    closed_rows = conn.execute(
        """SELECT exit_price, qty FROM paper_trades
           WHERE ts_exit IS NOT NULL AND exit_price IS NOT NULL
             AND substr(ts_exit, 1, 10) <= ?""",
        (as_of_iso,),
    ).fetchall()
    for row in closed_rows:
        exit_value = float(row["exit_price"]) * row["qty"]
        cash += exit_value - sell_side_cost(exit_value)

    return cash


# ---------------------------------------------------------------------------
# Portfolio snapshot — cash + open-position MTM value
# ---------------------------------------------------------------------------


def _prior_per_share_marks(conn: sqlite3.Connection, *, as_of: date) -> dict[str, float]:
    """Per-share marks from the latest snapshot strictly *before* `as_of`.

    Parsed as `{symbol: value/qty}` from that snapshot's `holdings_json` — the
    same shape `positions._marks` reads. Used as the F-052 fallback when a held
    symbol has no bar today: its last known close is a truer mark than snapping
    back to cost basis. Empty when no earlier snapshot exists.

    Day-gating uses `date < ?` on the ISO `date` primary key (a pure YYYY-MM-DD
    string), so no `substr`/`date()` concern applies here.
    """
    row = conn.execute(
        "SELECT holdings_json FROM portfolio_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
        (as_of.isoformat(),),
    ).fetchone()
    if row is None:
        return {}
    out: dict[str, float] = {}
    for sym, h in json.loads(row["holdings_json"]).items():
        qty = float(h.get("qty") or 0.0)
        if qty:
            out[sym] = float(h.get("value") or 0.0) / qty
    return out


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
    Equity = cash + sum(qty * close) over open positions.

    F-052 — missing-quote fallback. A symbol absent from `bars` is *not* snapped
    to `entry_price` (cost basis): that would flatten a live winner/loser back to
    break-even and silently distort the persisted equity/drawdown series the gate
    Sharpe (F-061) reads back. Instead we mark it at the **prior snapshot's
    per-share close** (its last known price) and append a warning naming the
    symbol. Only when no prior mark exists do we fall back to entry price, and the
    warning flags that row as an estimate. Warnings ride on the returned snapshot
    (transient, not persisted) so they surface in `post_close_summary.md`.

    Drawdown computed by comparing today's equity to the peak in
    `portfolio_snapshots` so far. Returns None on the very first snapshot.
    """
    cash = compute_paper_cash(conn, as_of=as_of, initial_capital=initial_capital)
    prior_marks = _prior_per_share_marks(conn, as_of=as_of)
    open_pts = open_trades(conn)
    by_symbol: dict[str, dict[str, float]] = {}
    warnings: list[str] = []
    for trade in open_pts:
        sym_row = conn.execute(
            "SELECT symbol FROM signals WHERE id = ?", (trade.signal_id,)
        ).fetchone()
        if sym_row is None:
            continue
        symbol = sym_row["symbol"]
        if symbol in bars:
            mark_price = bars[symbol].close
        elif symbol in prior_marks:
            mark_price = prior_marks[symbol]
            warnings.append(
                f"{symbol}: no quote today — marked at prior close ₹{mark_price:,.2f}"
            )
        else:
            mark_price = trade.entry_price
            warnings.append(
                f"{symbol}: no quote and no prior mark — estimated at entry price "
                f"₹{mark_price:,.2f}"
            )
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
        warnings=warnings,
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
# Gate Sharpe (F-061) — the go-live metric, read off the live paper equity
# ---------------------------------------------------------------------------


def equity_series(conn: sqlite3.Connection) -> pd.Series:
    """Full `portfolio_snapshots.equity` history, oldest first.

    Index is the ISO date string (matching the table's primary key); empty
    when no snapshots exist yet. Pure read — no marks, no derived cash.
    """
    rows = conn.execute("SELECT date, equity FROM portfolio_snapshots ORDER BY date").fetchall()
    return pd.Series(
        [float(r["equity"]) for r in rows],
        index=[str(r["date"]) for r in rows],
        dtype=float,
    )


def portfolio_gate_sharpe(conn: sqlite3.Connection) -> float | None:
    """Daily-annualised Sharpe of the live paper equity curve (F-061).

    This is the actual go-live-gate metric ("≥3 months OOS Sharpe > 1.0" on
    daily-annualised returns) — reads `portfolio_snapshots.equity` and
    delegates to `backtest.metrics.gate_sharpe`, which itself delegates to
    `metrics.sharpe(..., periods_per_year=252)`. Returns `None` (render as
    "n/a") when there isn't yet enough snapshot history to measure it.
    """
    return gate_sharpe(equity_series(conn))


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
