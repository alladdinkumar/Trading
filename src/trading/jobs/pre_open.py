"""Phase 13 — pre_open MVP orchestrator.

Runs each upstream phase in dependency order, auto-opens paper-trades
for all-pass signals, writes the Phase 12 context bundle, halts. The
per-step helpers stay private so the orchestrator's body reads as a
narrative. Each `_step_*` either returns a typed result or appends to
`warnings` on graceful-degradation paths.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from trading.config import Paths, Settings, get_paths, get_settings
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_holdings,
)
from trading.data.macro import snapshot_and_classify
from trading.data.news import DEFAULT_ALIASES, fetch_all_news
from trading.features.regime import Regime
from trading.features.sentiment import aggregate_daily, score_news_items
from trading.llm.context import ContextInputs, assemble_context
from trading.paper.ledger import log_signal_and_open_trade
from trading.portfolio.health import (
    FundamentalsSnapshot,
    HealthScore,
    HoldingContext,
    SentimentSnapshot,
    score_holding,
    technicals_from_history,
)
from trading.store.db import get_conn
from trading.store.macro_store import upsert_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.news_store import insert_news_items
from trading.store.ohlcv import read_ohlcv
from trading.store.repo import Signal
from trading.strategy.rules import Candidate, ScanContext, passing, scan
from trading.strategy.sizing import SizingInput, position_size


class PreOpenAborted(RuntimeError):  # noqa: N818 — "Aborted" is a state, not an error suffix
    """Raised when run_pre_open cannot proceed because a prerequisite is missing.

    Currently raised when the Kite snapshot for `as_of` is missing or stale —
    `/kite-snapshot` skill must run first. CLI catches this and exits 2 with
    the remediation message.
    """


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

        holdings = _step_portfolio(p, s, warnings, as_of=as_of)

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
    """Run Layer A scanner over the parquet universe."""
    ctx = ScanContext(scan_date=as_of)
    return scan(paths, as_of, ctx=ctx)


def _step_portfolio(
    paths: Paths,
    settings: Settings,
    warnings: list[str],
    *,
    as_of: date,
) -> list[HealthScore]:
    """Score each holding from today's Kite snapshot.

    Reads `data/raw/<as_of>/holdings.json` (written by the /kite-snapshot
    skill). Missing or stale snapshot raises `PreOpenAborted`. `settings`
    is unused today; kept on the signature for symmetry with other steps
    and forward-compat with future per-account config.
    """
    try:
        holdings = read_holdings(paths, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError) as e:
        raise PreOpenAborted(str(e)) from e

    results: list[HealthScore] = []
    for h in holdings:
        try:
            history = read_ohlcv(h.tradingsymbol, paths)
        except FileNotFoundError:
            warnings.append(f"no parquet for holding {h.tradingsymbol} — skipped")
            continue
        ctx = HoldingContext(
            symbol=h.tradingsymbol,
            qty=h.quantity,
            avg_price=h.average_price,
            last_price=h.last_price,
            technicals=technicals_from_history(history),
            fundamentals=FundamentalsSnapshot(),
            sentiment=SentimentSnapshot(),
        )
        results.append(score_holding(ctx))
    return results


def _step_auto_open(
    conn: sqlite3.Connection,
    as_of: date,
    passing: list[Candidate],
    regime: Regime,
    capital: float,
    risk_pct: float,
    warnings: list[str],
) -> int:
    """For each all-pass candidate: size + open paper-trade.

    Entry price = `cand.close` (D-1's close — the most recent bar in the
    parquet at pre_open time, per spec §4.4 'limit order at close').
    Skips if (a) idempotency guard finds an open trade for symbol+date,
    or (b) sizing returns qty=0 (caps bound to zero).
    """
    opened = 0
    for cand in passing:
        if _already_opened_today(conn, cand.symbol, as_of):
            continue
        stop_price = cand.close - 1.5 * cand.atr_14
        target_price = cand.close * 1.20
        if cand.close <= stop_price:
            warnings.append(
                f"{cand.symbol}: ATR={cand.atr_14:.2f} ≥ close — skip"
            )
            continue
        sizing = position_size(SizingInput(
            capital=capital, risk_pct=risk_pct,
            entry=cand.close, stop=stop_price, regime=regime,
        ))
        if sizing.qty == 0:
            warnings.append(
                f"{cand.symbol}: sizing bound to zero "
                f"({', '.join(sizing.reasons)})"
            )
            continue
        signal = Signal(
            id=None,
            ts=f"{as_of.isoformat()}T08:30:00",
            symbol=cand.symbol,
            side="LONG",
            entry=cand.close,
            stop=stop_price,
            target=target_price,
            horizon_days=25,
            rules_passed_json=json.dumps(
                [r.name for r in cand.rules if r.passed]
            ),
            created_by="pre_open",
        )
        log_signal_and_open_trade(
            conn, signal=signal,
            entry_ts=signal.ts, entry_price=cand.close, qty=sizing.qty,
            atr_at_entry=cand.atr_14, predicted_return_pct=20.0,
        )
        opened += 1
    return opened


def _already_opened_today(
    conn: sqlite3.Connection, symbol: str, as_of: date
) -> bool:
    """True if `symbol` has an OPEN paper-trade entered on `as_of`."""
    row = conn.execute(
        "SELECT 1 FROM paper_trades pt "
        "JOIN signals s ON s.id = pt.signal_id "
        "WHERE s.symbol = ? AND substr(pt.ts_entry, 1, 10) = ? "
        "  AND pt.ts_exit IS NULL "
        "LIMIT 1",
        (symbol, as_of.isoformat()),
    ).fetchone()
    return row is not None


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


def _main(  # pragma: no cover — manual entry
    date_str: str,
    skip_news: bool = False,
) -> None:
    """`python -m trading.jobs.pre_open <YYYY-MM-DD>` entry."""
    try:
        result = run_pre_open(
            date.fromisoformat(date_str),
            skip_news=skip_news,
        )
    except PreOpenAborted as e:
        print(f"Pre-open aborted: {e}")
        raise SystemExit(2) from e
    print(f"wrote {result.bundle_path}")
    if result.warnings:
        print(f"warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  - {w}")


if __name__ == "__main__":  # pragma: no cover
    import typer
    typer.run(_main)
