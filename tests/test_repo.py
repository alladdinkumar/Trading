"""Tests for trading.store.repo — typed CRUD for signals, paper_trades, predictions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.repo import (
    PaperTrade,
    Prediction,
    Signal,
    close_paper_trade,
    get_paper_trade,
    get_prediction,
    get_signal,
    insert_paper_trade,
    insert_prediction,
    insert_signal,
    list_open_paper_trades,
    list_predictions_by_symbol,
    list_signals_by_date,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Migrated, foreign-keys-on connection scoped to a single test."""
    with get_conn(tmp_path / "repo.db") as c:
        run_migrations(c)
        yield c


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------


def _sample_signal(symbol: str = "RVNL", ts: str = "2026-05-11T09:10:00") -> Signal:
    return Signal(
        id=None,
        ts=ts,
        symbol=symbol,
        side="LONG",
        entry=305.0,
        stop=290.0,
        target=365.0,
        horizon_days=15,
        rules_passed_json='["uptrend","rsi_dip","volume_low"]',
        ml_score=0.71,
        conviction="MEDIUM",
        rationale="Pullback to 20DMA with positive sentiment.",
        created_by="auto",
    )


def test_ranker_eval_log_insert_and_latest(conn: sqlite3.Connection) -> None:
    from trading.store.repo import (
        RankerEval,
        insert_ranker_eval,
        latest_ranker_eval,
    )

    assert latest_ranker_eval(conn) is None
    insert_ranker_eval(conn, RankerEval(
        as_of="2026-06-21", pooled_sharpe=-0.3, pooled_hit=0.27,
        n_oos=38, n_folds_pos=2, n_folds_total=7, usable=False,
        note="not usable", created_at="2026-06-21T00:00:00Z",
    ))
    insert_ranker_eval(conn, RankerEval(
        as_of="2026-06-28", pooled_sharpe=1.2, pooled_hit=0.55,
        n_oos=60, n_folds_pos=4, n_folds_total=6, usable=True,
        note="usable", created_at="2026-06-28T00:00:00Z",
    ))
    latest = latest_ranker_eval(conn)
    assert latest is not None
    assert latest.as_of == "2026-06-28"
    assert latest.usable is True
    # PK as_of makes a same-week re-run idempotent (INSERT OR REPLACE).
    insert_ranker_eval(conn, RankerEval(
        as_of="2026-06-28", pooled_sharpe=1.3, pooled_hit=0.56,
        n_oos=61, n_folds_pos=4, n_folds_total=6, usable=True,
        note="rerun", created_at="2026-06-28T01:00:00Z",
    ))
    rows = conn.execute("SELECT COUNT(*) AS c FROM ranker_eval_log").fetchone()
    assert rows["c"] == 2


def test_insert_and_get_signal(conn: sqlite3.Connection) -> None:
    sig = _sample_signal()
    sig_id = insert_signal(conn, sig)
    assert sig_id > 0
    fetched = get_signal(conn, sig_id)
    assert fetched is not None
    assert fetched.id == sig_id
    assert fetched.symbol == "RVNL"
    assert fetched.side == "LONG"
    assert fetched.entry == 305.0
    assert fetched.conviction == "MEDIUM"
    assert fetched.rules_passed_json == '["uptrend","rsi_dip","volume_low"]'


def test_get_signal_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert get_signal(conn, 9999) is None


def test_list_signals_by_date_filters(conn: sqlite3.Connection) -> None:
    insert_signal(conn, _sample_signal(symbol="RVNL", ts="2026-05-11T09:10:00"))
    insert_signal(conn, _sample_signal(symbol="NTPC", ts="2026-05-11T09:15:00"))
    insert_signal(conn, _sample_signal(symbol="IRB", ts="2026-05-12T09:10:00"))
    same_day = list_signals_by_date(conn, "2026-05-11")
    assert len(same_day) == 2
    assert {s.symbol for s in same_day} == {"RVNL", "NTPC"}


def test_signal_optional_fields_can_be_none(conn: sqlite3.Connection) -> None:
    sig = Signal(
        id=None,
        ts="2026-05-11T09:10:00",
        symbol="JIOFIN",
        side="LONG",
        entry=240.0,
        stop=228.0,
        target=290.0,
        horizon_days=20,
    )
    sig_id = insert_signal(conn, sig)
    fetched = get_signal(conn, sig_id)
    assert fetched is not None
    assert fetched.rules_passed_json is None
    assert fetched.ml_score is None
    assert fetched.conviction is None
    assert fetched.rationale is None
    assert fetched.created_by == "auto"  # default


