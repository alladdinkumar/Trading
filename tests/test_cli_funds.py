"""Tests for the `trading funds` CLI sub-app."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trading.cli import app


def test_funds_add_writes_row_and_prints_balance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    result = CliRunner().invoke(
        app, ["funds", "add", "50000", "--note", "June", "--date", "2026-06-19"]
    )
    assert result.exit_code == 0, result.output
    assert "50,000" in result.output or "50000" in result.output


def test_funds_add_rejects_non_positive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    result = CliRunner().invoke(app, ["funds", "add", "0"])
    assert result.exit_code != 0


def test_funds_list_shows_initial_and_topups(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    CliRunner().invoke(app, ["funds", "add", "25000", "--date", "2026-06-19"])
    result = CliRunner().invoke(app, ["funds", "list"])
    assert result.exit_code == 0, result.output
    assert "Initial capital" in result.output
    assert "25,000" in result.output or "25000" in result.output


def test_funds_balance_reflects_topup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    CliRunner().invoke(app, ["funds", "add", "30000", "--date", "2026-06-19"])
    result = CliRunner().invoke(app, ["funds", "balance", "--date", "2026-06-19"])
    assert result.exit_code == 0, result.output
    # Total funds in = 100,000 initial + 30,000 top-up = 130,000.
    assert "130,000" in result.output or "130000" in result.output


def test_funds_top_up_deposits_gap_to_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    result = CliRunner().invoke(
        app, ["funds", "top-up", "--to-available", "150000", "--date", "2026-06-19"]
    )
    assert result.exit_code == 0, result.output
    # Fresh book: cash = 100k seed → deposits the 50k gap to reach 150k available.
    balance = CliRunner().invoke(app, ["funds", "balance", "--date", "2026-06-19"])
    assert "150,000" in balance.output or "150000" in balance.output


def test_funds_top_up_idempotent_when_already_funded(tmp_path: Path, monkeypatch) -> None:
    from trading.config import get_paths
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    CliRunner().invoke(app, ["funds", "top-up", "--to-available", "150000", "--date", "2026-06-19"])
    result = CliRunner().invoke(
        app, ["funds", "top-up", "--to-available", "150000", "--date", "2026-06-19"]
    )
    assert result.exit_code == 0, result.output
    assert "already" in result.output.lower()
    with get_conn(get_paths().db_path) as conn:
        run_migrations(conn)
        n = conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0]
    assert n == 1  # second call wrote nothing
