"""Paper portfolio positions — per-symbol holdings + a portfolio summary.

Pure aggregation over `paper_trades` (open lots) and `portfolio_snapshots`
(offline marks). No live/network price source — LTP is the latest snapshot's
close, with deliberate fallbacks so the view never NULLs or crashes:

  * LTP missing for a symbol  → falls back to weighted avg entry (P&L = 0).
  * prev_close missing/<2 snaps → falls back to LTP (today's P&L = 0).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from trading.paper.funds import total_funds_added
from trading.paper.reconcile import INITIAL_CAPITAL, compute_paper_cash


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg: float
    invested: float
    ltp: float
    current_value: float
    pnl: float
    pnl_pct: float
    today_pnl: float


@dataclass(frozen=True)
class PortfolioSummary:
    invested: float
    current_value: float
    total_pnl: float
    total_pnl_pct: float
    today_pnl: float
    realised_pnl: float
    unrealised_pnl: float
    cash: float
    funds_added: float
    account_value: float
    as_of_mark: str | None


def _marks(conn: sqlite3.Connection) -> tuple[dict[str, float], dict[str, float], str | None]:
    """Return (latest per-share marks, prev per-share marks, latest date).

    Each map is `{symbol: value/qty}` parsed from a snapshot's holdings_json.
    `latest_date` is None when no snapshots exist.
    """
    rows = conn.execute(
        "SELECT date, holdings_json FROM portfolio_snapshots ORDER BY date DESC LIMIT 2"
    ).fetchall()

    def per_share(blob: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym, h in json.loads(blob).items():
            qty = float(h.get("qty") or 0.0)
            if qty:
                out[sym] = float(h.get("value") or 0.0) / qty
        return out

    if not rows:
        return {}, {}, None
    latest = per_share(rows[0]["holdings_json"])
    prev = per_share(rows[1]["holdings_json"]) if len(rows) > 1 else {}
    return latest, prev, str(rows[0]["date"])


def deployed_by_symbol(conn: sqlite3.Connection) -> dict[str, float]:
    """Cost-basis value of open paper positions, grouped by symbol."""
    rows = conn.execute(
        "SELECT s.symbol AS symbol, SUM(pt.entry_price * pt.qty) AS deployed "
        "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL GROUP BY s.symbol"
    ).fetchall()
    return {r["symbol"]: float(r["deployed"]) for r in rows}


def open_lots_by_symbol(conn: sqlite3.Connection) -> dict[str, int]:
    """Count of open paper lots (not notional) grouped by symbol.

    Feeds the F-048 per-symbol/per-sector concentration caps in the daily planner:
    `deployed_by_symbol` answers *how much* is in a name, this answers *how many
    lots*, which is what the lot caps gate on.
    """
    rows = conn.execute(
        "SELECT s.symbol AS symbol, COUNT(*) AS lots "
        "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL GROUP BY s.symbol"
    ).fetchall()
    return {r["symbol"]: int(r["lots"]) for r in rows}


def already_opened_today(conn: sqlite3.Connection, symbol: str, as_of: date) -> bool:
    """True if `symbol` has an OPEN paper-trade entered on `as_of`."""
    row = conn.execute(
        "SELECT 1 FROM paper_trades pt "
        "JOIN signals s ON s.id = pt.signal_id "
        "WHERE s.symbol = ? AND substr(pt.ts_entry, 1, 10) = ? "
        "  AND pt.ts_exit IS NULL "
        "LIMIT 1",
        (symbol, as_of.isoformat()),
    ).fetchone()
    return row is not None


def compute_positions(conn: sqlite3.Connection, *, as_of: date) -> list[Position]:
    """Per-symbol open holdings as of `as_of`, sorted by current value desc."""
    rows = conn.execute(
        """SELECT s.symbol AS symbol, pt.entry_price AS entry_price, pt.qty AS qty
             FROM paper_trades pt
             JOIN signals s ON s.id = pt.signal_id
            WHERE pt.ts_exit IS NULL AND date(pt.ts_entry) <= ?""",
        (as_of.isoformat(),),
    ).fetchall()

    agg: dict[str, dict[str, float]] = {}
    for r in rows:
        a = agg.setdefault(str(r["symbol"]), {"qty": 0.0, "invested": 0.0})
        a["qty"] += float(r["qty"])
        a["invested"] += float(r["entry_price"]) * float(r["qty"])

    latest, prev, _ = _marks(conn)

    positions: list[Position] = []
    for symbol, a in agg.items():
        qty = int(a["qty"])
        invested = a["invested"]
        avg = invested / qty if qty else 0.0
        ltp = latest.get(symbol, avg)
        prev_close = prev.get(symbol, ltp)
        current_value = ltp * qty
        pnl = current_value - invested
        positions.append(
            Position(
                symbol=symbol,
                qty=qty,
                avg=avg,
                invested=invested,
                ltp=ltp,
                current_value=current_value,
                pnl=pnl,
                pnl_pct=(pnl / invested * 100.0) if invested else 0.0,
                today_pnl=qty * (ltp - prev_close),
            )
        )
    positions.sort(key=lambda p: p.current_value, reverse=True)
    return positions


def _realised_pnl(conn: sqlite3.Connection, *, as_of: date, same_day_only: bool = False) -> float:
    """Net realised P&L (cost-inclusive) for closed trades, as of `as_of`.

    Reuses `paper_trades.pnl`, which `ledger.close_with_exit` already persists
    net of round-trip costs at close time (F-025) — this is the same number
    `compute_paper_cash` credits into cash, so realised P&L here can never
    drift out of sync with the account-value tile (F-059).

    `same_day_only=True` restricts to trades closed exactly on `as_of` (feeds
    the "Today's P&L" tile); otherwise every close with `ts_exit <= as_of` is
    summed (cumulative, feeds "Total P&L").
    """
    as_of_iso = as_of.isoformat()
    clause = "date(pt.ts_exit) = ?" if same_day_only else "date(pt.ts_exit) <= ?"
    row = conn.execute(
        f"SELECT COALESCE(SUM(pt.pnl), 0.0) AS total FROM paper_trades pt "
        f"WHERE pt.ts_exit IS NOT NULL AND pt.pnl IS NOT NULL AND {clause}",
        (as_of_iso,),
    ).fetchone()
    return float(row["total"]) if row is not None else 0.0


def compute_summary(
    conn: sqlite3.Connection,
    *,
    as_of: date,
    initial_capital: float = INITIAL_CAPITAL,
) -> PortfolioSummary:
    """Aggregate the positions and fold in cash + funds for the summary tiles.

    `total_pnl` / `today_pnl` are realised + unrealised together (F-059): the
    open-only figure used to be the entirety of "Total P&L", so it silently
    dropped every closed trade's contribution the instant it closed. Realised
    P&L is summed straight from `paper_trades.pnl` (the same net-of-costs
    number `compute_paper_cash` already credits into cash/account_value), so
    there's no second source of truth for what a closed trade was worth.
    """
    positions = compute_positions(conn, as_of=as_of)
    invested = sum(p.invested for p in positions)
    current_value = sum(p.current_value for p in positions)
    unrealised_pnl = current_value - invested
    unrealised_today_pnl = sum(p.today_pnl for p in positions)
    realised_pnl = _realised_pnl(conn, as_of=as_of)
    realised_today_pnl = _realised_pnl(conn, as_of=as_of, same_day_only=True)
    total_pnl = realised_pnl + unrealised_pnl
    today_pnl = realised_today_pnl + unrealised_today_pnl
    cash = compute_paper_cash(conn, as_of=as_of, initial_capital=initial_capital)
    funds_added = total_funds_added(conn, as_of=as_of)
    capital_base = initial_capital + funds_added
    _, _, as_of_mark = _marks(conn)
    return PortfolioSummary(
        invested=invested,
        current_value=current_value,
        total_pnl=total_pnl,
        total_pnl_pct=(total_pnl / capital_base * 100.0) if capital_base else 0.0,
        today_pnl=today_pnl,
        realised_pnl=realised_pnl,
        unrealised_pnl=unrealised_pnl,
        cash=cash,
        funds_added=funds_added,
        account_value=cash + current_value,
        as_of_mark=as_of_mark,
    )
