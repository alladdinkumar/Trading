"""Tests for trading.paper.reconcile — matured predictions + portfolio snapshot."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pandas as pd
import pytest

from trading.paper.ledger import (
    buy_side_cost,
    close_with_exit,
    compute_trade_pnl,
    log_signal_and_open_trade,
    sell_side_cost,
)
from trading.paper.reconcile import (
    compute_paper_cash,
    compute_portfolio_snapshot,
    equity_series,
    evaluate_matured_predictions,
    portfolio_gate_sharpe,
    reconcile_day,
    upsert_portfolio_snapshot,
)
from trading.store.migrations import run_migrations
from trading.store.repo import Signal, list_predictions_by_symbol, matured_score_outcomes
from trading.strategy.exits import Bar


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _signal(
    symbol: str = "X", entry: float = 100.0, horizon: int = 15, ml_score: float | None = None
) -> Signal:
    return Signal(
        id=None,
        ts="2026-05-01T09:15:00",
        symbol=symbol,
        side="LONG",
        entry=entry,
        stop=entry * 0.9,
        target=entry * 1.2,
        horizon_days=horizon,
        created_by="auto",
        ml_score=ml_score,
    )


# ---------------------------------------------------------------------------
# evaluate_matured_predictions — closed-trade path
# ---------------------------------------------------------------------------


def test_paper_cash_rises_by_top_up(conn: sqlite3.Connection) -> None:
    from trading.paper.funds import add_funds

    base = compute_paper_cash(conn, as_of=date(2026, 6, 19))
    add_funds(conn, amount=40_000.0, date="2026-06-19")
    after = compute_paper_cash(conn, as_of=date(2026, 6, 19))
    assert after == pytest.approx(base + 40_000.0)


def test_paper_cash_excludes_future_top_ups(conn: sqlite3.Connection) -> None:
    from trading.paper.funds import add_funds

    add_funds(conn, amount=40_000.0, date="2026-06-25")
    # A top-up dated after as_of must not count yet.
    assert compute_paper_cash(conn, as_of=date(2026, 6, 19)) == pytest.approx(100_000.0)


def test_paper_cash_unchanged_with_empty_ledger(conn: sqlite3.Connection) -> None:
    # Regression guard: no ledger rows ⇒ byte-identical to the seed.
    assert compute_paper_cash(conn, as_of=date(2026, 6, 19)) == pytest.approx(100_000.0)


def test_paper_cash_day_gating_correct_for_tz_aware_early_ist_timestamps(
    conn: sqlite3.Connection,
) -> None:
    """F-058 follow-up: since F-058 the ledger persists tz-aware IST timestamps
    (e.g. `2026-07-04T01:00:00+05:30`). SQLite's `date()` converts an offset-
    aware string to UTC before extracting the date, so an IST stamp between
    00:00 and 05:29 lands on the *previous* day — a paper-open at 01:00 IST on
    the 4th would be debited from the 3rd's cash balance. Day-gating must read
    the local date (the leading YYYY-MM-DD), for aware and naive rows alike.
    """
    res = log_signal_and_open_trade(
        conn,
        signal=_signal(entry=100),
        entry_ts="2026-07-04T01:00:00+05:30",  # 2026-07-03T19:30Z — same IST day 4th
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    entry_value = 100.0 * 10
    debited = 100_000.0 - entry_value - buy_side_cost(entry_value)
    # Opened on the 4th (IST): the 3rd's balance must not carry the debit.
    assert compute_paper_cash(conn, as_of=date(2026, 7, 3)) == pytest.approx(100_000.0)
    assert compute_paper_cash(conn, as_of=date(2026, 7, 4)) == pytest.approx(debited)

    close_with_exit(
        conn,
        res.paper_trade_id,
        exit_ts="2026-07-06T01:00:00+05:30",  # 2026-07-05T19:30Z — same IST day 6th
        exit_price=110,
        exit_reason="TARGET",
        days_held=2,
    )
    exit_value = 110.0 * 10
    credited = debited + exit_value - sell_side_cost(exit_value)
    # Closed on the 6th (IST): the 5th's balance must not carry the credit.
    assert compute_paper_cash(conn, as_of=date(2026, 7, 5)) == pytest.approx(debited)
    assert compute_paper_cash(conn, as_of=date(2026, 7, 6)) == pytest.approx(credited)


def test_matured_predictions_from_closed_trade(conn: sqlite3.Connection) -> None:
    res = log_signal_and_open_trade(
        conn,
        signal=_signal(entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    close_with_exit(
        conn,
        res.paper_trade_id,
        exit_ts="2026-05-10T15:30:00",
        exit_price=115,
        exit_reason="TARGET",
        days_held=9,
    )
    updates = evaluate_matured_predictions(conn, as_of=date(2026, 5, 11), bars={})
    assert len(updates) == 1
    u = updates[0]
    # F-051: actual must be net of round-trip costs (same yardstick as the
    # ledger's pnl_pct), not the gross (115-100)/100 = +15% price move.
    _, expected_net_pct = compute_trade_pnl(entry_price=100, exit_price=115, qty=10)
    assert u.actual_return_pct == pytest.approx(expected_net_pct)
    assert u.actual_return_pct < 15.0  # net must be below the gross figure
    assert u.predicted_return_pct == pytest.approx(20.0)
    assert u.error_pct == pytest.approx(expected_net_pct - 20.0)

    pred = list_predictions_by_symbol(conn, "X")[0]
    assert pred.actual_return_at_horizon == pytest.approx(expected_net_pct)
    assert pred.evaluated_at is not None


def test_matured_predictions_win_label_flips_net_of_costs(conn: sqlite3.Connection) -> None:
    """F-051: a small gross gain that costs eat through must be a net loss.

    entry=100, exit=100.3 is +0.3% gross (a 'win' under the old gross rule)
    but round-trip costs (~0.4-0.5%) push the net return negative — the
    matured actual (and hence the calibration 'won' label) must reflect that.
    """
    res = log_signal_and_open_trade(
        conn,
        signal=_signal(entry=100, ml_score=0.7),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=100,
        atr_at_entry=2.0,
    )
    close_with_exit(
        conn,
        res.paper_trade_id,
        exit_ts="2026-05-10T15:30:00",
        exit_price=100.3,
        exit_reason="TIME",
        days_held=9,
    )
    updates = evaluate_matured_predictions(conn, as_of=date(2026, 5, 11), bars={})
    assert len(updates) == 1
    assert updates[0].actual_return_pct < 0.0  # net loss despite +0.3% gross

    pred = list_predictions_by_symbol(conn, "X")[0]
    assert pred.actual_return_at_horizon is not None
    assert pred.actual_return_at_horizon < 0.0

    won = matured_score_outcomes(conn)
    assert won == [(pytest.approx(0.7), False)]


def test_matured_predictions_bar_path_when_trade_still_open(conn: sqlite3.Connection) -> None:
    """Horizon elapsed but trade still open → use bar.close vs entry, net of costs."""
    log_signal_and_open_trade(
        conn,
        signal=_signal(entry=100, horizon=5),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    bars = {"X": Bar(open=109, high=112, low=108, close=110)}
    updates = evaluate_matured_predictions(
        conn,
        as_of=date(2026, 5, 10),
        bars=bars,
    )
    assert len(updates) == 1
    _, expected_net_pct = compute_trade_pnl(entry_price=100, exit_price=110, qty=10)
    assert updates[0].actual_return_pct == pytest.approx(expected_net_pct)
    assert updates[0].actual_return_pct < 10.0  # net must be below the gross figure


def test_matured_predictions_skips_unmatured(conn: sqlite3.Connection) -> None:
    """Horizon not yet elapsed and trade still open → leave untouched."""
    log_signal_and_open_trade(
        conn,
        signal=_signal(entry=100, horizon=30),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    updates = evaluate_matured_predictions(
        conn,
        as_of=date(2026, 5, 10),
        bars={"X": Bar(105, 106, 104, 105.5)},
    )
    assert updates == []
    pred = list_predictions_by_symbol(conn, "X")[0]
    assert pred.actual_return_at_horizon is None


def test_matured_predictions_skips_when_bar_missing_and_trade_open(
    conn: sqlite3.Connection,
) -> None:
    log_signal_and_open_trade(
        conn,
        signal=_signal(entry=100, horizon=5),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    assert evaluate_matured_predictions(conn, as_of=date(2026, 5, 10), bars={}) == []


def test_matured_predictions_idempotent(conn: sqlite3.Connection) -> None:
    """Re-running on already-matured predictions yields no further updates."""
    res = log_signal_and_open_trade(
        conn,
        signal=_signal(entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    close_with_exit(
        conn,
        res.paper_trade_id,
        exit_ts="2026-05-10T15:30:00",
        exit_price=110,
        exit_reason="TARGET",
        days_held=9,
    )
    evaluate_matured_predictions(conn, as_of=date(2026, 5, 11), bars={})
    second = evaluate_matured_predictions(conn, as_of=date(2026, 5, 11), bars={})
    assert second == []


# ---------------------------------------------------------------------------
# compute_portfolio_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_no_open_positions_equals_cash(conn: sqlite3.Connection) -> None:
    snap = compute_portfolio_snapshot(
        conn, as_of=date(2026, 5, 15), bars={}, initial_capital=100_000
    )
    assert snap.equity == pytest.approx(100_000)
    assert snap.cash == pytest.approx(100_000)
    assert snap.drawdown_pct is None
    assert json.loads(snap.holdings_json) == {}


def test_snapshot_uses_bar_close_for_open_positions(conn: sqlite3.Connection) -> None:
    log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="B", entry=200),
        entry_ts="2026-05-01T09:20:00",
        entry_price=200,
        qty=5,
        atr_at_entry=4.0,
    )
    bars = {"A": Bar(108, 112, 107, 110), "B": Bar(195, 205, 192, 198)}
    snap = compute_portfolio_snapshot(
        conn,
        as_of=date(2026, 5, 15),
        bars=bars,
        initial_capital=50_000,
    )
    # Cash debited by the two opens incl. F-025 buy-side cost on each fill:
    #   50_000 - (1000 + buy_side_cost(1000)) - (1000 + buy_side_cost(1000)).
    # Open mark value: 10*110 + 5*198 = 1100 + 990 = 2090.
    expected_cash = 50_000 - (1000 + buy_side_cost(1000)) - (1000 + buy_side_cost(1000))
    assert snap.cash == pytest.approx(expected_cash)
    assert snap.equity == pytest.approx(expected_cash + 1100 + 990)
    holdings = json.loads(snap.holdings_json)
    assert holdings["A"]["value"] == pytest.approx(1100.0)
    assert holdings["B"]["value"] == pytest.approx(990.0)


def test_snapshot_falls_back_to_entry_price_when_bar_missing(conn: sqlite3.Connection) -> None:
    log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    snap = compute_portfolio_snapshot(
        conn,
        as_of=date(2026, 5, 15),
        bars={},
        initial_capital=50_000,
    )
    # Cash debited 1000 + buy_side_cost(1000) (F-025); open value falls back to
    # entry_price * qty = 1000 → equity = 50_000 minus the buy-side cost only.
    assert snap.cash == pytest.approx(49_000 - buy_side_cost(1000))
    assert snap.equity == pytest.approx(50_000 - buy_side_cost(1000))


def test_drawdown_computed_against_peak(conn: sqlite3.Connection) -> None:
    """Drawdown should be 0 on first snapshot, then grow if equity drops."""
    upsert_portfolio_snapshot(
        conn,
        compute_portfolio_snapshot(
            conn,
            as_of=date(2026, 5, 10),
            bars={},
            initial_capital=100_000,
        ),
    )
    # Equity goes up: open A (debits 1000 → cash 99_000), marked at 110.
    log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-11T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    upsert_portfolio_snapshot(
        conn,
        compute_portfolio_snapshot(
            conn,
            as_of=date(2026, 5, 11),
            bars={"A": Bar(108, 112, 107, 110)},
            initial_capital=100_000,
        ),
    )
    # Equity drops
    snap_drop = compute_portfolio_snapshot(
        conn,
        as_of=date(2026, 5, 12),
        bars={"A": Bar(95, 96, 93, 94)},
        initial_capital=100_000,
    )
    # Peak was 99_000 + 1100 = 100_100; now 99_000 + 940 = 99_940 → -0.16%
    assert snap_drop.drawdown_pct is not None
    assert snap_drop.drawdown_pct > 0
    assert snap_drop.drawdown_pct < 1.0


def test_upsert_portfolio_snapshot_replaces_existing(conn: sqlite3.Connection) -> None:
    snap1 = compute_portfolio_snapshot(
        conn, as_of=date(2026, 5, 15), bars={}, initial_capital=100_000
    )
    upsert_portfolio_snapshot(conn, snap1)
    # Open a trade, then re-snapshot the same date → row replaced, cash debited.
    log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-15T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    upsert_portfolio_snapshot(
        conn,
        compute_portfolio_snapshot(conn, as_of=date(2026, 5, 15), bars={}, initial_capital=100_000),
    )
    rows = conn.execute(
        "SELECT * FROM portfolio_snapshots WHERE date = ?", ("2026-05-15",)
    ).fetchall()
    assert len(rows) == 1
    # F-025: debit also carries the buy-side cost.
    assert rows[0]["cash"] == pytest.approx(99_000 - buy_side_cost(1000))


# ---------------------------------------------------------------------------
# compute_paper_cash — the F-023 cash ledger
# ---------------------------------------------------------------------------


def test_paper_cash_seed_with_no_trades(conn: sqlite3.Connection) -> None:
    assert compute_paper_cash(
        conn, as_of=date(2026, 5, 15), initial_capital=100_000
    ) == pytest.approx(100_000)


def test_paper_cash_debits_open_and_credits_close(conn: sqlite3.Connection) -> None:
    """A closed winner compounds into cash; an open position only debits."""
    won = log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    # While open: cash = 100_000 - 1000 - buy_side_cost(1000) (F-025).
    open_cash = 100_000 - 1000 - buy_side_cost(1000)
    assert compute_paper_cash(
        conn, as_of=date(2026, 5, 5), initial_capital=100_000
    ) == pytest.approx(open_cash)
    # Close at 115: credit 1150 net of sell-side cost → +150 gross realised, less costs.
    close_with_exit(
        conn,
        won.paper_trade_id,
        exit_ts="2026-05-10T15:30:00",
        exit_price=115,
        exit_reason="TARGET",
        days_held=9,
    )
    assert compute_paper_cash(
        conn, as_of=date(2026, 5, 11), initial_capital=100_000
    ) == pytest.approx(open_cash + 1150 - sell_side_cost(1150))


def test_paper_cash_filters_future_trades_by_date(conn: sqlite3.Connection) -> None:
    """A trade opened after `as_of` must not affect that day's cash."""
    log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-20T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    assert compute_paper_cash(
        conn, as_of=date(2026, 5, 15), initial_capital=100_000
    ) == pytest.approx(100_000)


