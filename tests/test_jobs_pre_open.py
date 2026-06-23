"""Tests for trading.jobs.pre_open — orchestrator + each _step_*."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date
from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from trading.config import Settings, get_paths
from trading.data.news import RawHeadline
from trading.domain import MacroSnapshot, SectorRow
from trading.features.regime import RegimeResult
from trading.jobs.pre_open import (
    PreOpenResult,
    _step_macro,
    _step_news,
    _step_ohlcv,
    _step_plan_and_record,
    _step_portfolio,
    _step_scan,
    _step_sector,
    build_scan_context,
    run_pre_open,
)
from trading.paper.pending import read_pending_entries
from trading.paper.positions import already_opened_today
from trading.ranking.ranker import ScoredCandidate
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.ohlcv import write_ohlcv
from trading.strategy.rules import (
    Candidate,
    RuleResult,
    passes_market_filter,
    passes_no_critical_event,
)


def _sc(
    cand: Candidate, *, ml_score: float | None = None, selected: bool = True
) -> ScoredCandidate:
    """Wrap a Candidate in a default-selected ScoredCandidate."""
    return ScoredCandidate(candidate=cand, ml_score=ml_score, selected=selected)


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
        "trading.jobs.pre_open._step_sector",
        lambda conn, as_of, warnings: False,
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_news",
        lambda conn, as_of, warnings: (0, 0),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_ohlcv",
        lambda paths, as_of, warnings: 0,
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_scan",
        lambda conn, paths, as_of, warnings: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_portfolio",
        lambda paths, settings, warnings, *, as_of, **_: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_plan_and_record",
        lambda conn, as_of, scored, regime, warnings, paths: 0,
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
    assert result.pending_entries == 0


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
        bar_date=date(2026, 5, 15),
    )


def test_step_scan_delegates_to_strategy(conn, paths) -> None:
    warnings: list[str] = []
    fake = [_candidate("RVNL", 9), _candidate("NTPC", 7)]
    with (
        patch(
            "trading.jobs.pre_open.load_candidate_universe",
            return_value=["RVNL", "NTPC"],
        ),
        patch("trading.jobs.pre_open.scan", return_value=fake),
    ):
        out = _step_scan(conn, paths, date(2026, 5, 15), warnings)
    assert out == fake
    assert warnings == []


def test_step_scan_restricts_to_candidate_universe(conn, paths) -> None:
    """_step_scan passes the Nifty-50 candidate list, not the full universe."""
    candidates = ["RELIANCE", "INFY", "TCS"]
    with (
        patch(
            "trading.jobs.pre_open.load_candidate_universe",
            return_value=candidates,
        ),
        patch("trading.jobs.pre_open.scan", return_value=[]) as mock_scan,
    ):
        _step_scan(conn, paths, date(2026, 5, 15), [])
    assert mock_scan.call_args.kwargs["symbols"] == candidates


def test_step_scan_passes_warnings_accumulator(conn, paths) -> None:
    """_step_scan threads the run warnings list into scan() for staleness."""
    warnings: list[str] = []
    with (
        patch("trading.jobs.pre_open.load_candidate_universe", return_value=["FOO"]),
        patch("trading.jobs.pre_open.scan", return_value=[]) as mock_scan,
    ):
        _step_scan(conn, paths, date(2026, 5, 15), warnings)
    assert mock_scan.call_args.kwargs["warnings"] is warnings


def _seed_macro_vix(conn: sqlite3.Connection, as_of: date, vix: float) -> None:
    from trading.store.macro_store import upsert_macro_snapshot

    upsert_macro_snapshot(
        conn,
        MacroSnapshot(
            date=as_of.isoformat(),
            sgx_nifty=None,
            dow_fut=None,
            nasdaq_fut=None,
            sp500=None,
            usdinr=None,
            crude=None,
            vix=vix,
            us_10y=None,
            fii_flow_cr=None,
            dii_flow_cr=None,
            regime="NEUTRAL",
        ),
    )


def _seed_critical(conn: sqlite3.Connection, as_of: date, symbol: str) -> None:
    from trading.store.news_store import SentimentDailyRow, upsert_sentiment_daily

    upsert_sentiment_daily(
        conn,
        SentimentDailyRow(
            date=as_of.isoformat(),
            symbol=symbol,
            score_7d=-0.4,
            score_30d=-0.2,
            news_count=3,
            negative_news_count=2,
            has_critical=True,
        ),
    )


def test_build_scan_context_pulls_vix_and_critical(conn) -> None:
    """F-019: context is populated from this run's macro + sentiment data."""
    as_of = date(2026, 5, 15)
    _seed_macro_vix(conn, as_of, vix=27.5)
    _seed_critical(conn, as_of, "RVNL")

    ctx = build_scan_context(conn, as_of)
    assert ctx.india_vix == 27.5
    assert "RVNL" in ctx.critical_event_symbols
    # The critical symbol now trips the hard veto that was previously dead.
    assert not passes_no_critical_event("RVNL", ctx).passed


