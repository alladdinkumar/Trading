"""Tests for trading.jobs.mid_day — orchestrator + helpers."""

from __future__ import annotations

import json as _j
import sqlite3
from datetime import date
from datetime import datetime as _dt
from pathlib import Path

import pytest
from freezegun import freeze_time

from tests.conftest import seed_kite_snapshot
from trading.clock import IST
from trading.config import get_paths
from trading.data.kite import Quote
from trading.jobs.mid_day import (
    MidDayAborted,
    MidDayResult,
    _quotes_to_bars,
    gather_quote_symbols,
    run_mid_day,
)
from trading.store.migrations import run_migrations
from trading.strategy.exits import Bar


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
    "tradingsymbol": "COALINDIA",
    "exchange": "NSE",
    "isin": "INE522F01014",
    "quantity": 69,
    "average_price": 463.68,
    "last_price": 462.2,
    "close_price": 454.05,
    "pnl": -102.25,
    "day_change": 8.15,
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


def test_gather_quote_symbols_unions_paper_signals_holdings(conn, paths) -> None:
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


def test_gather_quote_symbols_degrades_when_holdings_missing(conn, paths) -> None:
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


_QUOTE_ROW_RVNL = {
    "instrument_token": 2445313,
    "last_price": 280.0,  # below current_stop=295 → triggers EXIT_STOP
    "volume": 100,
    "open": 305.0,
    "high": 305.5,
    "low": 280.0,
    "close": 305.0,
    "bid": 279.9,
    "ask": 280.1,
    "oi": None,
    "upper_circuit_limit": None,
    "lower_circuit_limit": None,
    "tradingsymbol": "RVNL",
}


def _write_quotes(paths, as_of, hhmm: str, rows: list) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(_j.dumps(rows), encoding="utf-8")
    return target


def test_quotes_to_bars_uses_last_price_as_close() -> None:
    q = Quote(
        instrument_token=1,
        last_price=395.25,
        volume=8123456,
        open=396.30,
        high=397.10,
        low=393.80,
        close=396.30,
        bid=395.20,
        ask=395.30,
        oi=None,
        upper_circuit_limit=None,
        lower_circuit_limit=None,
    )
    bars = _quotes_to_bars({"NTPC": q})
    assert bars["NTPC"] == Bar(
        open=396.30,
        high=397.10,
        low=393.80,
        close=395.25,
    )


@freeze_time("2026-05-16T12:33:00+05:30")
def test_run_mid_day_apply_closes_stop_hit_and_writes_markdown(paths) -> None:
    from trading.store.db import get_conn

    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade(file_conn, "RVNL")
    _write_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW_RVNL])
    result = run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert isinstance(result, MidDayResult)
    assert result.quotes_capture_ts == _dt(2026, 5, 16, 12, 32, tzinfo=IST)
    assert result.bars_built == 1
    assert result.trades_evaluated == 1
    assert result.trades_closed == 1
    assert result.trades_held == 0
    # paper_trade is now closed
    with get_conn(paths.db_path) as file_conn:
        closed = file_conn.execute(
            "SELECT exit_reason, exit_price FROM paper_trades WHERE ts_exit IS NOT NULL"
        ).fetchone()
    assert closed["exit_reason"] == "STOP"
    assert closed["exit_price"] is not None
    # markdown written
    assert result.update_path is not None
    body = result.update_path.read_text(encoding="utf-8")
    assert "## Mid-day update" in body
    assert "RVNL" in body
    assert "EXIT_STOP" in body


@freeze_time("2026-05-16T12:33:00+05:30")
def test_run_mid_day_apply_aborts_when_quotes_missing(paths) -> None:
    from trading.store.db import get_conn

    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade(file_conn, "RVNL")
    with pytest.raises(MidDayAborted) as exc:
        run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert "/kite-quotes-snapshot" in str(exc.value)


@freeze_time("2026-05-16T12:33:00+05:30")
def test_run_mid_day_apply_idempotent_on_rerun(paths) -> None:
    from trading.store.db import get_conn

    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade(file_conn, "RVNL")
    _write_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW_RVNL])
    r1 = run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert r1.trades_closed == 1
    # Re-run: trade already closed → mtm_open_trades sees no open trades
    r2 = run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert r2.trades_evaluated == 0
    assert r2.trades_closed == 0


def test_mid_day_main_logging_and_failure(monkeypatch, tmp_path):
    import pytest as _pytest

    from trading.jobs import mid_day as job
    from trading.ops import logging_setup

    logger_calls: list[str] = []
    monkeypatch.setattr(logging_setup, "_configured", set())

    def fake_configure(job_name, slack_on_error=True):
        logger_calls.append(job_name)
        return tmp_path / f"{job_name}.log"

    monkeypatch.setattr(job, "configure_logging", fake_configure)

    def fake_run(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(job, "run_mid_day", fake_run)

    with _pytest.raises(RuntimeError, match="simulated"):
        job._main("2026-05-25", apply=False)

    assert logger_calls == ["mid_day"]
