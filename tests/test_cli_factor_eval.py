from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trading.cli import app

runner = CliRunner()


def test_factor_eval_errors_cleanly_with_no_data(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Point the CLI at an empty project root → no parquet → exit 1, no traceback.
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    result = runner.invoke(app, ["factor-eval", "--start", "2023-01-01"])
    assert result.exit_code == 1
    assert "No parquet" in result.stdout