# ---------------------------------------------------------------------------
# PaperTrade
# ---------------------------------------------------------------------------


def test_insert_paper_trade_requires_real_signal(conn: sqlite3.Connection) -> None:
    bogus = PaperTrade(
        id=None,
        signal_id=12345,
        ts_entry="2026-05-12T09:15:00",
        entry_price=305.0,
        qty=10,
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_paper_trade(conn, bogus)


def test_paper_trade_round_trip(conn: sqlite3.Connection) -> None:
    sig_id = insert_signal(conn, _sample_signal())
    trade = PaperTrade(
        id=None,
        signal_id=sig_id,
        ts_entry="2026-05-12T09:15:00",
        entry_price=305.0,
        qty=10,
    )
    trade_id = insert_paper_trade(conn, trade)
    fetched = get_paper_trade(conn, trade_id)
    assert fetched is not None
    assert fetched.signal_id == sig_id
    assert fetched.qty == 10
    assert fetched.ts_exit is None  # still open


def test_close_paper_trade(conn: sqlite3.Connection) -> None:
    sig_id = insert_signal(conn, _sample_signal())
    trade_id = insert_paper_trade(
        conn,
        PaperTrade(
            id=None,
            signal_id=sig_id,
            ts_entry="2026-05-12T09:15:00",
            entry_price=300.0,
            qty=10,
        ),
    )
    close_paper_trade(
        conn,
        trade_id,
        ts_exit="2026-05-22T15:30:00",
        exit_price=360.0,
        exit_reason="TARGET",
        pnl=600.0,
        pnl_pct=20.0,
        days_held=10,
    )
    fetched = get_paper_trade(conn, trade_id)
    assert fetched is not None
    assert fetched.ts_exit == "2026-05-22T15:30:00"
    assert fetched.exit_price == 360.0
    assert fetched.exit_reason == "TARGET"
    assert fetched.pnl == 600.0
    assert fetched.days_held == 10


def test_list_open_paper_trades_excludes_closed(conn: sqlite3.Connection) -> None:
    sig_id = insert_signal(conn, _sample_signal())
    open_id = insert_paper_trade(
        conn,
        PaperTrade(
            id=None,
            signal_id=sig_id,
            ts_entry="2026-05-12T09:15:00",
            entry_price=300.0,
            qty=10,
        ),
    )
    closed_id = insert_paper_trade(
        conn,
        PaperTrade(
            id=None,
            signal_id=sig_id,
            ts_entry="2026-05-13T09:15:00",
            entry_price=310.0,
            qty=5,
        ),
    )
    close_paper_trade(
        conn,
        closed_id,
        ts_exit="2026-05-14T15:30:00",
        exit_price=315.0,
        exit_reason="MANUAL",
        pnl=25.0,
        pnl_pct=1.6,
        days_held=1,
    )
    open_trades = list_open_paper_trades(conn)
    assert [t.id for t in open_trades] == [open_id]


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------


def test_prediction_round_trip(conn: sqlite3.Connection) -> None:
    pred = Prediction(
        id=None,
        ts="2026-05-11T09:10:00",
        symbol="RVNL",
        predicted_return_pct=12.5,
        predicted_horizon_days=15,
    )
    pid = insert_prediction(conn, pred)
    fetched = get_prediction(conn, pid)
    assert fetched is not None
    assert fetched.symbol == "RVNL"
    assert fetched.predicted_return_pct == 12.5
    assert fetched.actual_return_at_horizon is None


def test_list_predictions_by_symbol(conn: sqlite3.Connection) -> None:
    insert_prediction(
        conn,
        Prediction(
            id=None,
            ts="2026-05-11T09:10:00",
            symbol="RVNL",
            predicted_return_pct=10.0,
            predicted_horizon_days=10,
        ),
    )
    insert_prediction(
        conn,
        Prediction(
            id=None,
            ts="2026-05-12T09:10:00",
            symbol="RVNL",
            predicted_return_pct=8.0,
            predicted_horizon_days=10,
        ),
    )
    insert_prediction(
        conn,
        Prediction(
            id=None,
            ts="2026-05-11T09:15:00",
            symbol="NTPC",
            predicted_return_pct=7.0,
            predicted_horizon_days=10,
        ),
    )
    rvnl_preds = list_predictions_by_symbol(conn, "RVNL")
    assert len(rvnl_preds) == 2
    assert {p.symbol for p in rvnl_preds} == {"RVNL"}
