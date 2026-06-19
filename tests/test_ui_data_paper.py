"""Tests for the paper-portfolio loaders in trading.ui.data."""

from __future__ import annotations

from pathlib import Path

from trading.config import get_paths
from trading.paper.funds import add_funds
from trading.paper.ledger import log_signal_and_open_trade
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.repo import Signal
from trading.ui import data


def _seed(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        sig = Signal(
            id=None, ts="2026-06-01T09:20:00", symbol="ACME", side="LONG",
            entry=100.0, stop=90.0, target=120.0, horizon_days=15, created_by="auto",
        )
        log_signal_and_open_trade(
            conn, signal=sig, entry_ts="2026-06-01T09:20:00",
            entry_price=100.0, qty=10, atr_at_entry=2.0,
        )
        add_funds(conn, amount=20_000.0, date="2026-06-01")


def test_load_paper_trades_includes_horizon_days(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    df = data.load_paper_trades.__wrapped__()
    assert "horizon_days" in df.columns
    assert int(df.iloc[0]["horizon_days"]) == 15


def test_load_cash_ledger_returns_topups(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    df = data.load_cash_ledger.__wrapped__()
    assert list(df["amount"]) == [20_000.0]


def test_load_paper_positions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    df = data.load_paper_positions.__wrapped__("2026-06-03")
    assert list(df["symbol"]) == ["ACME"]
    assert int(df.iloc[0]["qty"]) == 10


def test_load_paper_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    summary = data.load_paper_summary("2026-06-03")
    assert summary.funds_added == 20_000.0
    assert summary.invested == 1000.0
