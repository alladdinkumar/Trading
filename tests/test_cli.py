"""Tests for trading.cli — typer CLI smoke + ingest-history."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
from typer.testing import CliRunner

from trading.cli import app

runner = CliRunner()


def _fake_ohlcv(rows: int = 5) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=rows, freq="B")
    idx.name = "date"
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(rows)],
            "high": [105.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "close": [104.0 + i for i in range(rows)],
            "volume": [1_000_000 + i for i in range(rows)],
        },
        index=idx,
    )


def test_app_help_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ingest-history" in result.stdout.replace("\n", " ")


def test_ingest_history_writes_parquet(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    fake = _fake_ohlcv()
    with patch("trading.cli.fetch_ohlcv", return_value=fake):
        result = runner.invoke(
            app,
            [
                "ingest-history",
                "--symbols",
                "RVNL",
                "--symbols",
                "NTPC",
                "--start",
                "2025-01-01",
                "--end",
                "2025-01-10",
            ],
        )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "data" / "parquet" / "nifty200" / "RVNL.parquet").is_file()
    assert (tmp_path / "data" / "parquet" / "nifty200" / "NTPC.parquet").is_file()


def test_ingest_history_continues_on_per_symbol_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))

    from trading.data.yfinance import OhlcvFetchError

    def fake_fetch(symbol: str, start, end, **kwargs):
        if symbol == "BAD":
            raise OhlcvFetchError(f"No data for {symbol}")
        return _fake_ohlcv()

    with patch("trading.cli.fetch_ohlcv", side_effect=fake_fetch):
        result = runner.invoke(
            app,
            [
                "ingest-history",
                "--symbols",
                "RVNL",
                "--symbols",
                "BAD",
                "--symbols",
                "NTPC",
                "--start",
                "2025-01-01",
            ],
        )
    assert result.exit_code == 0
    # Good ones should have been written
    assert (tmp_path / "data" / "parquet" / "nifty200" / "RVNL.parquet").is_file()
    assert (tmp_path / "data" / "parquet" / "nifty200" / "NTPC.parquet").is_file()
    # Bad one should not
    assert not (tmp_path / "data" / "parquet" / "nifty200" / "BAD.parquet").exists()


def test_ingest_history_skip_existing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    existing = tmp_path / "data" / "parquet" / "nifty200" / "RVNL.parquet"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"placeholder")  # pretend it exists

    with patch("trading.cli.fetch_ohlcv") as mock_fetch:
        result = runner.invoke(
            app,
            [
                "ingest-history",
                "--symbols",
                "RVNL",
                "--start",
                "2025-01-01",
                "--skip-existing",
            ],
        )
    assert result.exit_code == 0
    mock_fetch.assert_not_called()
