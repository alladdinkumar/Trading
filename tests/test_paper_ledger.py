"""Tests for trading.paper.ledger — signal/trade lifecycle + pnl helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from trading.paper.ledger import (
    close_with_exit,
    compute_trade_pnl,
    days_between,
    log_signal_and_open_trade,
    open_trades,
)
from trading.store.migrations import run_migrations
from trading.store.repo import (
    Signal,
    get_paper_trade,
    get_signal,
    list_predictions_by_symbol,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _signal(
    *,
    symbol: str = "RVNL",
    entry: float = 300.0,
    stop: float = 285.0,
    target: float = 360.0,
    horizon: int = 15,
) -> Signal:
    return Signal(
        id=None,
        ts="2026-05-15T09:15:00",
        symbol=symbol,
        side="LONG",
        entry=entry,
        stop=stop,
        target=target,
        horizon_days=horizon,
        rules_passed_json='["uptrend","pullback"]',
        rationale="dip in uptrend",
        created_by="auto",
    )


# ---------------------------------------------------------------------------
# log_signal_and_open_trade — atomic 3-row insert
# ---------------------------------------------------------------------------


def test_log_and_open_creates_signal_trade_and_prediction(conn: sqlite3.Connection) -> None:
    result = log_signal_and_open_trade(
        conn,
        signal=_signal(),
        entry_ts="2026-05-15T09:20:00",
        entry_price=302.0,
        qty=10,
        atr_at_entry=6.5,
    )
    assert result.signal_id > 0
    assert result.paper_trade_id > 0
    assert result.prediction_id > 0

    sig = get_signal(conn, result.signal_id)
    assert sig is not None
    assert sig.symbol == "RVNL"

    trade = get_paper_trade(conn, result.paper_trade_id)
    assert trade is not None
    assert trade.entry_price == pytest.approx(302.0)
    assert trade.qty == 10
    assert trade.current_stop == pytest.approx(285.0)  # mirrors signal.stop
    assert trade.atr_at_entry == pytest.approx(6.5)
    assert trade.days_held == 0
    assert trade.ts_exit is None

    preds = list_predictions_by_symbol(conn, "RVNL")
    assert len(preds) == 1
    # Implied target return = (360 - 300) / 300 = 20%
    assert preds[0].predicted_return_pct == pytest.approx(20.0)
    assert preds[0].predicted_horizon_days == 15


def test_log_and_open_accepts_explicit_predicted_return(conn: sqlite3.Connection) -> None:
    result = log_signal_and_open_trade(
        conn,
        signal=_signal(),
        entry_ts=datetime(2026, 5, 15, 9, 20),
        entry_price=302.0,
        qty=10,
        atr_at_entry=6.5,
        predicted_return_pct=12.5,
    )
    preds = list_predictions_by_symbol(conn, "RVNL")
    assert preds[0].predicted_return_pct == pytest.approx(12.5)
    assert result.prediction_id == preds[0].id


def test_log_and_open_accepts_datetime_entry_ts(conn: sqlite3.Connection) -> None:
    """Plain datetime → ISO string under the hood."""
    log_signal_and_open_trade(
        conn,
        signal=_signal(),
        entry_ts=datetime(2026, 5, 15, 9, 20),
        entry_price=302.0,
        qty=10,
        atr_at_entry=6.5,
    )
    trade = open_trades(conn)[0]
    assert trade.ts_entry.startswith("2026-05-15T09:20:00")


# ---------------------------------------------------------------------------
# compute_trade_pnl + days_between
# ---------------------------------------------------------------------------


def test_compute_trade_pnl_positive() -> None:
    pnl, pct = compute_trade_pnl(entry_price=100.0, exit_price=120.0, qty=10)
    assert pnl == pytest.approx(200.0)
    assert pct == pytest.approx(20.0)


def test_compute_trade_pnl_negative() -> None:
    pnl, pct = compute_trade_pnl(entry_price=100.0, exit_price=90.0, qty=10)
    assert pnl == pytest.approx(-100.0)
    assert pct == pytest.approx(-10.0)


def test_compute_trade_pnl_zero_entry_returns_zero_pct() -> None:
    pnl, pct = compute_trade_pnl(entry_price=0.0, exit_price=10.0, qty=5)
    assert pnl == pytest.approx(50.0)
    assert pct == 0.0


def test_days_between_basic() -> None:
    assert days_between("2026-05-01T09:15:00", "2026-05-15T15:30:00") == 14
    assert days_between("2026-05-15T09:15:00", "2026-05-15T15:30:00") == 0
    # Past dates clamp to 0
    assert days_between("2026-05-20T09:15:00", "2026-05-15T09:15:00") == 0


# ---------------------------------------------------------------------------
# close_with_exit — pnl persistence + idempotency guard
# ---------------------------------------------------------------------------


def test_close_with_exit_persists_pnl_and_reason(conn: sqlite3.Connection) -> None:
    res = log_signal_and_open_trade(
        conn, signal=_signal(), entry_ts="2026-05-15T09:20:00",
        entry_price=300.0, qty=10, atr_at_entry=6.5,
    )
    closed = close_with_exit(
        conn, res.paper_trade_id,
        exit_ts="2026-05-20T15:30:00",
        exit_price=360.0,
        exit_reason="TARGET",
        days_held=5,
    )
    assert closed.exit_reason == "TARGET"
    assert closed.pnl == pytest.approx(600.0)
    assert closed.pnl_pct == pytest.approx(20.0)
    assert closed.days_held == 5
    assert closed.ts_exit == "2026-05-20T15:30:00"


def test_close_with_exit_raises_on_missing_trade(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        close_with_exit(
            conn, 9999, exit_ts="2026-05-20T15:30:00",
            exit_price=300, exit_reason="STOP", days_held=1,
        )


def test_close_with_exit_raises_on_already_closed(conn: sqlite3.Connection) -> None:
    res = log_signal_and_open_trade(
        conn, signal=_signal(), entry_ts="2026-05-15T09:20:00",
        entry_price=300, qty=10, atr_at_entry=6.5,
    )
    close_with_exit(
        conn, res.paper_trade_id, exit_ts="2026-05-20T15:30:00",
        exit_price=360, exit_reason="TARGET", days_held=5,
    )
    with pytest.raises(ValueError, match="already closed"):
        close_with_exit(
            conn, res.paper_trade_id, exit_ts="2026-05-21T15:30:00",
            exit_price=370, exit_reason="TARGET", days_held=6,
        )


# ---------------------------------------------------------------------------
# open_trades pass-through
# ---------------------------------------------------------------------------


def test_open_trades_returns_only_open(conn: sqlite3.Connection) -> None:
    res1 = log_signal_and_open_trade(
        conn, signal=_signal(symbol="A"), entry_ts="2026-05-15T09:20:00",
        entry_price=100, qty=10, atr_at_entry=2.0,
    )
    log_signal_and_open_trade(
        conn, signal=_signal(symbol="B"), entry_ts="2026-05-15T09:20:00",
        entry_price=200, qty=5, atr_at_entry=4.0,
    )
    close_with_exit(
        conn, res1.paper_trade_id, exit_ts="2026-05-18T15:30:00",
        exit_price=110, exit_reason="TARGET", days_held=3,
    )
    opens = open_trades(conn)
    assert len(opens) == 1
    # The remaining open trade is the B one (id 2)
    sig = get_signal(conn, opens[0].signal_id)
    assert sig is not None and sig.symbol == "B"
