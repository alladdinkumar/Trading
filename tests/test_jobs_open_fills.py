"""Tests for trading.jobs.open_fills — live-LTP paper fill block."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from freezegun import freeze_time

from trading.config import get_paths
from trading.jobs.open_fills import OpenFillsAborted, run_open_fills
from trading.paper.pending import PendingEntry, write_pending_entries
from trading.store.db import get_conn
from trading.store.migrations import run_migrations

AS_OF = date(2026, 6, 23)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    p = get_paths()
    with get_conn(p.db_path) as conn:
        run_migrations(conn)
    return p


def _quote_row(symbol: str, token: int, ltp: float) -> dict:
    return {
        "instrument_token": token,
        "last_price": ltp,
        "volume": 100,
        "open": ltp,
        "high": ltp,
        "low": ltp,
        "close": ltp,
        "bid": None,
        "ask": None,
        "oi": None,
        "upper_circuit_limit": None,
        "lower_circuit_limit": None,
        "tradingsymbol": symbol,
    }


def _write_quotes(paths, hhmm: str, ltp_by_symbol: dict[str, float]) -> None:
    base = paths.raw_dir / AS_OF.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    rows = [
        _quote_row(sym, i + 1, ltp)
        for i, (sym, ltp) in enumerate(ltp_by_symbol.items())
    ]
    (base / f"quotes_{hhmm}.json").write_text(json.dumps(rows), encoding="utf-8")


def test_prepare_writes_quote_symbols(paths) -> None:
    write_pending_entries(
        paths,
        AS_OF,
        regime="NEUTRAL",
        entries=[PendingEntry(symbol="TATASTEEL", atr_14=4.30, ml_score=0.6, ref_close=198.97)],
    )
    result = run_open_fills(AS_OF, paths=paths, apply=False)
    assert result.symbols_path is not None
    assert "TATASTEEL" in result.symbols_path.read_text(encoding="utf-8").split()


@freeze_time("2026-06-23 09:21:00")
def test_apply_opens_at_live_ltp_with_recomputed_stop(paths) -> None:
    write_pending_entries(
        paths,
        AS_OF,
        regime="NEUTRAL",
        entries=[PendingEntry(symbol="TATASTEEL", atr_14=4.0, ml_score=0.6, ref_close=198.97)],
    )
    _write_quotes(paths, "0920", {"TATASTEEL": 205.0})  # gapped up from 198.97

    result = run_open_fills(AS_OF, paths=paths, apply=True)
    assert result.trades_opened == 1
    with get_conn(paths.db_path) as conn:
        row = conn.execute(
            "SELECT pt.entry_price, pt.current_stop, pt.qty "
            "FROM paper_trades pt JOIN signals s ON s.id=pt.signal_id "
            "WHERE s.symbol='TATASTEEL' AND pt.ts_exit IS NULL"
        ).fetchone()
    assert row["entry_price"] == pytest.approx(205.0)  # live LTP, not 198.97
    assert row["current_stop"] == pytest.approx(205.0 - 1.5 * 4.0)  # stop from LTP
    assert row["qty"] >= 1
    assert result.update_path is not None and result.update_path.is_file()


@freeze_time("2026-06-23 09:21:00")
def test_apply_skips_when_planner_funds_zero(paths) -> None:
    """An LTP so high one share exceeds the ₹7k daily cap → planner funds 0,
    symbol is reported skipped, nothing opens."""
    write_pending_entries(
        paths,
        AS_OF,
        regime="NEUTRAL",
        entries=[PendingEntry(symbol="EXPENSIVE", atr_14=100.0, ml_score=0.6, ref_close=9_000_000.0)],
    )
    _write_quotes(paths, "0920", {"EXPENSIVE": 9_000_000.0})

    result = run_open_fills(AS_OF, paths=paths, apply=True)
    assert result.trades_opened == 0
    assert any("EXPENSIVE" in w for w in result.warnings)


@freeze_time("2026-06-23 09:21:00")
def test_apply_skips_reentry_into_already_open_name(paths) -> None:
    """F-048: an open lot from a *prior* day (so `already_opened_today` is False)
    still blocks a fresh fill — the planner's per-symbol lot cap refuses lot #2."""
    from trading.paper.ledger import log_signal_and_open_trade
    from trading.store.repo import Signal

    with get_conn(paths.db_path) as conn:
        sig = Signal(
            id=None, ts="2026-06-19T09:20:00", symbol="TATASTEEL", side="LONG",
            entry=199.0, stop=192.0, target=210.0, horizon_days=25, created_by="test",
        )
        log_signal_and_open_trade(
            conn, signal=sig, entry_ts="2026-06-19T09:20:00",
            entry_price=199.0, qty=10, atr_at_entry=4.0,
        )
        conn.commit()

    write_pending_entries(
        paths,
        AS_OF,
        regime="NEUTRAL",
        entries=[PendingEntry(symbol="TATASTEEL", atr_14=4.0, ml_score=0.6, ref_close=198.97)],
    )
    _write_quotes(paths, "0920", {"TATASTEEL": 205.0})

    result = run_open_fills(AS_OF, paths=paths, apply=True)
    assert result.trades_opened == 0
    assert any("TATASTEEL" in w and "lot" in w.lower() for w in result.warnings)


@freeze_time("2026-06-23 09:21:00")
def test_apply_missing_quotes_aborts(paths) -> None:
    write_pending_entries(
        paths,
        AS_OF,
        regime="NEUTRAL",
        entries=[PendingEntry(symbol="TATASTEEL", atr_14=4.0, ml_score=0.6, ref_close=198.97)],
    )
    with pytest.raises(OpenFillsAborted):
        run_open_fills(AS_OF, paths=paths, apply=True)  # no quotes_*.json written


def test_apply_no_pending_is_noop(paths) -> None:
    result = run_open_fills(AS_OF, paths=paths, apply=True)
    assert result.trades_opened == 0
    assert result.update_path is None
