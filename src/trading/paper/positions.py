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

from trading.costs import sell_side_cost
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


def _per_share(blob: str) -> dict[str, float]:
    """Parse a snapshot's holdings_json into `{symbol: value/qty}` marks."""
    out: dict[str, float] = {}
    for sym, h in json.loads(blob).items():
        qty = float(h.get("qty") or 0.0)
        if qty:
            out[sym] = float(h.get("value") or 0.0) / qty
    return out


def _marks(conn: sqlite3.Connection) -> tuple[dict[str, float], dict[str, float], str | None]:
    """Return (latest per-share marks, prev per-share marks, latest date).

    Each map is `{symbol: value/qty}` parsed from a snapshot's holdings_json.
    `latest_date` is None when no snapshots exist.
    """
    rows = conn.execute(
        "SELECT date, holdings_json FROM portfolio_snapshots ORDER BY date DESC LIMIT 2"
    ).fetchall()
    if not rows:
        return {}, {}, None
    latest = _per_share(rows[0]["holdings_json"])
    prev = _per_share(rows[1]["holdings_json"]) if len(rows) > 1 else {}
    return latest, prev, str(rows[0]["date"])


def _prev_close_marks(conn: sqlite3.Connection, *, as_of: date) -> dict[str, float]:
    """Per-share marks from the latest snapshot strictly *before* `as_of`.

    Date-aware on purpose (unlike `_marks`, which is positional): the realised
    leg of "Today's P&L" needs yesterday's close regardless of whether today's
    post-close snapshot has been written yet.
    """
    row = conn.execute(
        "SELECT holdings_json FROM portfolio_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
        (as_of.isoformat(),),
    ).fetchone()
    return _per_share(row["holdings_json"]) if row else {}


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
    """Per-symbol open holdings as of `as_of`, sorted by current value desc.

    Exit-date-aware (F-059 follow-up): a trade whose `ts_exit` is *after*
    `as_of` was still held on `as_of`, so a backdated view keeps it — matching
    `compute_paper_cash`, which only credits exits with `date(ts_exit) <=
    as_of`. (Previously any set `ts_exit` dropped the lot, so a backdated
    account_value lost the position's value while cash had already paid for it.)
    """
    rows = conn.execute(
        """SELECT s.symbol AS symbol, pt.entry_price AS entry_price, pt.qty AS qty
             FROM paper_trades pt
             JOIN signals s ON s.id = pt.signal_id
            WHERE (pt.ts_exit IS NULL OR date(pt.ts_exit) > ?)
              AND date(pt.ts_entry) <= ?""",
        (as_of.isoformat(), as_of.isoformat()),
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


def _realised_pnl(conn: sqlite3.Connection, *, as_of: date) -> float:
    """Cumulative net realised P&L (cost-inclusive) for trades closed by `as_of`.

    Reuses `paper_trades.pnl`, which `ledger.close_with_exit` already persists
    net of round-trip costs at close time (F-025) — this is the same number
    `compute_paper_cash` credits into cash, so realised P&L here can never
    drift out of sync with the account-value tile (F-059).
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(pt.pnl), 0.0) AS total FROM paper_trades pt "
        "WHERE pt.ts_exit IS NOT NULL AND pt.pnl IS NOT NULL AND date(pt.ts_exit) <= ?",
        (as_of.isoformat(),),
    ).fetchone()
    return float(row["total"]) if row is not None else 0.0


def _realised_today_pnl(conn: sqlite3.Connection, *, as_of: date) -> float:
    """Day-scoped realised P&L for trades closed exactly on `as_of`.

    A multi-day trade that exits today must contribute only its move since
    yesterday's mark — `qty × (exit − prev_close) − sell-side costs` — to the
    "Today's P&L" tile, mirroring how the open-position leg computes
    `qty × (ltp − prev_close)`. Dumping the trade's full lifetime `pnl` into
    one day's tile would overstate the day by every prior day's movement
    (F-059 follow-up).

    Fallbacks, per trade:
      * opened the same day → the full net `pnl` (the whole trade lived today);
      * no prev-close mark for the symbol → 0, mirroring the open leg's
        `prev_close missing → today_pnl = 0` fallback.
    """
    prev_marks = _prev_close_marks(conn, as_of=as_of)
    as_of_iso = as_of.isoformat()
    rows = conn.execute(
        """SELECT s.symbol AS symbol, pt.ts_entry AS ts_entry,
                  pt.exit_price AS exit_price, pt.qty AS qty, pt.pnl AS pnl
             FROM paper_trades pt
             JOIN signals s ON s.id = pt.signal_id
            WHERE pt.ts_exit IS NOT NULL AND date(pt.ts_exit) = ?""",
        (as_of_iso,),
    ).fetchall()

    total = 0.0
    for r in rows:
        if r["exit_price"] is None:
            continue
        if str(r["ts_entry"])[:10] == as_of_iso:
            total += float(r["pnl"] or 0.0)  # same-day round trip: all of it is today's
        elif r["symbol"] in prev_marks:
            qty = float(r["qty"])
            exit_price = float(r["exit_price"])
            exit_value = exit_price * qty
            total += qty * (exit_price - prev_marks[r["symbol"]]) - sell_side_cost(exit_value)
        # else: multi-day close with no prev mark → contributes 0 (see docstring)
    return total


def compute_summary(
    conn: sqlite3.Connection,
    *,
    as_of: date,
    initial_capital: float = INITIAL_CAPITAL,
) -> PortfolioSummary:
    """Aggregate the positions and fold in cash + funds for the summary tiles.

    `total_pnl` is the account-level truth (F-059): `account_value − capital
    base` (initial capital + funds added). Because `compute_paper_cash` debits
    each open lot's buy-side cost at entry, this equals `realised_pnl +
    unrealised_pnl − open-lot buy costs` — open lots carry their entry costs
    as drag until recovered, which is conservative and keeps the tile in exact
    agreement with the "Account value" tile. `realised_pnl` (net of round-trip
    costs, straight from `paper_trades.pnl`) and `unrealised_pnl` (mark −
    cost, matching the holdings table) are exposed separately.

    `today_pnl` = open positions' move since the prior close, plus the
    day-scoped realised move of trades closed on `as_of` (see
    `_realised_today_pnl`).
    """
    positions = compute_positions(conn, as_of=as_of)
    invested = sum(p.invested for p in positions)
    current_value = sum(p.current_value for p in positions)
    unrealised_pnl = current_value - invested
    today_pnl = sum(p.today_pnl for p in positions) + _realised_today_pnl(conn, as_of=as_of)
    realised_pnl = _realised_pnl(conn, as_of=as_of)
    cash = compute_paper_cash(conn, as_of=as_of, initial_capital=initial_capital)
    funds_added = total_funds_added(conn, as_of=as_of)
    capital_base = initial_capital + funds_added
    account_value = cash + current_value
    total_pnl = account_value - capital_base
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
        account_value=account_value,
        as_of_mark=as_of_mark,
    )
