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