def test_build_scan_context_empty_when_no_data(conn) -> None:
    ctx = build_scan_context(conn, date(2026, 5, 15))
    assert ctx.india_vix is None
    assert ctx.critical_event_symbols == frozenset()
    # No macro/sentiment ⇒ gates degrade to passing (indicator-only).
    assert passes_market_filter(ctx).passed
    assert passes_no_critical_event("ANY", ctx).passed


def test_step_ohlcv_returns_bars_and_surfaces_warnings(paths) -> None:
    from trading.data.ohlcv_refresh import RefreshResult

    fake = RefreshResult(
        symbols_refreshed=2, symbols_failed=1, bars_added=7, warnings=["BAD: boom"]
    )
    warnings: list[str] = []
    with patch("trading.jobs.pre_open.refresh_ohlcv", return_value=fake):
        added = _step_ohlcv(paths, date(2026, 5, 15), warnings)
    assert added == 7
    assert warnings == ["BAD: boom"]


def test_step_ohlcv_degrades_on_top_level_failure(paths) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.refresh_ohlcv",
        side_effect=RuntimeError("universe missing"),
    ):
        added = _step_ohlcv(paths, date(2026, 5, 15), warnings)
    assert added == 0
    assert any("ohlcv refresh failed" in w for w in warnings)


