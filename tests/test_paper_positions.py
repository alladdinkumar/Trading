"""Tests for trading.paper.positions — per-symbol holdings + summary."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from trading.costs import buy_side_cost, sell_side_cost
from trading.paper.ledger import close_with_exit, log_signal_and_open_trade
from trading.paper.positions import (
    already_opened_today,
    compute_positions,
    compute_summary,
    deployed_by_symbol,
    open_lots_by_symbol,
)
from trading.store.migrations import run_migrations
from trading.store.repo import Signal, list_open_paper_trades


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _open(conn: sqlite3.Connection, symbol: str, entry: float, qty: int, ts: str) -> None:
    sig = Signal(
        id=None, ts=ts, symbol=symbol, side="LONG",
        entry=entry, stop=entry * 0.9, target=entry * 1.2,
        horizon_days=15, created_by="auto",
    )
    log_signal_and_open_trade(
        conn, signal=sig, entry_ts=ts, entry_price=entry, qty=qty, atr_at_entry=2.0
    )


def _snapshot(conn: sqlite3.Connection, d: str, holdings: dict[str, dict[str, float]]) -> None:
    conn.execute(
        "INSERT INTO portfolio_snapshots(date, cash, holdings_json, equity) VALUES (?, ?, ?, ?)",
        (d, 100000.0, json.dumps(holdings), 100000.0),
    )
    conn.commit()


def test_multi_lot_avg_and_invested(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _open(conn, "ACME", entry=120.0, qty=10, ts="2026-06-02T09:20:00")
    pos = {p.symbol: p for p in compute_positions(conn, as_of=date(2026, 6, 3))}
    acme = pos["ACME"]
    assert acme.qty == 20
    assert acme.invested == pytest.approx(2200.0)  # 100*10 + 120*10
    assert acme.avg == pytest.approx(110.0)


def test_ltp_from_latest_snapshot(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    # Latest snapshot marks ACME at 130 (value=1300 over qty 10).
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1300.0}})
    acme = compute_positions(conn, as_of=date(2026, 6, 3))[0]
    assert acme.ltp == pytest.approx(130.0)
    assert acme.current_value == pytest.approx(1300.0)
    assert acme.pnl == pytest.approx(300.0)
    assert acme.pnl_pct == pytest.approx(30.0)


def test_ltp_falls_back_to_avg_when_symbol_absent(conn: sqlite3.Connection) -> None:
    _open(conn, "NEW", entry=50.0, qty=4, ts="2026-06-03T09:20:00")
    # Snapshot predates the position and lacks NEW → LTP falls back to avg.
    _snapshot(conn, "2026-06-02", {"OTHER": {"qty": 1, "value": 10.0}})
    new = {p.symbol: p for p in compute_positions(conn, as_of=date(2026, 6, 3))}["NEW"]
    assert new.ltp == pytest.approx(50.0)
    assert new.pnl == pytest.approx(0.0)
    assert new.today_pnl == pytest.approx(0.0)


def test_today_pnl_from_prior_snapshot(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1200.0}})  # prev close 120
    _snapshot(conn, "2026-06-03", {"ACME": {"qty": 10, "value": 1300.0}})  # ltp 130
    acme = compute_positions(conn, as_of=date(2026, 6, 4))[0]
    assert acme.today_pnl == pytest.approx(100.0)  # 10 * (130 - 120)


def test_today_pnl_zero_with_single_snapshot(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1300.0}})
    acme = compute_positions(conn, as_of=date(2026, 6, 3))[0]
    assert acme.today_pnl == pytest.approx(0.0)  # no prior snapshot → prev_close = ltp


def test_summary_totals_include_cash_and_funds(conn: sqlite3.Connection) -> None:
    from trading.paper.funds import add_funds

    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1300.0}})
    add_funds(conn, amount=20_000.0, date="2026-06-01")
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert s.invested == pytest.approx(1000.0)
    assert s.current_value == pytest.approx(1300.0)
    assert s.unrealised_pnl == pytest.approx(300.0)
    # Total P&L = account_value − capital_base: the open lot's buy-side cost
    # counts as drag until recovered (F-059 follow-up).
    assert s.total_pnl == pytest.approx(300.0 - buy_side_cost(1000.0))
    assert s.funds_added == pytest.approx(20_000.0)
    assert s.as_of_mark == "2026-06-02"
    # account_value = cash + current_value; cash already includes the top-up.
    assert s.account_value == pytest.approx(s.cash + s.current_value)


def test_summary_total_pnl_includes_realised_gain_from_closed_trade(
    conn: sqlite3.Connection,
) -> None:
    """F-059: closing a winning trade must not vanish from the P&L tiles.

    Open one lot, close it for a profit, and leave nothing open. The old
    `compute_summary` derived `total_pnl` purely from open positions, so it
    silently reported 0 the instant the trade closed even though the account
    was up materially.
    """
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1200.0}})  # prev close 120
    trade = list_open_paper_trades(conn)[0]
    closed = close_with_exit(
        conn,
        trade.id,
        exit_ts="2026-06-03T09:20:00",
        exit_price=150.0,
        exit_reason="TARGET",
        days_held=2,
    )
    assert closed.pnl is not None and closed.pnl > 0  # sanity: a real winner

    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert compute_positions(conn, as_of=date(2026, 6, 3)) == []  # nothing open
    # Nothing open → no open-lot cost drag: total P&L is exactly the realised gain.
    assert s.total_pnl == pytest.approx(closed.pnl)
    assert s.total_pnl != 0.0
    # Today's tile is day-scoped: only the move since yesterday's mark (120 → 150),
    # net of sell-side costs — NOT the trade's full multi-day P&L.
    assert s.today_pnl == pytest.approx(10 * (150.0 - 120.0) - sell_side_cost(1500.0))


def test_today_pnl_full_realised_for_same_day_round_trip(conn: sqlite3.Connection) -> None:
    """A trade opened and closed the same day contributes its full net P&L today."""
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-03T09:20:00")
    trade = list_open_paper_trades(conn)[0]
    closed = close_with_exit(
        conn,
        trade.id,
        exit_ts="2026-06-03T14:20:00",
        exit_price=110.0,
        exit_reason="TARGET",
        days_held=0,
    )
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert s.today_pnl == pytest.approx(closed.pnl)


def test_today_pnl_realised_zero_for_multiday_close_without_prev_mark(
    conn: sqlite3.Connection,
) -> None:
    """No prev-close mark for a multi-day trade → today's realised leg is 0.

    Mirrors the open-position leg's fallback (prev_close missing → today_pnl 0)
    instead of dumping days of movement into one day's tile.
    """
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    trade = list_open_paper_trades(conn)[0]
    closed = close_with_exit(
        conn,
        trade.id,
        exit_ts="2026-06-03T09:20:00",
        exit_price=150.0,
        exit_reason="TARGET",
        days_held=2,
    )
    assert closed.pnl is not None and closed.pnl > 0
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert s.today_pnl == pytest.approx(0.0)
    assert s.total_pnl == pytest.approx(closed.pnl)  # cumulative tile still honest


def test_summary_total_pnl_identity_mixed_book(conn: sqlite3.Connection) -> None:
    """Mixed book: total_pnl is exactly account_value − capital_base.

    One closed winner + one still-open lot + a mid-run funds top-up. Pins the
    identity the "Total P&L" tooltip states, its decomposition (realised +
    unrealised − open-lot buy costs), and the pct denominator (capital base).
    """
    from trading.paper.funds import add_funds

    _open(conn, "WIN", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _open(conn, "HOLD", entry=200.0, qty=5, ts="2026-06-01T09:25:00")
    add_funds(conn, amount=50_000.0, date="2026-06-02")
    _snapshot(
        conn,
        "2026-06-02",
        {"WIN": {"qty": 10, "value": 1200.0}, "HOLD": {"qty": 5, "value": 1050.0}},
    )
    win = list_open_paper_trades(conn)[0]  # ordered by ts_entry → WIN first
    closed = close_with_exit(
        conn,
        win.id,
        exit_ts="2026-06-03T09:20:00",
        exit_price=150.0,
        exit_reason="TARGET",
        days_held=2,
    )
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    capital_base = 100_000.0 + 50_000.0
    assert s.funds_added == pytest.approx(50_000.0)
    # The tooltip's identity, exactly:
    assert s.total_pnl == pytest.approx(s.account_value - capital_base)
    # ...decomposed: realised + unrealised, minus the open lot's buy-side cost
    # (HOLD entry value 200 × 5 = 1000) which stays a drag until recovered.
    assert s.realised_pnl == pytest.approx(closed.pnl)
    assert s.unrealised_pnl == pytest.approx(1050.0 - 1000.0)  # HOLD marked at 210
    assert s.total_pnl == pytest.approx(
        s.realised_pnl + s.unrealised_pnl - buy_side_cost(1000.0)
    )
    # Pct is anchored on the capital base, not open cost basis.
    assert s.total_pnl_pct == pytest.approx(s.total_pnl / capital_base * 100.0)
    # Today: WIN's day-scoped realised move (120 → 150, net of sell costs);
    # HOLD has a single snapshot → prev_close falls back to ltp → 0.
    assert s.today_pnl == pytest.approx(10 * (150.0 - 120.0) - sell_side_cost(1500.0))


def test_summary_pct_zero_when_capital_base_zero(conn: sqlite3.Connection) -> None:
    """Degenerate capital_base = 0 → pct is 0.0, not a ZeroDivisionError."""
    s = compute_summary(conn, as_of=date(2026, 6, 3), initial_capital=0.0)
    assert s.total_pnl == pytest.approx(0.0)
    assert s.total_pnl_pct == 0.0


def test_summary_realised_pnl_only_counts_closes_up_to_as_of(
    conn: sqlite3.Connection,
) -> None:
    """A trade closed *after* `as_of` must not leak into an earlier summary."""
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    trade = list_open_paper_trades(conn)[0]
    close_with_exit(
        conn,
        trade.id,
        exit_ts="2026-06-05T09:20:00",
        exit_price=150.0,
        exit_reason="TARGET",
        days_held=4,
    )
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert s.realised_pnl == pytest.approx(0.0)
    assert s.today_pnl == pytest.approx(0.0)
    # As of 6/3 the lot was still held: it must appear in the backdated view
    # (exit-date-aware, matching compute_paper_cash's exit filter)...
    positions = compute_positions(conn, as_of=date(2026, 6, 3))
    assert [p.symbol for p in positions] == ["ACME"]
    # ...and total P&L carries only its buy-cost drag, not a phantom −₹1000.
    assert s.total_pnl == pytest.approx(-buy_side_cost(1000.0))


def test_summary_empty_when_no_trades(conn: sqlite3.Connection) -> None:
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert s.invested == 0.0
    assert s.current_value == 0.0
    assert s.as_of_mark is None
    assert compute_positions(conn, as_of=date(2026, 6, 3)) == []


def test_deployed_by_symbol_sums_open_cost_basis(conn: sqlite3.Connection) -> None:
    _open(conn, "NESTLEIND", entry=100.0, qty=2, ts="2026-06-23T09:20:00")
    _open(conn, "NESTLEIND", entry=110.0, qty=1, ts="2026-06-23T09:21:00")
    _open(conn, "TATASTEEL", entry=200.0, qty=3, ts="2026-06-23T09:20:00")
    assert deployed_by_symbol(conn) == {"NESTLEIND": 310.0, "TATASTEEL": 600.0}


def test_open_lots_by_symbol_counts_open_entries(conn: sqlite3.Connection) -> None:
    # Two open POWERGRID lots across two days + one TATASTEEL → counts, not notional.
    _open(conn, "POWERGRID", entry=285.0, qty=10, ts="2026-06-16T09:20:00")
    _open(conn, "POWERGRID", entry=288.0, qty=10, ts="2026-06-19T09:20:00")
    _open(conn, "TATASTEEL", entry=200.0, qty=3, ts="2026-06-23T09:20:00")
    assert open_lots_by_symbol(conn) == {"POWERGRID": 2, "TATASTEEL": 1}


def test_already_opened_today_true_only_for_open_same_day(conn: sqlite3.Connection) -> None:
    _open(conn, "TATASTEEL", entry=200.0, qty=1, ts="2026-06-23T09:20:00")
    assert already_opened_today(conn, "TATASTEEL", date(2026, 6, 23)) is True
    assert already_opened_today(conn, "TATASTEEL", date(2026, 6, 22)) is False
    assert already_opened_today(conn, "POWERGRID", date(2026, 6, 23)) is False
