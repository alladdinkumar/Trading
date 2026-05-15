"""Tests for trading.jobs.pre_open — orchestrator + each _step_*."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.jobs.pre_open import PreOpenResult, run_pre_open
from trading.store.migrations import run_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def test_run_pre_open_returns_result_with_bundle_path(
    paths, monkeypatch
) -> None:
    """Skeleton: orchestrator returns a PreOpenResult and writes a bundle.

    Stub every upstream call so the test runs offline. Subsequent tasks
    fill in the real wiring per step.
    """
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_macro",
        lambda conn, as_of, warnings: (False, "NEUTRAL"),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_news",
        lambda conn, as_of, warnings: (0, 0),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_scan",
        lambda paths, as_of, warnings: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_portfolio",
        lambda paths, settings, warnings, skip_kite: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_auto_open",
        lambda conn, as_of, passing, regime, capital, risk_pct, warnings: 0,
    )

    result = run_pre_open(
        date(2026, 5, 15),
        paths=paths,
        skip_news=True,
        skip_kite=True,
    )
    assert isinstance(result, PreOpenResult)
    assert result.as_of == date(2026, 5, 15)
    assert result.bundle_path == paths.research_dir / "2026-05-15" / "_context.md"
    assert result.bundle_path.is_file()
    assert result.candidates_passing == 0
    assert result.paper_trades_opened == 0