def test_equity_compounds_realised_pnl(conn: sqlite3.Connection) -> None:
    """F-023: closing a winner lifts the equity curve above the seed capital;
    closing a loser drops it below — realised P&L no longer vanishes."""
    win = log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="W", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    close_with_exit(
        conn,
        win.paper_trade_id,
        exit_ts="2026-05-05T15:30:00",
        exit_price=120,
        exit_reason="TARGET",
        days_held=4,
    )
    snap_win = compute_portfolio_snapshot(
        conn, as_of=date(2026, 5, 6), bars={}, initial_capital=100_000
    )
    # +200 gross realised (20 * 10), less round-trip costs (F-025); no open positions.
    win_equity = 100_000 - 1000 - buy_side_cost(1000) + 1200 - sell_side_cost(1200)
    assert snap_win.equity == pytest.approx(win_equity)
    assert snap_win.equity < 100_200  # costs shave the gross gain

    loss = log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="L", entry=100),
        entry_ts="2026-05-07T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    close_with_exit(
        conn,
        loss.paper_trade_id,
        exit_ts="2026-05-08T15:30:00",
        exit_price=90,
        exit_reason="STOP",
        days_held=1,
    )
    snap_loss = compute_portfolio_snapshot(
        conn, as_of=date(2026, 5, 9), bars={}, initial_capital=100_000
    )
    # Net realised = +200 - 100 gross, less both round-trips' costs (F-025).
    loss_equity = win_equity - 1000 - buy_side_cost(1000) + 900 - sell_side_cost(900)
    assert snap_loss.equity == pytest.approx(loss_equity)
    assert snap_loss.equity < 100_100  # costs below the gross net of +100


