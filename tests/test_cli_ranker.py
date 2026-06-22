from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest
from typer.testing import CliRunner

from trading.cli import app
from trading.config import Paths
from trading.ranking.ranker_features import FEATURE_NAMES
from trading.store.model_registry import RegistryRow, register, save_model


@pytest.fixture()
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=tmp_path / "data" / "logs",
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.parquet_dir.mkdir(parents=True, exist_ok=True)
    return paths


def test_ranker_status_empty(isolated_paths: Paths) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["ranker-status"])
    assert result.exit_code == 0
    assert "no models registered" in result.output.lower()


def test_ranker_status_lists_rows(isolated_paths: Paths) -> None:
    isolated_paths.models_dir.mkdir(parents=True, exist_ok=True)
    pkl = isolated_paths.models_dir / "ranker_test.pkl"
    m = lgb.LGBMClassifier(n_estimators=5, num_leaves=4, verbose=-1)
    m.fit(np.zeros((20, len(FEATURE_NAMES))), np.array([0, 1] * 10))
    save_model(pkl, m, FEATURE_NAMES)
    register(
        isolated_paths,
        row=RegistryRow(
            version="test",
            trained_at=datetime.now(UTC).isoformat(),
            train_start="2022-01-01",
            train_end="2024-12-31",
            oos_sharpe=1.23,
            oos_hit_rate=0.51,
            n_train_examples=100,
            n_features=len(FEATURE_NAMES),
            path=str(pkl.relative_to(isolated_paths.project_root)),
            active=True,
            notes="hello",
        ),
        promote=True,
    )
    runner = CliRunner()
    result = runner.invoke(app, ["ranker-status"])
    assert result.exit_code == 0
    assert "test" in result.output
    assert "1.23" in result.output or "1.2" in result.output


def test_train_ranker_refuses_when_no_parquet(isolated_paths: Paths) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["train-ranker", "--start", "2024-01-01", "--end", "2024-06-01"]
    )
    # Empty parquet dir → exit 2 with "no parquet symbols".
    assert result.exit_code == 2
    assert "no parquet symbols" in result.output.lower()
