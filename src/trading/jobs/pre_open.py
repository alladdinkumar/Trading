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
from trading.data.ohlcv_refresh import cross_check_closes, refresh_ohlcv
from trading.data.sector import fetch_all_sectors
from trading.data.universe import load_candidate_universe
from trading.features.regime import Regime
from trading.features.sentiment import aggregate_daily, score_news_items
from trading.llm.context import ContextInputs, assemble_context
from trading.ops.logging_setup import configure_logging
from trading.paper.ledger import log_signal_and_open_trade
from trading.portfolio.fundamentals import load_fundamentals_map
from trading.portfolio.health import (
    HealthScore,
    score_holding,
)
from trading.portfolio.holding_context import build_holding_context
from trading.store.db import get_conn
from trading.store.macro_store import get_macro_snapshot, upsert_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.news_store import insert_news_items, list_critical_symbols
from trading.store.ohlcv import read_ohlcv
from trading.store.repo import Signal, insert_signal
from trading.store.sector_store import upsert_sector_daily
from trading.strategy.exits import target_price
from trading.strategy.ranker import ScoredCandidate, score_and_filter
from trading.strategy.rules import Candidate, ScanContext, passing, scan
from trading.strategy.sizing import SizingInput, position_size

RANKER_TOP_K = 5


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
    sector_written: bool
    news_inserted: int
    sentiment_rows: int
    candidates_total: int
    candidates_passing: int
    candidates_selected: int
    paper_trades_opened: int
    holdings_scored: int
    ohlcv_bars_added: int = 0
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
        sector_written = _step_sector(conn, as_of, warnings)

        if skip_news:
            news_inserted, sentiment_rows = 0, 0
            warnings.append("skip_news=True — news ingest skipped")
        else:
            news_inserted, sentiment_rows = _step_news(conn, as_of, warnings)

        ohlcv_bars_added = _step_ohlcv(p, as_of, warnings)

        candidates = _step_scan(conn, p, as_of, warnings)
        passing_candidates = passing(candidates)
        scored = _step_rank(conn, p, as_of, passing_candidates, warnings)

        holdings = _step_portfolio(p, s, warnings, as_of=as_of)
        _step_cross_check(p, as_of, warnings)

        opened = _step_auto_open(
            conn,
            as_of,
            scored,
            regime,
            capital_per_trade,
            risk_pct,
            warnings,
        )

        bundle_path = _step_assemble(
            conn,
            p,
            as_of,
            candidates,
            holdings,
            scored,
        )

    return PreOpenResult(
        as_of=as_of,
        bundle_path=bundle_path,
        macro_written=macro_written,
        sector_written=sector_written,
        news_inserted=news_inserted,
        sentiment_rows=sentiment_rows,
        candidates_total=len(candidates),
        candidates_passing=len(passing_candidates),
        candidates_selected=sum(1 for sc in scored if sc.selected),
        paper_trades_opened=opened,
        holdings_scored=len(holdings),
        ohlcv_bars_added=ohlcv_bars_added,
        warnings=warnings,
    )


def _step_rank(
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    passing_candidates: list[Candidate],
    warnings: list[str],
    k: int = RANKER_TOP_K,
) -> list[ScoredCandidate]:
    """Score rules-passing candidates and mark top-K as selected.

    Cold-start path (no active model, missing pkl, feature-name mismatch,
    or any IO error inside score_and_filter) returns each candidate marked
    selected=True with ml_score=None — preserving pre-Phase-16 behaviour.
    """
    if not passing_candidates:
        return []
    try:
        return score_and_filter(passing_candidates, paths, conn, as_of, k=k)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"ranker scoring failed — cold start ({e!s})")
        return [ScoredCandidate(c, None, True) for c in passing_candidates]


def _step_macro(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> tuple[bool, Regime]:
    """Pull macro inputs, classify regime, upsert snapshot. Degrade on error."""
    try:
        snap, rr = snapshot_and_classify(as_of)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"macro snapshot failed: {e!s}")
        return False, "NEUTRAL"
    upsert_macro_snapshot(conn, snap)
    return True, rr.regime