# ---------------------------------------------------------------------------
# reconcile_day — top-level orchestrator
# ---------------------------------------------------------------------------


def test_reconcile_day_writes_snapshot_and_returns_updates(conn: sqlite3.Connection) -> None:
    res = log_signal_and_open_trade(
        conn,
        signal=_signal(symbol="A", entry=100),
        entry_ts="2026-05-01T09:20:00",
        entry_price=100,
        qty=10,
        atr_at_entry=2.0,
    )
    close_with_exit(
        conn,
        res.paper_trade_id,
        exit_ts="2026-05-10T15:30:00",
        exit_price=115,
        exit_reason="TARGET",
        days_held=9,
    )
    result = reconcile_day(
        conn,
        as_of=date(2026, 5, 11),
        bars={},
        initial_capital=100_000,
    )
    assert result.snapshot.date == "2026-05-11"
    assert len(result.prediction_updates) == 1
    assert result.prediction_updates[0].symbol == "A"

    # Snapshot persisted. Closed A at 115 (entry 100, qty 10) → +150 gross realised
    # less round-trip costs (F-025), compounded into equity (F-023); no opens remain.
    row = conn.execute(
        "SELECT * FROM portfolio_snapshots WHERE date = ?", ("2026-05-11",)
    ).fetchone()
    assert row is not None
    expected_equity = 100_000 - 1000 - buy_side_cost(1000) + 1150 - sell_side_cost(1150)
    assert row["equity"] == pytest.approx(expected_equity)


