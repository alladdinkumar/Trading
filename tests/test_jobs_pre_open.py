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


from datetime import UTC, datetime as _dt

from trading.data.news import RawHeadline
from trading.jobs.pre_open import _step_news


def _raw_headline(symbol: str = "RVNL") -> RawHeadline:
    return RawHeadline(
        ts=_dt(2026, 5, 14, 10, 0, tzinfo=UTC),
        source="moneycontrol",
        headline=f"{symbol} test headline",
        url=f"https://example.com/{symbol.lower()}",
    )


def test_step_news_inserts_headlines_and_aggregates(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.fetch_all_news",
        return_value=[_raw_headline("RVNL")],
    ), patch(
        "trading.jobs.pre_open.score_news_items",
        side_effect=lambda items: [
            __import__("trading.data.news", fromlist=["NewsItem"]).NewsItem(
                ts=i.ts.isoformat(),
                symbol="RVNL",
                source=i.source,
                headline=i.headline,
                url=i.url,
                sentiment=0.5,
                category="results",
                is_critical=False,
            )
            for i in items
        ],
    ):
        inserted, rollups = _step_news(
            conn, date(2026, 5, 15), warnings
        )
    assert inserted == 1
    assert rollups == 1
    assert warnings == []


def test_step_news_degrades_gracefully_on_fetch_error(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.fetch_all_news",
        side_effect=RuntimeError("RSS down"),
    ):
        inserted, rollups = _step_news(
            conn, date(2026, 5, 15), warnings
        )
    assert inserted == 0
    assert rollups == 0
    assert any("news" in w.lower() for w in warnings)


from trading.jobs.pre_open import _step_scan
from trading.strategy.rules import Candidate, RuleResult


def _candidate(symbol: str, n_passed: int) -> Candidate:
    rules = tuple(
        RuleResult(name=f"r{i}", passed=(i < n_passed), reason="")
        for i in range(10)
    )
    return Candidate(
        symbol=symbol, scan_date=date(2026, 5, 15),
        close=100.0, rsi_14=40.0, sma_20=100.0, sma_50=100.0,
        sma_200=100.0, atr_14=2.0, rules=rules,
    )


def test_step_scan_delegates_to_strategy(paths) -> None:
    warnings: list[str] = []
    fake = [_candidate("RVNL", 9), _candidate("NTPC", 7)]
    with patch("trading.jobs.pre_open.scan", return_value=fake):
        out = _step_scan(paths, date(2026, 5, 15), warnings)
    assert out == fake
    assert warnings == []


from trading.config import Settings
from trading.data.kite import KiteAuthError
from trading.jobs.pre_open import _step_portfolio


def _settings(token: str | None = None) -> Settings:
    return Settings(
        anthropic_api_key=None, kite_api_key="k",
        kite_api_secret="s", kite_access_token=token,
        log_level="INFO", news_user_agent="test",
    )


def test_step_portfolio_returns_empty_when_skip_kite(paths) -> None:
    warnings: list[str] = []
    out = _step_portfolio(paths, _settings(token="x"), warnings, skip_kite=True)
    assert out == []
    assert any("kite" in w.lower() for w in warnings)


def test_step_portfolio_returns_empty_when_no_token(paths) -> None:
    warnings: list[str] = []
    out = _step_portfolio(paths, _settings(token=None), warnings, skip_kite=False)
    assert out == []
    assert any("kite token" in w.lower() for w in warnings)


def test_step_portfolio_degrades_on_kite_auth_error(paths) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.make_client", side_effect=KiteAuthError("expired")
    ):
        out = _step_portfolio(paths, _settings(token="x"), warnings, skip_kite=False)
    assert out == []
    assert any("kite auth" in w.lower() for w in warnings)
