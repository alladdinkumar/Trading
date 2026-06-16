"""Tests for trading.paper.mtm — exit-loop persistence + trailing ratchet."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from trading.paper.ledger import log_signal_and_open_trade, open_trades
from trading.paper.mtm import (
    MtmResult,
    build_bars_from_history,
    mtm_open_trades,
)
from trading.store.migrations import run_migrations
from trading.store.repo import Signal, get_paper_trade
from trading.strategy.exits import Bar


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _signal(
    symbol: str = "X", entry: float = 100.0, stop: float = 90.0, target: float = 120.0
) -> Signal:
    return Signal(
        id=None,
        ts="2026-05-15T09:15:00",
        symbol=symbol,
        side="LONG",
        entry=entry,
        stop=stop,
        target=target,
        horizon_days=20,
        created_by="auto",
    )


def _open(
    conn: sqlite3.Connection, *, symbol="X", entry=100.0, stop=90.0, target=120.0, qty=10, atr=2.0
) -> int:
    """Helper: create one open paper trade."""
    res = log_signal_and_open_trade(
        conn,
        signal=_signal(symbol=symbol, entry=entry, stop=stop, target=target),
        entry_ts="2026-05-15T09:20:00",
        entry_price=entry,
        qty=qty,
        atr_at_entry=atr,
    )
    return res.paper_trade_id


# ---------------------------------------------------------------------------
# Exit branches — verify each one closes the trade with the right reason/pnl
# ---------------------------------------------------------------------------


def test_mtm_stop_hit_closes_trade(conn: sqlite3.Connection) -> None:
    trade_id = _open(conn, entry=100, stop=90, target=120, qty=10)
    bars = {"X": Bar(open=95, high=96, low=89, close=92)}  # low ≤ stop
    results = mtm_open_trades(conn, bars, as_of=datetime(2026, 5, 16, 15, 30))

    assert len(results) == 1
    r = results[0]
    assert r.action == "EXIT_STOP"
    assert r.exit_price == pytest.approx(90.0)
    assert r.symbol == "X"

    trade = get_paper_trade(conn, trade_id)
    assert trade is not None
    assert trade.exit_reason == "STOP"
    assert trade.exit_price == pytest.approx(90.0)
    assert trade.pnl == pytest.approx(-100.0)  # (90-100) * 10
    assert trade.pnl_pct == pytest.approx(-10.0)


def test_mtm_target_hit_closes_trade(conn: sqlite3.Connection) -> None:
    """target = min(+20%, 2.5*R/R) = min(120, 100 + 2.5*10) = min(120, 125) = 120."""
    trade_id = _open(conn, entry=100, stop=90, target=120, qty=10)
    bars = {"X": Bar(open=115, high=125, low=114, close=121)}
    results = mtm_open_trades(conn, bars, as_of=datetime(2026, 5, 20, 15, 30))

    assert results[0].action == "EXIT_TARGET"
    trade = get_paper_trade(conn, trade_id)
    assert trade is not None
    assert trade.exit_reason == "TARGET"
    assert trade.exit_price == pytest.approx(120.0)
    assert trade.pnl == pytest.approx(200.0)


def test_mtm_time_stop_closes_at_close(conn: sqlite3.Connection) -> None:
    """days_held=25 + close ≤ entry → EXIT_TIME at the close.

    days_held is now derived from (ts_entry, as_of): entry 2026-05-15 (Fri),
    as_of 2026-06-19 (Fri) → busday_count = 25 weekdays."""
    trade_id = _open(conn, entry=100, stop=90, qty=10, atr=2.0)
    bars = {"X": Bar(open=99, high=102, low=98, close=99)}
    results = mtm_open_trades(conn, bars, as_of=datetime(2026, 6, 19, 15, 30))

    assert results[0].action == "EXIT_TIME"
    trade = get_paper_trade(conn, trade_id)
    assert trade is not None
    assert trade.exit_reason == "TIME"
    assert trade.exit_price == pytest.approx(99.0)
    assert trade.days_held == 25


def test_mtm_hold_persists_ratcheted_stop_and_days(conn: sqlite3.Connection) -> None:
    trade_id = _open(conn, entry=100, stop=90, qty=10, atr=2.0)
    # +10% trail → stop moves to entry (breakeven)
    bars = {"X": Bar(open=108, high=111, low=107, close=110)}
    results = mtm_open_trades(conn, bars, as_of=datetime(2026, 5, 20, 15, 30))

    assert results[0].action == "HOLD"
    assert results[0].new_stop == pytest.approx(100.0)
    trade = get_paper_trade(conn, trade_id)
    assert trade is not None
    assert trade.current_stop == pytest.approx(100.0)
    assert trade.days_held == 3  # busday_count(2026-05-15, 2026-05-20)
    assert trade.ts_exit is None  # still open


def test_mtm_trail_ratchets_across_runs(conn: sqlite3.Connection) -> None:
    """Two consecutive MTM passes should ratchet the stop higher each time."""
    trade_id = _open(conn, entry=100, stop=90, qty=10, atr=2.0)
    # Run 1: +10% → stop to entry (100)
    mtm_open_trades(
        conn,
        {"X": Bar(98, 112, 97, 110)},
        as_of=datetime(2026, 5, 20, 15, 30),
    )
    trade_after_1 = get_paper_trade(conn, trade_id)
    assert trade_after_1 is not None
    assert trade_after_1.current_stop == pytest.approx(100.0)
    # Run 2: +15% → stop to close - ATR = 115 - 2 = 113
    mtm_open_trades(
        conn,
        {"X": Bar(110, 118, 108, 115)},
        as_of=datetime(2026, 5, 21, 15, 30),
    )
    trade_after_2 = get_paper_trade(conn, trade_id)
    assert trade_after_2 is not None
    assert trade_after_2.current_stop == pytest.approx(113.0)
    assert trade_after_2.days_held == 4  # busday_count(2026-05-15, 2026-05-21)


def test_mtm_days_held_does_not_double_count_same_day(conn: sqlite3.Connection) -> None:
    """F-024: days_held is derived from (ts_entry, as_of), so the twice-daily
    mid-day + post-close passes on one calendar day don't double-count it.

    Entry 2026-05-15 (Fri); both passes use as_of 2026-05-20 (Wed) →
    busday_count = 3 (Fri 15, Mon 18, Tue 19). Two passes must both yield 3,
    not 3 then 6."""
    trade_id = _open(conn, entry=100, stop=90, qty=10, atr=2.0)
    bar = {"X": Bar(open=101, high=103, low=99, close=101)}
    mtm_open_trades(conn, bar, as_of=datetime(2026, 5, 20, 12, 30))  # mid-day
    after_mid = get_paper_trade(conn, trade_id)
    assert after_mid is not None
    assert after_mid.days_held == 3

    mtm_open_trades(conn, bar, as_of=datetime(2026, 5, 20, 15, 30))  # post-close
    after_close = get_paper_trade(conn, trade_id)
    assert after_close is not None
    assert after_close.days_held == 3  # NOT 6 — same calendar day


def test_mtm_skips_when_bar_missing(conn: sqlite3.Connection) -> None:
    trade_id = _open(conn, symbol="X")
    results = mtm_open_trades(conn, bars={}, as_of=datetime(2026, 5, 20, 15, 30))
    assert results[0].action == "SKIP"
    assert "missing bar" in (results[0].note or "")
    # Trade still open, untouched
    trade = get_paper_trade(conn, trade_id)
    assert trade is not None
    assert trade.ts_exit is None
    assert trade.days_held == 0  # not bumped


def test_mtm_processes_multiple_trades_in_one_pass(conn: sqlite3.Connection) -> None:
    _open(conn, symbol="A", entry=100, stop=90)
    b = _open(conn, symbol="B", entry=200, stop=180)
    bars = {
        "A": Bar(95, 96, 89, 92),  # stop hit
        "B": Bar(210, 215, 205, 212),  # hold (+6%)
    }
    results = mtm_open_trades(conn, bars, as_of=datetime(2026, 5, 16, 15, 30))
    by_sym = {r.symbol: r for r in results}
    assert by_sym["A"].action == "EXIT_STOP"
    assert by_sym["B"].action == "HOLD"

    # A closed, B still open
    open_now = open_trades(conn)
    assert len(open_now) == 1
    assert open_now[0].id == b


def test_mtm_returns_empty_when_no_open_trades(conn: sqlite3.Connection) -> None:
    assert mtm_open_trades(conn, bars={"X": Bar(1, 2, 1, 1.5)}, as_of=datetime.now()) == []


# ---------------------------------------------------------------------------
# build_bars_from_history — translate parquet histories → Bar map
# ---------------------------------------------------------------------------


def _history(n: int = 30, *, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100.0 + np.cumsum(rng.normal(0, 1, n))
    idx = pd.date_range("2026-04-15", periods=n, freq="B")
    return pd.DataFrame(
        {"open": closes - 0.5, "high": closes + 1, "low": closes - 1, "close": closes},
        index=idx,
    )


def test_build_bars_picks_row_at_or_before_as_of() -> None:
    df = _history(n=10)
    target_date = datetime(2026, 4, 22, 15, 30)  # within range
    bars = build_bars_from_history({"X": df}, target_date)
    assert "X" in bars
    assert bars["X"].close == pytest.approx(
        float(df.loc[df.index <= pd.Timestamp("2026-04-22")].iloc[-1]["close"])
    )


def test_build_bars_skips_symbols_with_no_history_before_target() -> None:
    df = _history(n=10)  # starts 2026-04-15
    bars = build_bars_from_history({"X": df}, datetime(2026, 1, 1, 15, 30))
    assert bars == {}


def test_build_bars_handles_empty_dataframe() -> None:
    bars = build_bars_from_history({"X": pd.DataFrame()}, datetime(2026, 5, 1))
    assert bars == {}


def test_build_bars_drops_nan_rows() -> None:
    df = _history(n=10)
    df.iloc[-1, df.columns.get_loc("close")] = float("nan")
    bars = build_bars_from_history({"X": df}, datetime(2026, 4, 30, 15, 30))
    # Should fall back to the prior (non-NaN) row
    assert "X" in bars


def test_mtm_result_dataclass_field_defaults() -> None:
    """MtmResult.note defaults to None when not provided."""
    r = MtmResult(
        paper_trade_id=1,
        symbol="X",
        action="HOLD",
        new_stop=None,
        exit_price=None,
        reason="held",
    )
    assert r.note is None