def test_step_ohlcv_runs_before_scan(paths, monkeypatch) -> None:
    """The orchestrator refreshes OHLCV before reading it in the scan."""
    order: list[str] = []
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_ohlcv",
        lambda p, d, w: order.append("ohlcv") or 0,
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_scan",
        lambda c, p, d, w: order.append("scan") or [],
    )
    monkeypatch.setattr("trading.jobs.pre_open._step_macro", lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr("trading.jobs.pre_open._step_sector", lambda c, d, w: False)
    monkeypatch.setattr("trading.jobs.pre_open._step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr("trading.jobs.pre_open._step_portfolio", lambda p, s, w, *, as_of, **_: [])

    result = run_pre_open(date(2026, 5, 15), paths=paths, skip_news=True)
    assert order == ["ohlcv", "scan"]
    assert result.ohlcv_bars_added == 0


def test_cross_check_warnings_surface_in_run(paths, monkeypatch) -> None:
    from tests.conftest import seed_kite_snapshot

    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[_PRE_OPEN_HOLDING])
    monkeypatch.setattr("trading.jobs.pre_open._step_ohlcv", lambda p, d, w: 0)
    monkeypatch.setattr("trading.jobs.pre_open._step_scan", lambda c, p, d, w: [])
    monkeypatch.setattr("trading.jobs.pre_open._step_macro", lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr("trading.jobs.pre_open._step_sector", lambda c, d, w: False)
    monkeypatch.setattr("trading.jobs.pre_open._step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr("trading.jobs.pre_open._step_portfolio", lambda p, s, w, *, as_of, **_: [])
    monkeypatch.setattr(
        "trading.jobs.pre_open.cross_check_closes",
        lambda paths, as_of, holdings: ["RVNL: parquet vs Kite mismatch"],
    )

    result = run_pre_open(date(2026, 5, 15), paths=paths, skip_news=True)
    assert any("RVNL" in w for w in result.warnings)


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


def test_step_portfolio_critical_sentiment_forces_exit(paths) -> None:
    """F-022: with sentiment now wired, a holding flagged critical → EXIT verdict.

    Locks the revived critical-news EXIT veto at the job boundary (previously
    dead because `_step_portfolio` passed an empty SentimentSnapshot)."""
    from tests.conftest import seed_kite_snapshot
    from trading.store.db import get_conn
    from trading.store.news_store import SentimentDailyRow, upsert_sentiment_daily

    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[_PRE_OPEN_HOLDING])
    write_ohlcv(_all_pass_frame(), "RVNL", paths)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sentiment_daily(
            conn,
            SentimentDailyRow(
                date="2026-05-13",
                symbol="RVNL",
                score_7d=-0.5,
                score_30d=-0.4,
                news_count=4,
                negative_news_count=3,
                has_critical=True,
            ),
        )
        conn.commit()

    warnings: list[str] = []
    out = _step_portfolio(paths, _settings(), warnings, as_of=date(2026, 5, 15))
    assert len(out) == 1
    assert out[0].symbol == "RVNL"
    assert out[0].verdict == "EXIT"
    assert any("critical" in r.lower() for r in out[0].reasons)


def test_step_portfolio_raises_pre_open_aborted_when_snapshot_missing(
    paths,
) -> None:
    from trading.jobs.pre_open import PreOpenAborted

    warnings: list[str] = []
    with pytest.raises(PreOpenAborted) as exc:
        _step_portfolio(paths, _settings(), warnings, as_of=date(2026, 5, 15))
    assert "/kite-snapshot" in str(exc.value)


def test_step_portfolio_degrades_when_snapshot_missing_and_not_required(paths) -> None:
    """F-032: with require_snapshot=False, a missing snapshot warns + returns []
    instead of raising PreOpenAborted (the unattended broker-free run)."""
    warnings: list[str] = []
    out = _step_portfolio(
        paths, _settings(), warnings, as_of=date(2026, 5, 15), require_snapshot=False
    )
    assert out == []
    assert any("snapshot" in w.lower() for w in warnings)


def test_step_portfolio_still_scores_when_snapshot_present_and_not_required(paths) -> None:
    """require_snapshot=False must still *use* a snapshot when one exists."""
    from tests.conftest import seed_kite_snapshot

    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[_PRE_OPEN_HOLDING])
    write_ohlcv(_all_pass_frame(), "RVNL", paths)
    warnings: list[str] = []
    out = _step_portfolio(
        paths, _settings(), warnings, as_of=date(2026, 5, 15), require_snapshot=False
    )
    assert len(out) == 1
    assert out[0].symbol == "RVNL"


def test_run_pre_open_unattended_writes_bundle_without_snapshot(paths, monkeypatch) -> None:
    """F-032: run_pre_open(require_snapshot=False) completes — bundle written,
    holdings-health skipped with a warning — even with no Kite snapshot."""
    monkeypatch.setattr("trading.jobs.pre_open._step_macro", lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr("trading.jobs.pre_open._step_sector", lambda c, d, w: False)
    monkeypatch.setattr("trading.jobs.pre_open._step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr("trading.jobs.pre_open._step_ohlcv", lambda p, d, w: 0)
    monkeypatch.setattr("trading.jobs.pre_open._step_scan", lambda c, p, d, w: [])

    result = run_pre_open(date(2026, 5, 15), paths=paths, skip_news=True, require_snapshot=False)
    assert result.bundle_path.is_file()
    assert result.holdings_scored == 0
    assert any("snapshot" in w.lower() for w in result.warnings)


def test_plan_and_record_writes_pending_for_selected(
    conn: sqlite3.Connection, paths
) -> None:
    """A selected candidate is recorded to _pending_entries.json and NO paper
    trade or signal is opened at pre-open (the fill happens in open-fills)."""
    warnings: list[str] = []
    cand = _candidate("RVNL", 10)  # close=100, atr=2
    count = _step_plan_and_record(
        conn, date(2026, 5, 15), [_sc(cand, ml_score=0.55)], "NEUTRAL", warnings, paths
    )
    assert count == 1

    pt_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    sig_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert pt_count == 0  # nothing opened at pre-open
    assert sig_count == 0  # selected candidates get no signal until open-fills

    regime, entries = read_pending_entries(paths, date(2026, 5, 15))
    assert regime == "NEUTRAL"
    assert [e.symbol for e in entries] == ["RVNL"]
    assert entries[0].atr_14 == pytest.approx(2.0)
    assert entries[0].ml_score == pytest.approx(0.55)
    assert entries[0].ref_close == pytest.approx(100.0)


def test_plan_and_record_non_selected_logs_signal_only(
    conn: sqlite3.Connection, paths
) -> None:
    """selected=False candidates write a visibility signal (with ml_score) but
    are NOT recorded as pending and open no trade."""
    warnings: list[str] = []
    cand = _candidate("RVNL", 10)
    count = _step_plan_and_record(
        conn,
        date(2026, 5, 15),
        [_sc(cand, ml_score=0.42, selected=False)],
        "NEUTRAL",
        warnings,
        paths,
    )
    assert count == 0
    sig_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    pt_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    score = conn.execute("SELECT ml_score FROM signals").fetchone()["ml_score"]
    assert sig_count == 1
    assert pt_count == 0
    assert score == pytest.approx(0.42)

    _, entries = read_pending_entries(paths, date(2026, 5, 15))
    assert entries == []


def test_plan_and_record_skips_already_open(conn: sqlite3.Connection, paths) -> None:
    """A symbol already holding an OPEN trade entered today is not re-recorded."""
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days) "
        "VALUES ('2026-05-15T08:30:00','RVNL','LONG',100.0,95.0,120.0,25)"
    )
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty) VALUES (?,?,?,?)",
        (cur.lastrowid, "2026-05-15T09:20:00", 100.0, 5),
    )
    conn.commit()
    warnings: list[str] = []
    count = _step_plan_and_record(
        conn, date(2026, 5, 15), [_sc(_candidate("RVNL", 10))], "NEUTRAL", warnings, paths
    )
    assert count == 0
    _, entries = read_pending_entries(paths, date(2026, 5, 15))
    assert entries == []


