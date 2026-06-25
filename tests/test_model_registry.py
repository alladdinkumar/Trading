from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest

from trading.config import Paths
from trading.store.model_registry import (
    MIN_OOS_TRADES,
    SHARPE_PROMOTION_DEADBAND,
    SHARPE_PROMOTION_FLOOR,
    RegistryRow,
    _is_usable,
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


def _row(
    version: str,
    sharpe: float = 1.5,
    *,
    active_flag: bool = False,
    n_oos_trades: int = 60,
    n_folds_positive: int = 3,
    n_folds_total: int = 4,
) -> RegistryRow:
    """A registry row that is *usable* by default (t = 1.5·√(60/12) = 3.35 ≥ 2.0,
    breadth 3/4). Override the gate inputs to construct unusable rows."""
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
        n_oos_trades=n_oos_trades,
        n_folds_positive=n_folds_positive,
        n_folds_total=n_folds_total,
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
    save_model(paths.models_dir / "ranker_2026-05-01.pkl", _toy_model(), ("a", "b", "c", "d"))
    register(paths, row=_row("2026-05-01"), promote=True)
    am = active(paths)
    assert am is not None
    assert am.feature_names == ("a", "b", "c", "d")
    proba = am.model.predict_proba(np.zeros((1, 4)))
    assert proba.shape == (1, 2)


def test_save_model_round_trips_threshold(tmp_path: Path) -> None:
    """Tier 2b: the calibrated act/pass threshold travels with the model so
    inference can apply it."""
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(
        paths.models_dir / "ranker_2026-05-01.pkl",
        _toy_model(),
        ("a", "b", "c", "d"),
        threshold=0.57,
    )
    register(paths, row=_row("2026-05-01"), promote=True)
    am = active(paths)
    assert am is not None
    assert am.threshold == pytest.approx(0.57)


def test_active_threshold_defaults_none_for_old_pkl(tmp_path: Path) -> None:
    """Backward compat: a model saved without a threshold loads with None."""
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_2026-05-01.pkl", _toy_model(), ("a", "b", "c", "d"))
    register(paths, row=_row("2026-05-01"), promote=True)
    am = active(paths)
    assert am is not None
    assert am.threshold is None


def test_nan_oos_sharpe_blocks_promotion(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_a.pkl", _toy_model(), ("a",))
    promoted = register(paths, row=_row("a", sharpe=math.nan), promote=True)
    assert promoted is False
    assert active(paths) is None


def test_negative_sharpe_first_model_blocked_by_floor(tmp_path: Path) -> None:
    # F-043: the first model to ever train must still clear an absolute OOS-Sharpe
    # floor — a negative-Sharpe model has no demonstrated edge and must not promote
    # (so the EV planner keeps its p_win=prior rather than consuming an unproven score).
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_a.pkl", _toy_model(), ("a",))
    promoted = register(paths, row=_row("a", sharpe=-1.49), promote=True)
    assert promoted is False
    assert active(paths) is None
    # The run is still recorded (inactive), so history/idempotency are preserved.
    rows = all_rows(paths)
    assert len(rows) == 1
    assert rows[0].active is False


def test_zero_sharpe_does_not_clear_floor(tmp_path: Path) -> None:
    # The floor demands a *positive* OOS Sharpe; exactly-zero is no edge.
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_a.pkl", _toy_model(), ("a",))
    promoted = register(paths, row=_row("a", sharpe=SHARPE_PROMOTION_FLOOR), promote=True)
    assert promoted is False
    assert active(paths) is None


def test_marginally_positive_sharpe_fails_tstat_gate(tmp_path: Path) -> None:
    # F-046: a barely-positive Sharpe clears the old floor but its t-stat
    # (0.01·√(60/12) = 0.022) is far below T_MIN=2.0 — no statistical edge.
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_a.pkl", _toy_model(), ("a",))
    promoted = register(paths, row=_row("a", sharpe=SHARPE_PROMOTION_FLOOR + 0.01), promote=True)
    assert promoted is False
    assert active(paths) is None


def test_usable_first_model_promotes(tmp_path: Path) -> None:
    # Strong edge (t = 1.5·√(60/12) = 3.35 ≥ 2.0) over a majority of folds.
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_a.pkl", _toy_model(), ("a",))
    promoted = register(paths, row=_row("a", sharpe=1.5), promote=True)
    assert promoted is True
    assert active(paths) is not None


def test_register_demotes_preexisting_subfloor_active(tmp_path: Path) -> None:
    # F-043: a model promoted *before* the floor existed (e.g. the live -1.49 one)
    # may sit active in registry.csv. The next register call must demote it so we
    # never keep serving a sub-floor score — even when the new model is also weak.
    from trading.store.model_registry import _write_all_rows

    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    _write_all_rows(paths, [_row("2026-06-18", sharpe=-1.49, active_flag=True)])
    assert sum(1 for r in all_rows(paths) if r.active) == 1

    save_model(paths.models_dir / "ranker_2026-06-25.pkl", _toy_model(), ("a",))
    promoted = register(paths, row=_row("2026-06-25", sharpe=-1.20), promote=True)

    assert promoted is False
    assert active(paths) is None  # cold-start: no model served
    assert sum(1 for r in all_rows(paths) if r.active) == 0


def test_positive_model_replaces_subfloor_active(tmp_path: Path) -> None:
    from trading.store.model_registry import _write_all_rows

    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    _write_all_rows(paths, [_row("2026-06-18", sharpe=-1.49, active_flag=True)])
    save_model(paths.models_dir / "ranker_2026-06-25.pkl", _toy_model(), ("a",))

    promoted = register(paths, row=_row("2026-06-25", sharpe=1.5), promote=True)

    assert promoted is True
    rows = all_rows(paths)
    active_row = next(r for r in rows if r.active)
    assert active_row.version == "2026-06-25"
    assert sum(1 for r in rows if r.active) == 1


def test_has_row_for_train_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.config import get_paths
    from trading.store.model_registry import (
        RegistryRow,
        has_row_for_train_end,
        register,
    )

    p = get_paths()
    assert has_row_for_train_end(p, "2026-06-14") is False

    register(
        p,
        row=RegistryRow(
            version="2026-06-14",
            trained_at="2026-06-14T05:00:00+00:00",
            train_start="2023-06-14",
            train_end="2026-06-14",
            oos_sharpe=float("nan"),
            oos_hit_rate=float("nan"),
            n_train_examples=40,
            n_features=20,
            path="models/ranker_2026-06-14.pkl",
            active=False,
            notes="",
        ),
        promote=False,
    )
    assert has_row_for_train_end(p, "2026-06-14") is True
    assert has_row_for_train_end(p, "2026-06-21") is False


def test_is_usable_requires_tstat_not_just_positive_sharpe() -> None:
    # positive Sharpe but tiny N → t = 0.5·√(20/12) = 0.645 < 2.0 → not usable
    assert not _is_usable(0.5, 20, 2, 2)
    # below the computability floor regardless of t
    assert not _is_usable(5.0, MIN_OOS_TRADES - 1, 2, 2)
    # strong, broad, enough trades: t = 1.2·√(60/12) = 2.68 ≥ 2.0
    assert _is_usable(1.2, 60, 2, 2)


def test_is_usable_requires_fold_breadth() -> None:
    # t passes (t = 1.2·√(60/12) = 2.68) but only 1 of 4 folds positive
    assert not _is_usable(1.2, 60, 1, 4)
    # majority of folds positive
    assert _is_usable(1.2, 60, 2, 4)


def test_is_usable_nan_never_passes() -> None:
    assert not _is_usable(float("nan"), 100, 4, 4)


def test_registry_csv_roundtrip_new_columns(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    save_model(paths.models_dir / "ranker_a.pkl", _toy_model(), ("a",))
    register(paths, row=_row("a", sharpe=1.5), promote=True)
    (got,) = all_rows(paths)
    assert got.n_oos_trades == 60
    assert got.n_folds_positive == 3
    assert got.n_folds_total == 4
