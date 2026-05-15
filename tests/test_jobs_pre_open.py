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


from unittest.mock import patch

from trading.data.macro import MacroSnapshot
from trading.features.regime import RegimeResult
from trading.jobs.pre_open import _step_macro


def test_step_macro_writes_snapshot_and_returns_regime(
    conn: sqlite3.Connection,
) -> None:
    snap = MacroSnapshot(
        date=date(2026, 5, 15), sgx_nifty=None, dow_fut=None,
        nasdaq_fut=None, sp500=None, usdinr=95.0, crude=None,
        vix=18.0, us_10y=None, fii_flow_cr=200.0, dii_flow_cr=500.0,
        regime="RISK_ON",
    )
    rr = RegimeResult(
        regime="RISK_ON", composite_score=2,
        vix_vote=1, futures_vote=0, fii_vote=1, usdinr_vote=0,
        reasons=["VIX low", "FII positive"],
    )
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.snapshot_and_classify", return_value=(snap, rr)
    ):
        ok, regime = _step_macro(conn, date(2026, 5, 15), warnings)
    assert ok is True
    assert regime == "RISK_ON"
    row = conn.execute(
        "SELECT vix, regime FROM macro_snapshot WHERE date = ?",
        ("2026-05-15",),
    ).fetchone()
    assert row is not None
    assert row["vix"] == 18.0
    assert row["regime"] == "RISK_ON"
    assert warnings == []


def test_step_macro_degrades_gracefully_on_fetch_error(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.snapshot_and_classify",
        side_effect=RuntimeError("yfinance down"),
    ):
        ok, regime = _step_macro(conn, date(2026, 5, 15), warnings)
    assert ok is False
    assert regime == "NEUTRAL"
    assert any("macro" in w.lower() for w in warnings)
    assert conn.execute(
        "SELECT COUNT(*) FROM macro_snapshot"
    ).fetchone()[0] == 0
