"""Tests for trading.jobs.post_close — orchestrator + helpers."""

from __future__ import annotations

import json as _j
import sqlite3
from datetime import date
from datetime import datetime as _dt
from pathlib import Path

import pytest
from freezegun import freeze_time

from tests.conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.jobs.post_close import (
    PostCloseAborted,
    PostCloseResult,
    run_post_close,
)
from trading.store.db import get_conn
from trading.store.migrations import run_migrations


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_HOLDING_ROW = {
    "tradingsymbol": "RVNL", "exchange": "NSE", "isin": "INE415G01027",
    "quantity": 594, "average_price": 328.0, "last_price": 283.0,
    "close_price": 287.2, "pnl": -26851.0, "day_change": -4.2,
    "day_change_percentage": -1.46,
}


# Quote shaped to trigger TIME exit (not STOP and not TARGET):
#   low=289.5  > current_stop=289 → no STOP
#   high=291.0 < target≈345       → no TARGET
#   close=290  ≤ entry=305        → TIME fires (days_held becomes 25)
_QUOTE_ROW_RVNL_TIME = {
    "instrument_token": 2445313,
    "last_price": 290.0,
    "volume": 100,
    "open": 290.0, "high": 291.0, "low": 289.5, "close": 287.2,
    "bid": 289.9, "ask": 290.1, "oi": None,
    "upper_circuit_limit": None, "lower_circuit_limit": None,
    "tradingsymbol": "RVNL",
}


def _seed_open_trade_at_day_24(conn: sqlite3.Connection) -> None:
    """Trade with days_held=24 so the +1 in mtm_open_trades pushes it to 25 (TIME exit)."""
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-04-21T08:30:00", "RVNL", 305.0, 289.0, 366.0),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, "
        "current_stop, atr_at_entry, days_held) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sig_id, "2026-04-21T08:30:00", 305.0, 32, 289.0, 8.4, 24),
    )
    conn.commit()


def _write_quotes(paths, as_of, hhmm: str, rows: list) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(_j.dumps(rows), encoding="utf-8")
    return target


def test_run_post_close_prepare_writes_symbol_file(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    seed_kite_snapshot(paths, date(2026, 5, 16), holdings=[_HOLDING_ROW])
    result = run_post_close(date(2026, 5, 16), paths=paths, apply=False)
    assert isinstance(result, PostCloseResult)
    assert result.symbols_path is not None
    assert result.symbols_path.is_file()
    body = result.symbols_path.read_text(encoding="utf-8")
    assert "RVNL" in body
    assert result.summary_path is None
    assert result.trades_evaluated == 0


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_closes_time_stop_and_writes_summary(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(
        date(2026, 5, 16), paths=paths, apply=True, cash=100_000.0
    )
    assert isinstance(result, PostCloseResult)
    assert result.quotes_capture_ts == _dt(2026, 5, 16, 16, 1)
    assert result.bars_built == 1
    assert result.trades_evaluated == 1
    assert result.trades_closed == 1   # day 24+1=25 → TIME exit
    # paper_trade is now closed
    with get_conn(paths.db_path) as file_conn:
        closed = file_conn.execute(
            "SELECT exit_reason FROM paper_trades WHERE ts_exit IS NOT NULL"
        ).fetchone()
    assert closed["exit_reason"] == "TIME"
    # portfolio snapshot row written
    with get_conn(paths.db_path) as file_conn:
        snap = file_conn.execute(
            "SELECT date, cash, equity FROM portfolio_snapshots WHERE date = ?",
            ("2026-05-16",),
        ).fetchone()
    assert snap is not None
    assert snap["cash"] == 100_000.0
    assert result.equity == snap["equity"]
    # markdown written
    assert result.summary_path is not None
    body = result.summary_path.read_text(encoding="utf-8")
    assert "## Post-close summary" in body
    assert "16:01" in body  # capture timestamp from filename HHMM
    assert "RVNL" in body
    assert "EXIT_TIME" in body
    assert "Portfolio snapshot" in body
    assert "₹" in body  # equity formatted


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_aborts_when_quotes_missing(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    with pytest.raises(PostCloseAborted) as exc:
        run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert "/kite-quotes-snapshot" in str(exc.value)


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_idempotent_on_rerun(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    r1 = run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert r1.trades_closed == 1
    # Re-run: trade already closed, snapshot UPSERT overwrites
    r2 = run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert r2.trades_evaluated == 0
    assert r2.trades_closed == 0
    # portfolio snapshot still has exactly one row for as_of
    with get_conn(paths.db_path) as file_conn:
        n = file_conn.execute(
            "SELECT COUNT(*) FROM portfolio_snapshots WHERE date = ?",
            ("2026-05-16",),
        ).fetchone()[0]
    assert n == 1


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_no_open_trades_still_writes_summary(paths) -> None:
    """Quiet day: no open trades, no matured predictions. Summary still written."""
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(
        date(2026, 5, 16), paths=paths, apply=True, cash=100_000.0
    )
    assert result.bars_built == 1
    assert result.trades_evaluated == 0
    assert result.trades_closed == 0
    assert result.predictions_matured == 0
    assert result.summary_path is not None
    body = result.summary_path.read_text(encoding="utf-8")
    assert "0 open trades evaluated" in body
    assert "_(none today)_" in body  # matured predictions section


def test_post_close_main_logging_and_failure(monkeypatch, tmp_path):
    import pytest as _pytest
    from trading.jobs import post_close as job
    from trading.ops import logging_setup

    logger_calls: list[str] = []
    monkeypatch.setattr(logging_setup, "_configured", set())

    def fake_configure(job_name, slack_on_error=True):
        logger_calls.append(job_name)
        return tmp_path / f"{job_name}.log"

    monkeypatch.setattr(job, "configure_logging", fake_configure)

    def fake_run(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(job, "run_post_close", fake_run)

    with _pytest.raises(RuntimeError, match="simulated"):
        job._main("2026-05-25", apply=False)

    assert logger_calls == ["post_close"]
