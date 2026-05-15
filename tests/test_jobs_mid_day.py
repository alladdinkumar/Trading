"""Tests for trading.jobs.mid_day — orchestrator + helpers."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from tests.conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.jobs.mid_day import (
    MidDayResult,
    gather_quote_symbols,
    run_mid_day,
)
from trading.store.migrations import run_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_HOLDING_ROW = {
    "tradingsymbol": "COALINDIA", "exchange": "NSE", "isin": "INE522F01014",
    "quantity": 69, "average_price": 463.68, "last_price": 462.2,
    "close_price": 454.05, "pnl": -102.25, "day_change": 8.15,
    "day_change_percentage": 1.79,
}


def _seed_open_trade(conn: sqlite3.Connection, symbol: str = "RVNL") -> None:
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-05-15T08:30:00", symbol, 305.0, 290.0, 360.0),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, "
        "current_stop, atr_at_entry) VALUES (?, ?, ?, ?, ?, ?)",
        (sig_id, "2026-05-15T08:30:00", 305.0, 32, 295.0, 8.4),
    )
    conn.commit()


def test_gather_quote_symbols_unions_paper_signals_holdings(
    conn, paths
) -> None:
    _seed_open_trade(conn, "RVNL")
    conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-05-16T08:30:00", "NTPC", 395.0, 380.0, 470.0),
    )
    conn.commit()
    seed_kite_snapshot(paths, date(2026, 5, 16), holdings=[_HOLDING_ROW])
    out = gather_quote_symbols(conn, paths, date(2026, 5, 16))
    assert out == sorted({"RVNL", "NTPC", "COALINDIA"})


def test_gather_quote_symbols_degrades_when_holdings_missing(
    conn, paths
) -> None:
    _seed_open_trade(conn, "RVNL")
    out = gather_quote_symbols(conn, paths, date(2026, 5, 16))
    assert out == ["RVNL"]


def test_run_mid_day_prepare_writes_symbol_file(paths) -> None:
    # run_mid_day opens its own connection against paths.db_path, so seed
    # the file db (not an in-memory fixture).
    from trading.store.db import get_conn
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade(file_conn, "RVNL")
    seed_kite_snapshot(paths, date(2026, 5, 16), holdings=[_HOLDING_ROW])
    result = run_mid_day(date(2026, 5, 16), paths=paths, apply=False)
    assert isinstance(result, MidDayResult)
    assert result.symbols_path is not None
    assert result.symbols_path.is_file()
    body = result.symbols_path.read_text(encoding="utf-8")
    assert body.split("\n") == ["COALINDIA", "RVNL", ""]
    assert result.update_path is None
    assert result.trades_evaluated == 0
