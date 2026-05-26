from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest

from trading.config import Paths
from trading.store.model_registry import (
    SHARPE_PROMOTION_DEADBAND,
    RegistryRow,
    active,
    all_rows,
    register,
    save_model,
)


def _paths(tmp_path: Path) -> Paths:
    return Paths(
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


def _row(version: str, sharpe: float = 1.0, *, active_flag: bool = False) -> RegistryRow:
    return RegistryRow(
        version=version,
        trained_at=datetime.now(UTC).isoformat(),
        train_start="2023-05-01",
        train_end="2026-05-01",
        oos_sharpe=sharpe,
        oos_hit_rate=0.50,
        n_train_examples=100,
        n_features=20,
        path=f"models/ranker_{version}.pkl",
        active=active_flag,
        notes="test",
    )


def _toy_model() -> lgb.LGBMClassifier:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(40, 4))
    y = (X[:, 0] > 0).astype(int)
    m = lgb.LGBMClassifier(n_estimators=10, num_leaves=5, verbose=-1)
    m.fit(X, y)
    return m


def test_active_returns_none_when_no_registry(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert active(paths) is None
    assert all_rows(paths) == []


def test_register_first_row_with_promote_becomes_active(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    pkl_path = paths.models_dir / "ranker_2026-05-01.pkl"
    save_model(pkl_path, _toy_model(), ("a", "b", "c", "d"))

    row = _row("2026-05-01", sharpe=1.0)
    became_active = register(paths, row=row, promote=True)
    assert became_active is True

    rows = all_rows(paths)
    assert len(rows) == 1
    assert rows[0].active is True
    assert rows[0].oos_sharpe == pytest.approx(1.0)


def test_register_without_promote_stays_inactive(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_2026-05-01.pkl", _toy_model(), ("a",))
    became = register(paths, row=_row("2026-05-01"), promote=False)
    assert became is False
    assert active(paths) is None


def test_promotion_deadband_blocks_marginal_improvement(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_2026-04-01.pkl", _toy_model(), ("a",))
    save_model(paths.models_dir / "ranker_2026-05-01.pkl", _toy_model(), ("a",))

    register(paths, row=_row("2026-04-01", sharpe=1.00), promote=True)
    # New model is +0.04 better — under the 0.05 deadband.
    promoted = register(
        paths,
        row=_row("2026-05-01", sharpe=1.00 + SHARPE_PROMOTION_DEADBAND - 0.01),
        promote=True,
    )
    assert promoted is False
    rows = all_rows(paths)
    assert sum(1 for r in rows if r.active) == 1
    active_row = next(r for r in rows if r.active)
    assert active_row.version == "2026-04-01"


def test_promotion_above_deadband_flips_active(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_2026-04-01.pkl", _toy_model(), ("a",))
    save_model(paths.models_dir / "ranker_2026-05-01.pkl", _toy_model(), ("a",))

    register(paths, row=_row("2026-04-01", sharpe=1.00), promote=True)
    promoted = register(paths, row=_row("2026-05-01", sharpe=1.10), promote=True)
    assert promoted is True
    rows = all_rows(paths)
    active_row = next(r for r in rows if r.active)
    assert active_row.version == "2026-05-01"
    inactive = [r for r in rows if not r.active]
    assert len(inactive) == 1
    assert inactive[0].version == "2026-04-01"


def test_active_loads_model_and_feature_names(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(
        paths.models_dir / "ranker_2026-05-01.pkl", _toy_model(), ("a", "b", "c", "d")
    )
    register(paths, row=_row("2026-05-01"), promote=True)
    am = active(paths)
    assert am is not None
    assert am.feature_names == ("a", "b", "c", "d")
    proba = am.model.predict_proba(np.zeros((1, 4)))
    assert proba.shape == (1, 2)


def test_nan_oos_sharpe_blocks_promotion(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_a.pkl", _toy_model(), ("a",))
    promoted = register(paths, row=_row("a", sharpe=math.nan), promote=True)
    assert promoted is False
    assert active(paths) is None
