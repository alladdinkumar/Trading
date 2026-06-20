from __future__ import annotations

import math
from datetime import UTC, date, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from trading.config import Paths
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.model_registry import RegistryRow, register, save_model
from trading.store.ohlcv import write_ohlcv
from trading.strategy.ranker import conviction_from_score, score_and_filter
from trading.strategy.ranker_features import FEATURE_NAMES
from trading.strategy.rules import Candidate, RuleResult


def test_conviction_from_score_bands() -> None:
    # F-038: ml_score (predict_proba 0..1) → HIGH/MEDIUM/LOW band.
    assert conviction_from_score(0.72) == "HIGH"
    assert conviction_from_score(0.60) == "HIGH"  # boundary inclusive
    assert conviction_from_score(0.55) == "MEDIUM"
    assert conviction_from_score(0.50) == "MEDIUM"  # boundary inclusive
    assert conviction_from_score(0.40) == "LOW"
    # Cold-start (no active model) → no conviction, not a fabricated LOW.
    assert conviction_from_score(None) is None


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


def _ohlcv(seed: int = 0, n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n)
    close = 100 + np.cumsum(rng.normal(0.20, 0.20, size=n))
    return pd.DataFrame(
        {
            "open": close + rng.uniform(-0.5, 0.5, size=n),
            "high": close + rng.uniform(0.1, 1.0, size=n),
            "low": close - rng.uniform(0.1, 1.0, size=n),
            "close": close,
            "volume": rng.integers(80_000, 120_000, size=n).astype(int),
        },
        index=dates,
    )


def _seed_universe(paths: Paths, symbols: list[str]) -> None:
    paths.parquet_dir.mkdir(parents=True, exist_ok=True)
    for i, sym in enumerate(symbols):
        write_ohlcv(_ohlcv(seed=i), sym, paths)


def _candidate(sym: str, scan_date: date) -> Candidate:
    return Candidate(
        symbol=sym,
        scan_date=scan_date,
        close=100.0,
        rsi_14=40.0,
        sma_20=99.0,
        sma_50=98.0,
        sma_200=95.0,
        atr_14=2.0,
        rules=(RuleResult("uptrend", True),),
        bar_date=scan_date,
    )


def test_cold_start_returns_all_candidates_selected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.parquet_dir.mkdir(parents=True, exist_ok=True)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    sym = "RELIANCE"
    _seed_universe(paths, [sym])
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        cands = [_candidate(sym, date(2024, 12, 31))]
        out = score_and_filter(cands, paths, conn, date(2024, 12, 31), k=5)
    assert len(out) == 1
    assert out[0].ml_score is None
    assert out[0].selected is True


def _toy_model() -> lgb.LGBMClassifier:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(100, len(FEATURE_NAMES)))
    y = (X[:, 0] > 0).astype(int)
    m = lgb.LGBMClassifier(n_estimators=20, num_leaves=8, verbose=-1)
    m.fit(X, y)
    return m


def _register_active_model(paths: Paths) -> None:
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    pkl = paths.models_dir / "ranker_2024-12-31.pkl"
    save_model(pkl, _toy_model(), FEATURE_NAMES)
    register(
        paths,
        row=RegistryRow(
            version="2024-12-31",
            trained_at=datetime.now(UTC).isoformat(),
            train_start="2022-01-01",
            train_end="2024-12-31",
            oos_sharpe=1.0,
            oos_hit_rate=0.5,
            n_train_examples=100,
            n_features=len(FEATURE_NAMES),
            path=str(pkl.relative_to(paths.project_root)),
            active=True,
            notes="test",
        ),
        promote=True,
    )


def test_active_model_scores_and_filters_to_top_k(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    syms = [f"SYM{i}" for i in range(8)]
    _seed_universe(paths, syms)
    _register_active_model(paths)

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        cands = [_candidate(s, date(2024, 12, 31)) for s in syms]
        out = score_and_filter(cands, paths, conn, date(2024, 12, 31), k=3)

    assert len(out) == 8
    selected = [s for s in out if s.selected]
    assert len(selected) == 3
    for sc in out:
        assert sc.ml_score is not None and not math.isnan(sc.ml_score)
    scores_selected = sorted([s.ml_score for s in selected], reverse=True)
    scores_all = sorted([s.ml_score for s in out], reverse=True)
    assert scores_selected == scores_all[:3]


def test_missing_pkl_falls_back_to_cold_start(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    paths.parquet_dir.mkdir(parents=True, exist_ok=True)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)

    register(
        paths,
        row=RegistryRow(
            version="2024-12-31",
            trained_at="2024-12-31T00:00:00+00:00",
            train_start="2022-01-01",
            train_end="2024-12-31",
            oos_sharpe=1.0,
            oos_hit_rate=0.5,
            n_train_examples=100,
            n_features=len(FEATURE_NAMES),
            path="models/missing.pkl",
            active=True,
            notes="dangling",
        ),
        promote=True,
    )
    _seed_universe(paths, ["RELIANCE"])
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        out = score_and_filter(
            [_candidate("RELIANCE", date(2024, 12, 31))],
            paths,
            conn,
            date(2024, 12, 31),
            k=5,
        )
    assert out[0].ml_score is None
    assert out[0].selected is True


def test_k_larger_than_candidates_selects_all(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_universe(paths, ["A", "B"])
    _register_active_model(paths)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        cands = [_candidate("A", date(2024, 12, 31)), _candidate("B", date(2024, 12, 31))]
        out = score_and_filter(cands, paths, conn, date(2024, 12, 31), k=10)
    assert all(sc.selected for sc in out)


# ---------------------------------------------------------------------------
# RankerSignalProvider
# ---------------------------------------------------------------------------


def test_ranker_signal_provider_truncates_to_top_k(tmp_path: Path) -> None:
    from trading.backtest.engine import Signal
    from trading.features.technicals import add_indicators
    from trading.store.model_registry import active as load_active
    from trading.strategy.ranker import RankerSignalProvider

    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.parquet_dir.mkdir(parents=True, exist_ok=True)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    _register_active_model(paths)

    enriched: dict[str, pd.DataFrame] = {}
    for i in range(6):
        sym = f"SYM{i}"
        df = _ohlcv(seed=i + 10, n=260)
        enriched[sym] = add_indicators(df)
        write_ohlcv(df, sym, paths)

    sd = enriched["SYM0"].index[-1]
    signals = [Signal(symbol=f"SYM{i}", close=100.0, atr=2.0, stop_price=97.0) for i in range(6)]

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        am = load_active(paths)
        assert am is not None
        provider = RankerSignalProvider(am.model, am.feature_names, paths, conn, top_k=3)
        out = provider.score_signals(signals, enriched, sd)
    assert len(out) == 3
