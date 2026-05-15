"""Tests for trading.paper.reconcile — matured predictions + portfolio snapshot."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from trading.paper.ledger import close_with_exit, log_signal_and_open_trade
from trading.paper.reconcile import (
    compute_portfolio_snapshot,
    evaluate_matured_predictions,
    reconcile_day,
    upsert_portfolio_snapshot,
)
from trading.store.migrations import run_migrations
from trading.store.repo import Signal, list_predictions_by_symbol
from trading.strategy.exits import Bar


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _signal(symbol: str = "X", entry: float = 100.0, horizon: int = 15) -> Signal:
    return Signal(
        id=None, ts="2026-05-01T09:15:00", symbol=symbol, side="LONG",
        entry=entry, stop=entry * 0.9, target=entry * 1.2,
        horizon_days=horizon, created_by="auto",
    )


# ---------------------------------------------------------------------------
# evaluate_matured_predictions — closed-trade path
# ---------------------------------------------------------------------------


def test_matured_predictions_from_closed_trade(conn: sqlite3.Connection) -> None:
    res = log_signal_and_open_trade(
        conn, signal=_signal(entry=100), entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    close_with_exit(
        conn, res.paper_trade_id, exit_ts="2026-05-10T15:30:00",
        exit_price=115, exit_reason="TARGET", days_held=9,
    )
    updates = evaluate_matured_predictions(conn, as_of=date(2026, 5, 11), bars={})
    assert len(updates) == 1
    u = updates[0]
    # Actual = (115 - 100) / 100 = +15%; predicted = +20% (from signal.target)
    assert u.actual_return_pct == pytest.approx(15.0)
    assert u.predicted_return_pct == pytest.approx(20.0)
    assert u.error_pct == pytest.approx(-5.0)

    pred = list_predictions_by_symbol(conn, "X")[0]
    assert pred.actual_return_at_horizon == pytest.approx(15.0)
    assert pred.evaluated_at is not None


def test_matured_predictions_bar_path_when_trade_still_open(conn: sqlite3.Connection) -> None:
    """Horizon elapsed but trade still open → use bar.close vs entry."""
    log_signal_and_open_trade(
        conn, signal=_signal(entry=100, horizon=5),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    bars = {"X": Bar(open=109, high=112, low=108, close=110)}
    updates = evaluate_matured_predictions(
        conn, as_of=date(2026, 5, 10), bars=bars,
    )
    assert len(updates) == 1
    assert updates[0].actual_return_pct == pytest.approx(10.0)


def test_matured_predictions_skips_unmatured(conn: sqlite3.Connection) -> None:
    """Horizon not yet elapsed and trade still open → leave untouched."""
    log_signal_and_open_trade(
        conn, signal=_signal(entry=100, horizon=30),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    updates = evaluate_matured_predictions(
        conn, as_of=date(2026, 5, 10), bars={"X": Bar(105, 106, 104, 105.5)},
    )
    assert updates == []
    pred = list_predictions_by_symbol(conn, "X")[0]
    assert pred.actual_return_at_horizon is None


def test_matured_predictions_skips_when_bar_missing_and_trade_open(conn: sqlite3.Connection) -> None:
    log_signal_and_open_trade(
        conn, signal=_signal(entry=100, horizon=5),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    assert evaluate_matured_predictions(conn, as_of=date(2026, 5, 10), bars={}) == []


def test_matured_predictions_idempotent(conn: sqlite3.Connection) -> None:
    """Re-running on already-matured predictions yields no further updates."""
    res = log_signal_and_open_trade(
        conn, signal=_signal(entry=100), entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    close_with_exit(
        conn, res.paper_trade_id, exit_ts="2026-05-10T15:30:00",
        exit_price=110, exit_reason="TARGET", days_held=9,
    )
    evaluate_matured_predictions(conn, as_of=date(2026, 5, 11), bars={})
    second = evaluate_matured_predictions(conn, as_of=date(2026, 5, 11), bars={})
    assert second == []


# ---------------------------------------------------------------------------
# compute_portfolio_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_no_open_positions_equals_cash(conn: sqlite3.Connection) -> None:
    snap = compute_portfolio_snapshot(conn, as_of=date(2026, 5, 15), cash=100_000, bars={})
    assert snap.equity == pytest.approx(100_000)
    assert snap.drawdown_pct is None
    assert json.loads(snap.holdings_json) == {}


def test_snapshot_uses_bar_close_for_open_positions(conn: sqlite3.Connection) -> None:
    log_signal_and_open_trade(
        conn, signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    log_signal_and_open_trade(
        conn, signal=_signal(symbol="B", entry=200),
        entry_ts="2026-05-01T09:20:00",
        entry_price=200, qty=5, atr_at_entry=4.0,
    )
    bars = {"A": Bar(108, 112, 107, 110), "B": Bar(195, 205, 192, 198)}
    snap = compute_portfolio_snapshot(
        conn, as_of=date(2026, 5, 15), cash=50_000, bars=bars,
    )
    # Open value: 10*110 + 5*198 = 1100 + 990 = 2090
    assert snap.equity == pytest.approx(50_000 + 1100 + 990)
    holdings = json.loads(snap.holdings_json)
    assert holdings["A"]["value"] == pytest.approx(1100.0)
    assert holdings["B"]["value"] == pytest.approx(990.0)


def test_snapshot_falls_back_to_entry_price_when_bar_missing(conn: sqlite3.Connection) -> None:
    log_signal_and_open_trade(
        conn, signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    snap = compute_portfolio_snapshot(
        conn, as_of=date(2026, 5, 15), cash=50_000, bars={},
    )
    # Falls back to entry_price * qty = 100 * 10 = 1000
    assert snap.equity == pytest.approx(51_000)


def test_drawdown_computed_against_peak(conn: sqlite3.Connection) -> None:
    """Drawdown should be 0 on first snapshot, then grow if equity drops."""
    upsert_portfolio_snapshot(
        conn,
        compute_portfolio_snapshot(
            conn, as_of=date(2026, 5, 10), cash=100_000, bars={},
        ),
    )
    # Equity goes up
    log_signal_and_open_trade(
        conn, signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-11T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    upsert_portfolio_snapshot(
        conn,
        compute_portfolio_snapshot(
            conn, as_of=date(2026, 5, 11), cash=99_000,
            bars={"A": Bar(108, 112, 107, 110)},
        ),
    )
    # Equity drops 5%
    snap_drop = compute_portfolio_snapshot(
        conn, as_of=date(2026, 5, 12), cash=99_000,
        bars={"A": Bar(95, 96, 93, 94)},
    )
    # Peak was 99_000 + 1100 = 100_100; now 99_000 + 940 = 99_940 → -0.16%
    assert snap_drop.drawdown_pct is not None
    assert snap_drop.drawdown_pct > 0
    assert snap_drop.drawdown_pct < 1.0


def test_upsert_portfolio_snapshot_replaces_existing(conn: sqlite3.Connection) -> None:
    snap1 = compute_portfolio_snapshot(conn, as_of=date(2026, 5, 15), cash=100_000, bars={})
    upsert_portfolio_snapshot(conn, snap1)
    upsert_portfolio_snapshot(
        conn,
        compute_portfolio_snapshot(conn, as_of=date(2026, 5, 15), cash=99_000, bars={}),
    )
    rows = conn.execute(
        "SELECT * FROM portfolio_snapshots WHERE date = ?", ("2026-05-15",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["cash"] == pytest.approx(99_000)


# ---------------------------------------------------------------------------
# reconcile_day — top-level orchestrator
# ---------------------------------------------------------------------------


def test_reconcile_day_writes_snapshot_and_returns_updates(conn: sqlite3.Connection) -> None:
    res = log_signal_and_open_trade(
        conn, signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    close_with_exit(
        conn, res.paper_trade_id, exit_ts="2026-05-10T15:30:00",
        exit_price=115, exit_reason="TARGET", days_held=9,
    )
    result = reconcile_day(
        conn, as_of=date(2026, 5, 11), cash=100_000,
        bars={},
    )
    assert result.snapshot.date == "2026-05-11"
    assert len(result.prediction_updates) == 1
    assert result.prediction_updates[0].symbol == "A"

    # Snapshot persisted
    row = conn.execute(
        "SELECT * FROM portfolio_snapshots WHERE date = ?", ("2026-05-11",)
    ).fetchone()
    assert row is not None
    assert row["equity"] == pytest.approx(100_000)  # no open positions
