"""Phase 13 — pre_open MVP orchestrator.

Runs each upstream phase in dependency order, auto-opens paper-trades
for all-pass signals, writes the Phase 12 context bundle, halts. The
per-step helpers stay private so the orchestrator's body reads as a
narrative. Each `_step_*` either returns a typed result or appends to
`warnings` on graceful-degradation paths.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from trading.config import Paths, Settings, get_paths, get_settings
from trading.data.macro import snapshot_and_classify
from trading.data.news import DEFAULT_ALIASES, fetch_all_news
from trading.features.regime import Regime
from trading.features.sentiment import aggregate_daily, score_news_items
from trading.llm.context import ContextInputs, assemble_context
from trading.portfolio.health import HealthScore
from trading.store.db import get_conn
from trading.store.macro_store import upsert_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.news_store import insert_news_items
from trading.strategy.rules import Candidate, passing


@dataclass(frozen=True)
class PreOpenResult:
    """What pre_open produced. Returned by `run_pre_open` for tests + CLI."""

    as_of: date
    bundle_path: Path
    macro_written: bool
    news_inserted: int
    sentiment_rows: int
    candidates_total: int
    candidates_passing: int
    paper_trades_opened: int
    holdings_scored: int
    warnings: list[str] = field(default_factory=list)


def run_pre_open(
    as_of: date,
    *,
    paths: Paths | None = None,
    settings: Settings | None = None,
    skip_news: bool = False,
    skip_kite: bool = False,
    capital_per_trade: float = 100_000.0,
    risk_pct: float = 0.02,
) -> PreOpenResult:
    """Orchestrate Phases 1–12 for `as_of` and write the analyst bundle.

    Each step runs in dependency order. Failures in graceful-degradation
    steps (macro, news, portfolio) are collected as warnings; the bundle
    is written either way. Auto-opened paper-trades use D-1 close as
    entry price (the most recent bar in the parquet).
    """
    p = paths if paths is not None else get_paths()
    s = settings if settings is not None else get_settings()
    warnings: list[str] = []

    with get_conn(p.db_path) as conn:
        run_migrations(conn)

        macro_written, regime = _step_macro(conn, as_of, warnings)

        if skip_news:
            news_inserted, sentiment_rows = 0, 0
            warnings.append("skip_news=True — news ingest skipped")
        else:
            news_inserted, sentiment_rows = _step_news(conn, as_of, warnings)

        candidates = _step_scan(p, as_of, warnings)
        passing_candidates = passing(candidates)

        holdings = _step_portfolio(p, s, warnings, skip_kite=skip_kite)

        opened = _step_auto_open(
            conn, as_of, passing_candidates, regime,
            capital_per_trade, risk_pct, warnings,
        )

        bundle_path = _step_assemble(
            conn, p, as_of, candidates, holdings,
        )

    return PreOpenResult(
        as_of=as_of,
        bundle_path=bundle_path,
        macro_written=macro_written,
        news_inserted=news_inserted,
        sentiment_rows=sentiment_rows,
        candidates_total=len(candidates),
        candidates_passing=len(passing_candidates),
        paper_trades_opened=opened,
        holdings_scored=len(holdings),
        warnings=warnings,
    )


def _step_macro(
    conn: sqlite3.Connection, as_of: date, warnings: list[str]
) -> tuple[bool, Regime]:
    """Pull macro inputs, classify regime, upsert snapshot. Degrade on error."""
    try:
        snap, rr = snapshot_and_classify(as_of)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"macro snapshot failed: {e!s}")
        return False, "NEUTRAL"
    upsert_macro_snapshot(conn, snap)
    return True, rr.regime


def _step_news(
    conn: sqlite3.Connection, as_of: date, warnings: list[str]
) -> tuple[int, int]:
    """Fetch RSS + NSE events, score with FinBERT, insert + aggregate.

    Returns (news_inserted, sentiment_rollups). Degrades gracefully:
    a top-level failure (e.g. all RSS sources down) returns (0, 0)
    with a warning. Per-source failures are isolated by Phase 8 already.
    """
    try:
        items = fetch_all_news()
        scored = score_news_items(items)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"news fetch/score failed: {e!s}")
        return 0, 0

    inserted = insert_news_items(conn, scored)
    watched = sorted(DEFAULT_ALIASES.keys())
    rollups = aggregate_daily(conn, watched, as_of)
    return inserted, len(rollups)


def _step_scan(
    paths: Paths, as_of: date, warnings: list[str]
) -> list[Candidate]:
    """Stub — Task 4 wires the scanner."""
    return []


def _step_portfolio(
    paths: Paths,
    settings: Settings,
    warnings: list[str],
    *,
    skip_kite: bool,
) -> list[HealthScore]:
    """Stub — Task 5 wires Kite holdings + score_holding."""
    return []


def _step_auto_open(
    conn: sqlite3.Connection,
    as_of: date,
    passing: list[Candidate],
    regime: Regime,
    capital: float,
    risk_pct: float,
    warnings: list[str],
) -> int:
    """Stub — Task 6 wires sizing + log_signal_and_open_trade."""
    return 0


def _step_assemble(
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    candidates: list[Candidate],
    holdings: list[HealthScore],
) -> Path:
    """Render the input bundle. Real wiring; no upstream calls."""
    return assemble_context(
        conn=conn, paths=paths, as_of=as_of, mode="pre_open",
        inputs=ContextInputs(candidates=candidates, holdings_health=holdings),
    )
