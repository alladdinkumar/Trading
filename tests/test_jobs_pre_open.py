"""Tests for trading.jobs.pre_open — orchestrator + each _step_*."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date
from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from trading.config import Settings, get_paths
from trading.data.macro import MacroSnapshot
from trading.data.news import RawHeadline
from trading.features.regime import RegimeResult
from trading.jobs.pre_open import (
    PreOpenResult,
    _already_opened_today,
    _step_auto_open,
    _step_macro,
    _step_news,
    _step_portfolio,
    _step_scan,
    run_pre_open,
)
from trading.store.migrations import run_migrations
from trading.store.ohlcv import write_ohlcv
from trading.strategy.rules import Candidate, RuleResult


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


def test_run_pre_open_returns_result_with_bundle_path(paths, monkeypatch) -> None:
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
        lambda paths, settings, warnings, *, as_of: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_auto_open",
        lambda conn, as_of, passing, regime, capital, risk_pct, warnings: 0,
    )

    result = run_pre_open(
        date(2026, 5, 15),
        paths=paths,
        skip_news=True,
    )
    assert isinstance(result, PreOpenResult)
    assert result.as_of == date(2026, 5, 15)
    assert result.bundle_path == paths.research_dir / "2026-05-15" / "_context.md"
    assert result.bundle_path.is_file()
    assert result.candidates_passing == 0
    assert result.paper_trades_opened == 0


def test_step_macro_writes_snapshot_and_returns_regime(
    conn: sqlite3.Connection,
) -> None:
    snap = MacroSnapshot(
        date=date(2026, 5, 15),
        sgx_nifty=None,
        dow_fut=None,
        nasdaq_fut=None,
        sp500=None,
        usdinr=95.0,
        crude=None,
        vix=18.0,
        us_10y=None,
        fii_flow_cr=200.0,
        dii_flow_cr=500.0,
        regime="RISK_ON",
    )
    rr = RegimeResult(
        regime="RISK_ON",
        composite_score=2,
        vix_vote=1,
        futures_vote=0,
        fii_vote=1,
        usdinr_vote=0,
        reasons=["VIX low", "FII positive"],
    )
    warnings: list[str] = []
    with patch("trading.jobs.pre_open.snapshot_and_classify", return_value=(snap, rr)):
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
    assert conn.execute("SELECT COUNT(*) FROM macro_snapshot").fetchone()[0] == 0


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
    with (
        patch(
            "trading.jobs.pre_open.fetch_all_news",
            return_value=[_raw_headline("RVNL")],
        ),
        patch(
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
        ),
    ):
        inserted, rollups = _step_news(conn, date(2026, 5, 15), warnings)
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
        inserted, rollups = _step_news(conn, date(2026, 5, 15), warnings)
    assert inserted == 0
    assert rollups == 0
    assert any("news" in w.lower() for w in warnings)


def _candidate(symbol: str, n_passed: int) -> Candidate:
    rules = tuple(RuleResult(name=f"r{i}", passed=(i < n_passed), reason="") for i in range(10))
    return Candidate(
        symbol=symbol,
        scan_date=date(2026, 5, 15),
        close=100.0,
        rsi_14=40.0,
        sma_20=100.0,
        sma_50=100.0,
        sma_200=100.0,
        atr_14=2.0,
        rules=rules,
    )


def test_step_scan_delegates_to_strategy(paths) -> None:
    warnings: list[str] = []
    fake = [_candidate("RVNL", 9), _candidate("NTPC", 7)]
    with patch("trading.jobs.pre_open.scan", return_value=fake):
        out = _step_scan(paths, date(2026, 5, 15), warnings)
    assert out == fake
    assert warnings == []


def _settings(token: str | None = None) -> Settings:
    return Settings(
        anthropic_api_key=None,
        kite_api_key="k",
        kite_api_secret="s",
        kite_access_token=token,
        slack_webhook_url=None,
        log_level="INFO",
        news_user_agent="test",
    )


_PRE_OPEN_HOLDING = {
    "tradingsymbol": "RVNL",
    "exchange": "NSE",
    "isin": "INE415G01027",
    "quantity": 32,
    "average_price": 305.0,
    "last_price": 329.6,
    "close_price": 327.1,
    "pnl": 787.2,
    "day_change": 2.5,
    "day_change_percentage": 0.76,
}


def test_step_portfolio_reads_snapshot_and_scores(paths) -> None:
    from tests.conftest import seed_kite_snapshot

    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[_PRE_OPEN_HOLDING])
    warnings: list[str] = []
    out = _step_portfolio(paths, _settings(), warnings, as_of=date(2026, 5, 15))
    # No parquet for RVNL in this fixture so the warning notes the skip;
    # but the snapshot read succeeded. Empty result is expected here.
    assert out == []
    assert any("no parquet" in w.lower() for w in warnings)


def test_step_portfolio_raises_pre_open_aborted_when_snapshot_missing(
    paths,
) -> None:
    from trading.jobs.pre_open import PreOpenAborted

    warnings: list[str] = []
    with pytest.raises(PreOpenAborted) as exc:
        _step_portfolio(paths, _settings(), warnings, as_of=date(2026, 5, 15))
    assert "/kite-snapshot" in str(exc.value)


def test_step_auto_open_creates_signal_and_paper_trade(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    cand = _candidate("RVNL", 10)
    opened = _step_auto_open(
        conn,
        date(2026, 5, 15),
        [cand],
        "NEUTRAL",
        capital=100_000.0,
        risk_pct=0.02,
        warnings=warnings,
    )
    assert opened == 1
    sig_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    pt_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE ts_exit IS NULL").fetchone()[0]
    assert sig_count == 1
    assert pt_count == 1


def test_step_auto_open_idempotent_on_rerun(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    cand = _candidate("RVNL", 10)
    _step_auto_open(
        conn,
        date(2026, 5, 15),
        [cand],
        "NEUTRAL",
        capital=100_000.0,
        risk_pct=0.02,
        warnings=warnings,
    )
    opened2 = _step_auto_open(
        conn,
        date(2026, 5, 15),
        [cand],
        "NEUTRAL",
        capital=100_000.0,
        risk_pct=0.02,
        warnings=warnings,
    )
    assert opened2 == 0
    pt_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert pt_count == 1


def test_already_opened_today_detects_open_trade(
    conn: sqlite3.Connection,
) -> None:
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-05-15T08:30:00", "RVNL", 100.0, 95.0, 120.0),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty) VALUES (?, ?, ?, ?)",
        (sig_id, "2026-05-15T08:30:00", 100.0, 10),
    )
    conn.commit()
    assert _already_opened_today(conn, "RVNL", date(2026, 5, 15)) is True
    assert _already_opened_today(conn, "NTPC", date(2026, 5, 15)) is False


def _all_pass_frame() -> pd.DataFrame:
    """Construct an OHLCV frame engineered for pipeline-shape testing.

    Long flat history at 100 then a recent rally. Whether all 10 rules
    pass is sensitive to threshold tuning; the test asserts structural
    invariants (bundle written, counts consistent), not a specific
    passing count.
    """
    n = 360
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    idx.name = "date"
    closes = [100.0] * (n - 30) + list(range(101, 116)) + [110.0] * 15
    df = pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [2_000_000] * n,
        },
        index=idx,
    )
    return df


def test_run_pre_open_full_happy_path_integration(paths, monkeypatch) -> None:
    write_ohlcv(_all_pass_frame(), "TESTSYM", paths)

    monkeypatch.setattr(
        "trading.jobs.pre_open._step_macro",
        lambda c, d, w: (True, "RISK_ON"),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_news",
        lambda c, d, w: (0, 0),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_portfolio",
        lambda p, s, w, *, as_of: [],
    )

    result = run_pre_open(
        date(2026, 5, 15),
        paths=paths,
        skip_news=False,
    )
    assert result.bundle_path.is_file()
    body = result.bundle_path.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "## Today's candidates" in body
    assert result.candidates_total == 1
    assert result.paper_trades_opened == result.candidates_passing

    result2 = run_pre_open(
        date(2026, 5, 15),
        paths=paths,
        skip_news=False,
    )
    assert result2.paper_trades_opened == 0


def test_pre_open_main_configures_logging_and_propagates_failure(monkeypatch, tmp_path):
    """When run_pre_open raises, _main configures logging, lets the Slack
    sink fire via logger.exception, and re-raises so exit code propagates."""
    import pytest as _pytest

    from trading.jobs import pre_open as job
    from trading.ops import logging_setup

    logger_calls: list[str] = []
    monkeypatch.setattr(logging_setup, "_configured", set())

    def fake_configure(job_name, slack_on_error=True):
        logger_calls.append(job_name)
        return tmp_path / f"{job_name}.log"

    monkeypatch.setattr(job, "configure_logging", fake_configure)

    def fake_run_pre_open(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(job, "run_pre_open", fake_run_pre_open)

    with _pytest.raises(RuntimeError, match="simulated"):
        job._main("2026-05-25")

    assert logger_calls == ["pre_open"]
