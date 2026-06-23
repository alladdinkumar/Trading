"""Tests for trading.paper.positions — per-symbol holdings + summary."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from trading.paper.ledger import log_signal_and_open_trade
from trading.paper.positions import (
    already_opened_today,
    compute_positions,
    compute_summary,
    deployed_by_symbol,
)
from trading.store.migrations import run_migrations
from trading.store.repo import Signal


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
    assert s.total_pnl == pytest.approx(300.0)
    assert s.funds_added == pytest.approx(20_000.0)
    assert s.as_of_mark == "2026-06-02"
    # account_value = cash + current_value; cash already includes the top-up.
    assert s.account_value == pytest.approx(s.cash + s.current_value)


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


def test_already_opened_today_true_only_for_open_same_day(conn: sqlite3.Connection) -> None:
    _open(conn, "TATASTEEL", entry=200.0, qty=1, ts="2026-06-23T09:20:00")
    assert already_opened_today(conn, "TATASTEEL", date(2026, 6, 23)) is True
    assert already_opened_today(conn, "TATASTEEL", date(2026, 6, 22)) is False
    assert already_opened_today(conn, "POWERGRID", date(2026, 6, 23)) is False