def _step_sector(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> bool:
    """Pull NSE sectoral indices, compute RS vs Nifty 50, upsert sector_daily.

    Graceful: any error (yfinance down, benchmark missing) yields a warning
    and returns False so the wider pre-open continues.
    """
    try:
        rows = fetch_all_sectors(as_of)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"sector snapshot failed: {e!s}")
        return False
    if not rows:
        warnings.append("no sector rows fetched (benchmark or all sectors failed)")
        return False
    upsert_sector_daily(conn, rows)
    return True


def _step_news(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> tuple[int, int]:
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


def _step_ohlcv(paths: Paths, as_of: date, warnings: list[str]) -> int:
    """Refresh parquet OHLCV up to D-1 before the scan reads it.

    Runs over the full ingest universe (candidates + holdings) so both the
    scan and portfolio-health steps see current bars. Refresh failures degrade
    to warnings — the scan's own staleness guard skips any symbol still stale.
    Returns the total number of bars appended.
    """
    try:
        result = refresh_ohlcv(paths, as_of)
    except Exception as e:  # pragma: no cover — defensive; refresh isolates per-symbol
        warnings.append(f"ohlcv refresh failed: {e!s}")
        return 0
    warnings.extend(result.warnings)
    return result.bars_added


def build_scan_context(conn: sqlite3.Connection, as_of: date) -> ScanContext:
    """Assemble the Layer-A `ScanContext` from data already persisted this run.

    Re-enables the risk gates that were dead while the context was empty
    (F-019):
    - `india_vix` ← today's `macro_snapshot.vix` (drives the regime/VIX gate);
    - `critical_event_symbols` ← `sentiment_daily.has_critical` for `as_of`
      (drives the hard critical-news veto).

    `nifty200_drawdown_5d_pct` is left `None` (not yet computed/stored — the
    regime rule degrades gracefully). `fno_ban_symbols` and `t2t_symbols` need
    external NSE feeds (F-010) and stay empty until those land.
    """
    snap = get_macro_snapshot(conn, as_of.isoformat())
    india_vix = snap.vix if snap is not None else None
    critical = list_critical_symbols(conn, as_of.isoformat())
    return ScanContext(
        scan_date=as_of,
        india_vix=india_vix,
        critical_event_symbols=frozenset(critical),
    )


def _step_scan(
    conn: sqlite3.Connection, paths: Paths, as_of: date, warnings: list[str]
) -> list[Candidate]:
    """Run Layer A scanner over the Nifty-50 candidate universe.

    Candidates are restricted to the Nifty 50 (`data/static/nifty50.txt`) so
    the user's non-Nifty holdings are scored for health but never auto-traded.
    The scan context is built from this run's macro + sentiment data so the
    regime/VIX and critical-news gates are live (F-019). Symbols without parquet
    (or <200 bars) are skipped inside `scan`; symbols whose latest bar is stale
    are skipped with a warning.
    """
    ctx = build_scan_context(conn, as_of)
    symbols = load_candidate_universe(paths)
    return scan(paths, as_of, symbols=symbols, ctx=ctx, warnings=warnings)


def _step_cross_check(paths: Paths, as_of: date, warnings: list[str]) -> None:
    """Flag holdings whose parquet close disagrees with the broker close.

    Best-effort: reads the same `holdings.json` `_step_portfolio` validated.
    A missing/stale snapshot is silently skipped (the portfolio step already
    raised or warned). Each divergent holding appends a warning.
    """
    try:
        holdings = read_holdings(paths, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError):
        return
    warnings.extend(cross_check_closes(paths, as_of, holdings))


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

    fundamentals_map = load_fundamentals_map(paths)
    results: list[HealthScore] = []
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        for h in holdings:
            try:
                history = read_ohlcv(h.tradingsymbol, paths)
            except FileNotFoundError:
                warnings.append(f"no parquet for holding {h.tradingsymbol} — skipped")
                continue
            ctx = build_holding_context(
                conn,
                symbol=h.tradingsymbol,
                qty=h.quantity,
                avg_price=h.average_price,
                last_price=h.last_price,
                history=history,
                as_of=as_of,
                fundamentals_map=fundamentals_map,
            )
            results.append(score_holding(ctx))
    return results


def _step_auto_open(
    conn: sqlite3.Connection,
    as_of: date,
    scored: list[ScoredCandidate],
    regime: Regime,
    capital: float,
    risk_pct: float,
    warnings: list[str],
) -> int:
    """Persist a signal for each scored candidate; open paper-trade for the
    top-K (selected=True).

    Entry price = `cand.close` (D-1's close — the most recent bar in the
    parquet at pre_open time, per spec §4.4 'limit order at close').
    Skips if (a) idempotency guard finds an open trade for symbol+date,
    or (b) sizing returns qty=0 (caps bound to zero).

    Signals for non-selected candidates are still logged with ml_score so
    they appear in the dashboard / brief — they just don't open trades.
    """
    opened = 0
    for sc in scored:
        cand = sc.candidate
        if sc.selected and _already_opened_today(conn, cand.symbol, as_of):
            continue
        stop_price = cand.close - 1.5 * cand.atr_14
        # Target = the exact price the exit engine aims for, min(+20%, 2.5R),
        # so signal.target no longer disagrees with the exit logic (F-029).
        signal_target = target_price(cand.close, stop_price)
        if cand.close <= stop_price:
            warnings.append(f"{cand.symbol}: ATR={cand.atr_14:.2f} ≥ close — skip")
            continue
        sizing = position_size(
            SizingInput(
                capital=capital,
                risk_pct=risk_pct,
                entry=cand.close,
                stop=stop_price,
                regime=regime,
            )
        )
        if sizing.qty == 0:
            warnings.append(f"{cand.symbol}: sizing bound to zero ({', '.join(sizing.reasons)})")
            continue
        signal = Signal(
            id=None,
            ts=f"{as_of.isoformat()}T08:30:00",
            symbol=cand.symbol,
            side="LONG",
            entry=cand.close,
            stop=stop_price,
            target=signal_target,
            horizon_days=25,
            rules_passed_json=json.dumps([r.name for r in cand.rules if r.passed]),
            ml_score=sc.ml_score,
            created_by="pre_open",
        )
        if not sc.selected:
            # Visibility-only: log the signal (with ml_score) but don't
            # open a paper-trade.
            insert_signal(conn, signal)
            continue
        log_signal_and_open_trade(
            conn,
            signal=signal,
            entry_ts=signal.ts,
            entry_price=cand.close,
            qty=sizing.qty,
            atr_at_entry=cand.atr_14,
            # Let predicted_return_pct default to the signal's implied target %
            # ((target - entry)/entry); since signal.target is now min(+20%,2.5R)
            # the prediction varies per signal instead of a constant +20% (F-029).
        )
        opened += 1
    return opened


def _already_opened_today(conn: sqlite3.Connection, symbol: str, as_of: date) -> bool:
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
    scored: list[ScoredCandidate] | None = None,
) -> Path:
    """Render the input bundle. Real wiring; no upstream calls."""
    return assemble_context(
        conn=conn,
        paths=paths,
        as_of=as_of,
        mode="pre_open",
        inputs=ContextInputs(
            candidates=candidates,
            holdings_health=holdings,
            scored_candidates=scored,
        ),
    )


def _main(
    date_str: str,
    skip_news: bool = False,
) -> None:
    """`python -m trading.jobs.pre_open <YYYY-MM-DD>` entry."""
    configure_logging("pre_open")
    from loguru import logger

    try:
        result = run_pre_open(
            date.fromisoformat(date_str),
            skip_news=skip_news,
        )
    except PreOpenAborted as e:
        print(f"Pre-open aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("pre_open failed")
        raise
    print(f"wrote {result.bundle_path}")
    if result.warnings:
        print(f"warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  - {w}")


if __name__ == "__main__":  # pragma: no cover
    import typer

    typer.run(_main)