# ---------------------------------------------------------------------------
# equity_series / portfolio_gate_sharpe (F-061)
# ---------------------------------------------------------------------------


def _seed_snapshots(conn: sqlite3.Connection, rows: list[tuple[str, float]]) -> None:
    for d, equity in rows:
        conn.execute(
            "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity) "
            "VALUES (?, ?, '{}', ?)",
            (d, equity, equity),
        )
    conn.commit()


def test_equity_series_empty_when_no_snapshots(conn: sqlite3.Connection) -> None:
    s = equity_series(conn)
    assert s.empty


def test_equity_series_ordered_ascending_by_date(conn: sqlite3.Connection) -> None:
    _seed_snapshots(
        conn,
        [
            ("2026-06-02", 101_000.0),
            ("2026-06-01", 100_000.0),
            ("2026-06-03", 100_500.0),
        ],
    )
    s = equity_series(conn)
    assert list(s.index) == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert list(s) == [100_000.0, 101_000.0, 100_500.0]


def test_portfolio_gate_sharpe_matches_pure_function(conn: sqlite3.Connection) -> None:
    from trading.backtest.metrics import gate_sharpe

    rows = [
        ("2026-06-01", 100_000.0),
        ("2026-06-02", 101_000.0),
        ("2026-06-03", 100_500.0),
        ("2026-06-04", 102_000.0),
        ("2026-06-05", 103_000.0),
    ]
    _seed_snapshots(conn, rows)
    expected = gate_sharpe(pd.Series([r[1] for r in rows]))
    assert portfolio_gate_sharpe(conn) == pytest.approx(expected)


def test_portfolio_gate_sharpe_none_when_insufficient_snapshots(
    conn: sqlite3.Connection,
) -> None:
    _seed_snapshots(conn, [("2026-06-01", 100_000.0)])
    assert portfolio_gate_sharpe(conn) is None


def test_portfolio_gate_sharpe_none_with_no_snapshots(conn: sqlite3.Connection) -> None:
    assert portfolio_gate_sharpe(conn) is None