def test_plan_and_record_atr_wider_than_close_skipped(
    conn: sqlite3.Connection, paths
) -> None:
    """A non-positive ATR (stop ≥ close) is warned and not recorded."""
    warnings: list[str] = []
    cand = replace(_candidate("RVNL", 10), atr_14=0.0)  # stop == close → skip
    count = _step_plan_and_record(
        conn, date(2026, 5, 15), [_sc(cand)], "NEUTRAL", warnings, paths
    )
    assert count == 0
    assert any("RVNL" in w for w in warnings)
    _, entries = read_pending_entries(paths, date(2026, 5, 15))
    assert entries == []


def test_run_pre_open_writes_pending_and_opens_nothing(paths, monkeypatch) -> None:
    """End-to-end (upstream stubbed): a selected candidate is recorded to the
    handoff file and run_pre_open reports paper_trades_opened == 0."""
    monkeypatch.setattr("trading.jobs.pre_open._step_macro", lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr("trading.jobs.pre_open._step_sector", lambda c, d, w: False)
    monkeypatch.setattr("trading.jobs.pre_open._step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr("trading.jobs.pre_open._step_ohlcv", lambda p, d, w: 0)
    monkeypatch.setattr("trading.jobs.pre_open._step_fno_ban", lambda c, d, w: None)
    cand = _candidate("RVNL", 10)
    monkeypatch.setattr("trading.jobs.pre_open._step_scan", lambda c, p, d, w: [cand])
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_rank", lambda c, p, d, passing, w: [_sc(cand, ml_score=0.6)]
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_portfolio",
        lambda p, s, w, *, as_of, **_: [],
    )

    result = run_pre_open(date(2026, 5, 15), paths=paths, skip_news=True, require_snapshot=False)
    assert result.paper_trades_opened == 0
    assert result.pending_entries == 1
    with get_conn(paths.db_path) as conn2:
        opened = conn2.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert opened == 0
    _, entries = read_pending_entries(paths, date(2026, 5, 15))
    assert [e.symbol for e in entries] == ["RVNL"]


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
    assert already_opened_today(conn, "RVNL", date(2026, 5, 15)) is True
    assert already_opened_today(conn, "NTPC", date(2026, 5, 15)) is False


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
        "trading.jobs.pre_open.load_candidate_universe",
        lambda p: ["TESTSYM"],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_ohlcv",
        lambda p, d, w: 0,
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_macro",
        lambda c, d, w: (True, "RISK_ON"),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_sector",
        lambda c, d, w: False,
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_news",
        lambda c, d, w: (0, 0),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_portfolio",
        lambda p, s, w, *, as_of, **_: [],
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


def _register_passive_top1_model(paths) -> None:
    """Register a tiny active model that prefers higher-RSI inputs."""
    from datetime import datetime

    import lightgbm as lgb
    import numpy as np

    from trading.ranking.ranker_features import FEATURE_NAMES
    from trading.store.model_registry import RegistryRow, register, save_model

    rng = np.random.default_rng(0)
    X = rng.normal(loc=0, scale=1, size=(80, len(FEATURE_NAMES)))
    rsi_idx = FEATURE_NAMES.index("rsi_14")
    X[:, rsi_idx] = rng.uniform(20, 70, size=80)
    y = (X[:, rsi_idx] > 40).astype(int)
    m = lgb.LGBMClassifier(n_estimators=20, num_leaves=8, verbose=-1)
    m.fit(X, y)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    pkl = paths.models_dir / "ranker_test.pkl"
    save_model(pkl, m, FEATURE_NAMES)
    register(
        paths,
        row=RegistryRow(
            version="test",
            trained_at=datetime.now(UTC).isoformat(),
            train_start="2022-01-01",
            train_end="2024-12-31",
            oos_sharpe=1.0,
            oos_hit_rate=0.5,
            n_train_examples=80,
            n_features=len(FEATURE_NAMES),
            path=str(pkl.relative_to(paths.project_root)),
            active=True,
            notes="test",
        ),
        promote=True,
    )


def _multi_pass_universe(paths, syms: list[str]) -> None:
    """Write the engineered all-pass parquet for each symbol."""
    for s in syms:
        write_ohlcv(_all_pass_frame(), s, paths)


def test_pre_open_persists_ml_score_on_all_passing(paths, monkeypatch) -> None:
    """Ranker active → every passing candidate gets ml_score, top-K open.

    Stubs `_step_scan` to return 7 all-pass candidates and seeds the
    corresponding parquet so score_and_filter can read OHLCV history.
    """
    import sqlite3 as _sql

    syms = [f"S{i}" for i in range(7)]
    _multi_pass_universe(paths, syms)
    _register_passive_top1_model(paths)

    fake_cands = [_candidate(s, n_passed=10) for s in syms]
    monkeypatch.setattr("trading.jobs.pre_open._step_macro", lambda c, d, w: (True, "RISK_ON"))
    monkeypatch.setattr("trading.jobs.pre_open._step_sector", lambda c, d, w: False)
    monkeypatch.setattr("trading.jobs.pre_open._step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr("trading.jobs.pre_open._step_ohlcv", lambda p, d, w: 0)
    monkeypatch.setattr("trading.jobs.pre_open._step_scan", lambda c, p, d, w: fake_cands)
    monkeypatch.setattr("trading.jobs.pre_open._step_portfolio", lambda p, s, w, *, as_of, **_: [])

    result = run_pre_open(date(2026, 5, 15), paths=paths, skip_news=False)
    assert result.candidates_passing == 7
    assert result.candidates_selected == 5
    # Pre-open no longer opens trades; the 5 selected are recorded as pending
    # entries (filled at the live LTP by open-fills), the 2 non-selected get
    # visibility-only signals.
    assert result.paper_trades_opened == 0
    assert result.pending_entries == 5

    db = _sql.connect(paths.db_path)
    db.row_factory = _sql.Row
    sig_rows = db.execute(
        "SELECT symbol, ml_score FROM signals WHERE substr(ts, 1, 10) = ?",
        ("2026-05-15",),
    ).fetchall()
    db.close()
    # Only the non-selected (passing − selected) candidates are logged as signals.
    assert len(sig_rows) == result.candidates_passing - result.candidates_selected
    assert all(r["ml_score"] is not None for r in sig_rows)

    # ml_score is carried on the selected candidates too — via the handoff file.
    _, pending = read_pending_entries(paths, date(2026, 5, 15))
    assert len(pending) == 5
    assert all(e.ml_score is not None for e in pending)


def test_pre_open_without_active_model_opens_all_passing(paths, monkeypatch) -> None:
    """No registry → cold-start path; ml_score=None; all passing candidates open."""
    import sqlite3 as _sql

    syms = ["A", "B", "C"]
    _multi_pass_universe(paths, syms)

    fake_cands = [_candidate(s, n_passed=10) for s in syms]
    monkeypatch.setattr("trading.jobs.pre_open._step_macro", lambda c, d, w: (True, "RISK_ON"))
    monkeypatch.setattr("trading.jobs.pre_open._step_sector", lambda c, d, w: False)
    monkeypatch.setattr("trading.jobs.pre_open._step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr("trading.jobs.pre_open._step_ohlcv", lambda p, d, w: 0)
    monkeypatch.setattr("trading.jobs.pre_open._step_scan", lambda c, p, d, w: fake_cands)
    monkeypatch.setattr("trading.jobs.pre_open._step_portfolio", lambda p, s, w, *, as_of, **_: [])

    result = run_pre_open(date(2026, 5, 15), paths=paths, skip_news=False)
    assert result.candidates_selected == result.candidates_passing

    db = _sql.connect(paths.db_path)
    db.row_factory = _sql.Row
    rows = db.execute(
        "SELECT ml_score FROM signals WHERE substr(ts, 1, 10) = ?",
        ("2026-05-15",),
    ).fetchall()
    db.close()
    for r in rows:
        assert r["ml_score"] is None


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


def test_step_sector_writes_rows_and_returns_true(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        SectorRow(
            date="2026-05-26",
            sector="IT",
            close=36000.0,
            rs_5d=0.012,
            rs_20d=0.035,
            rs_60d=0.02,
            regime="LEADING",
        ),
        SectorRow(
            date="2026-05-26",
            sector="METAL",
            close=9000.0,
            rs_5d=-0.01,
            rs_20d=-0.03,
            rs_60d=-0.04,
            regime="LAGGING",
        ),
    ]
    monkeypatch.setattr("trading.jobs.pre_open.fetch_all_sectors", lambda _as_of: rows)
    warnings: list[str] = []
    ok = _step_sector(conn, date(2026, 5, 26), warnings)
    assert ok is True
    assert warnings == []
    fetched = conn.execute(
        "SELECT sector, regime FROM sector_daily WHERE date = ? ORDER BY sector",
        ("2026-05-26",),
    ).fetchall()
    assert [r["sector"] for r in fetched] == ["IT", "METAL"]
    assert [r["regime"] for r in fetched] == ["LEADING", "LAGGING"]


def test_step_sector_degrades_on_fetch_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_as_of: date) -> list:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr("trading.jobs.pre_open.fetch_all_sectors", boom)
    warnings: list[str] = []
    ok = _step_sector(conn, date(2026, 5, 26), warnings)
    assert ok is False
    assert any("sector snapshot failed" in w for w in warnings)
    n = conn.execute("SELECT COUNT(*) AS n FROM sector_daily").fetchone()["n"]
    assert n == 0


def test_step_sector_returns_false_when_no_rows(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("trading.jobs.pre_open.fetch_all_sectors", lambda _as_of: [])
    warnings: list[str] = []
    ok = _step_sector(conn, date(2026, 5, 26), warnings)
    assert ok is False
    assert any("no sector rows fetched" in w for w in warnings)


def test_build_scan_context_populates_fno_ban(tmp_path) -> None:
    from datetime import date

    from trading.jobs.pre_open import build_scan_context
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import replace_fno_ban_list
    from trading.store.migrations import run_migrations

    db = tmp_path / "c.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "GNFC"])
        ctx = build_scan_context(conn, date(2026, 6, 19))
    assert ctx.fno_ban_symbols == frozenset({"IDEA", "GNFC"})


def test_banned_symbol_fails_gate_via_context(tmp_path) -> None:
    from datetime import date

    from trading.jobs.pre_open import build_scan_context
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import replace_fno_ban_list
    from trading.store.migrations import run_migrations
    from trading.strategy.rules import passes_not_fno_banned

    db = tmp_path / "d.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA"])
        ctx = build_scan_context(conn, date(2026, 6, 19))
    assert passes_not_fno_banned("IDEA", ctx).passed is False
    assert passes_not_fno_banned("RELIANCE", ctx).passed is True


def test_step_fno_ban_persists_and_counts(tmp_path, monkeypatch) -> None:
    from datetime import date

    import trading.jobs.pre_open as po
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import get_fno_ban_symbols
    from trading.store.migrations import run_migrations

    monkeypatch.setattr(po, "fetch_fno_ban_symbols", lambda: ["IDEA", "GNFC"])
    db = tmp_path / "e.db"
    warnings: list[str] = []
    with get_conn(db) as conn:
        run_migrations(conn)
        n = po._step_fno_ban(conn, date(2026, 6, 19), warnings)
        assert n == 2
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["GNFC", "IDEA"]
    assert warnings == []


def test_step_fno_ban_degrades_on_empty(tmp_path, monkeypatch) -> None:
    from datetime import date

    import trading.jobs.pre_open as po
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import get_fno_ban_symbols
    from trading.store.migrations import run_migrations

    monkeypatch.setattr(po, "fetch_fno_ban_symbols", lambda: [])
    db = tmp_path / "f.db"
    warnings: list[str] = []
    with get_conn(db) as conn:
        run_migrations(conn)
        n = po._step_fno_ban(conn, date(2026, 6, 19), warnings)
        assert n == 0
        assert get_fno_ban_symbols(conn, "2026-06-19") == []
    assert any("ban list" in w.lower() for w in warnings)
