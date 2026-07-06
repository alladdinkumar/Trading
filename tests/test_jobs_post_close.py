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
from trading.clock import IST
from trading.config import get_paths
from trading.jobs.post_close import (
    PostCloseAborted,
    PostCloseResult,
    run_post_close,
)
from trading.paper.ledger import buy_side_cost, sell_side_cost
from trading.store.db import get_conn
from trading.store.migrations import run_migrations


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_HOLDING_ROW = {
    "tradingsymbol": "RVNL",
    "exchange": "NSE",
    "isin": "INE415G01027",
    "quantity": 594,
    "average_price": 328.0,
    "last_price": 283.0,
    "close_price": 287.2,
    "pnl": -26851.0,
    "day_change": -4.2,
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
    "open": 290.0,
    "high": 291.0,
    "low": 289.5,
    "close": 287.2,
    "bid": 289.9,
    "ask": 290.1,
    "oi": None,
    "upper_circuit_limit": None,
    "lower_circuit_limit": None,
    "tradingsymbol": "RVNL",
}


def _seed_open_trade_at_time_stop(conn: sqlite3.Connection) -> None:
    """Trade entered so that at as_of 2026-05-16 it has been held 25 business days.

    days_held is derived from (ts_entry, as_of) (F-024), so entry 2026-04-13 (Mon)
    → busday_count(2026-04-13, 2026-05-16) = 25 → the time stop fires.
    """
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-04-13T08:30:00", "RVNL", 305.0, 289.0, 366.0),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, "
        "current_stop, atr_at_entry, days_held) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sig_id, "2026-04-13T08:30:00", 305.0, 32, 289.0, 8.4, 0),
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
        _seed_open_trade_at_time_stop(file_conn)
    seed_kite_snapshot(paths, date(2026, 5, 16), holdings=[_HOLDING_ROW])
    result = run_post_close(date(2026, 5, 16), paths=paths, apply=False)
    assert isinstance(result, PostCloseResult)
    assert result.symbols_path is not None
    assert result.symbols_path.is_file()
    body = result.symbols_path.read_text(encoding="utf-8")
    assert "RVNL" in body
    assert result.summary_path is None
    assert result.trades_evaluated == 0


@freeze_time("2026-05-16T16:01:23+05:30")
def test_run_post_close_apply_closes_time_stop_and_writes_summary(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_time_stop(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(date(2026, 5, 16), paths=paths, apply=True, initial_capital=100_000.0)
    assert isinstance(result, PostCloseResult)
    assert result.quotes_capture_ts == _dt(2026, 5, 16, 16, 1, tzinfo=IST)
    assert result.bars_built == 1
    assert result.trades_evaluated == 1
    assert result.trades_closed == 1  # 25 business days held → TIME exit
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
    # F-023 + F-025: cash reflects the closed trade net of round-trip costs —
    # debit 305*32 + buy_side_cost, credit 290*32 - sell_side_cost.
    entry_value, exit_value = 305.0 * 32, 290.0 * 32
    expected_cash = (
        100_000.0
        - entry_value
        - buy_side_cost(entry_value)
        + exit_value
        - sell_side_cost(exit_value)
    )
    assert snap["cash"] == pytest.approx(expected_cash)
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


@freeze_time("2026-05-16T16:01:23+05:30")
def test_run_post_close_apply_gate_sharpe_is_na_with_thin_history(paths) -> None:
    """F-061: with fewer than 2 daily returns of equity history the go-live
    gate Sharpe must be None / "n/a" — not a crash, not a misleading 0.0."""
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(date(2026, 5, 16), paths=paths, apply=True, initial_capital=100_000.0)
    assert result.gate_sharpe is None
    body = result.summary_path.read_text(encoding="utf-8")
    assert "Gate Sharpe (daily, annualised): n/a" in body


@freeze_time("2026-05-20T16:01:23+05:30")
def test_run_post_close_apply_computes_gate_sharpe_from_full_equity_history(paths) -> None:
    """F-061: PostCloseResult.gate_sharpe is the daily-annualised Sharpe of the
    *full* portfolio_snapshots.equity history (matching `portfolio_gate_sharpe`
    directly), not the per-trade ratios shown elsewhere."""
    from trading.paper.reconcile import portfolio_gate_sharpe

    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        for d, equity in [
            ("2026-05-16", 100_000.0),
            ("2026-05-17", 101_000.0),
            ("2026-05-18", 100_500.0),
            ("2026-05-19", 102_000.0),
        ]:
            file_conn.execute(
                "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity) "
                "VALUES (?, ?, '{}', ?)",
                (d, equity, equity),
            )
        file_conn.commit()
    _write_quotes(paths, date(2026, 5, 20), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(date(2026, 5, 20), paths=paths, apply=True, initial_capital=100_000.0)
    with get_conn(paths.db_path) as file_conn:
        expected = portfolio_gate_sharpe(file_conn)
    assert expected is not None
    assert result.gate_sharpe == pytest.approx(expected)
    body = result.summary_path.read_text(encoding="utf-8")
    assert f"Gate Sharpe (daily, annualised): {expected:.2f}" in body


@freeze_time("2026-05-16T16:01:23+05:30")
def test_run_post_close_apply_aborts_when_quotes_missing(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_time_stop(file_conn)
    with pytest.raises(PostCloseAborted) as exc:
        run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert "/kite-quotes-snapshot" in str(exc.value)


@freeze_time("2026-05-17T10:00:00+05:30")
def test_run_post_close_apply_honors_max_age_for_backfill(paths) -> None:
    """Next-day backfill: quotes captured 2026-05-16 16:01, run on 2026-05-17.

    The default 30-min freshness ceiling rejects the ~18h-old snapshot, but an
    explicit large max_age_minutes lifts the ceiling so the backfill proceeds.
    """
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_time_stop(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])

    # Default ceiling rejects the stale snapshot.
    with pytest.raises(PostCloseAborted) as exc:
        run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert "stale" in str(exc.value)

    # Explicit override lifts the ceiling for the backfill.
    result = run_post_close(date(2026, 5, 16), paths=paths, apply=True, max_age_minutes=100_000)
    assert result.quotes_capture_ts == _dt(2026, 5, 16, 16, 1, tzinfo=IST)
    assert result.trades_closed == 1
    assert result.summary_path is not None


@freeze_time("2026-05-16T16:01:23+05:30")
def test_run_post_close_apply_idempotent_on_rerun(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_time_stop(file_conn)
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


@freeze_time("2026-05-16T16:01:23+05:30")
def test_run_post_close_apply_no_open_trades_still_writes_summary(paths) -> None:
    """Quiet day: no open trades, no matured predictions. Summary still written."""
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(date(2026, 5, 16), paths=paths, apply=True, initial_capital=100_000.0)
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
