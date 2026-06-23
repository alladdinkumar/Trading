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
from trading.data.fno_ban import fetch_fno_ban_symbols
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_holdings,
)
from trading.data.news import default_aliases, fetch_all_news
from trading.data.ohlcv_refresh import cross_check_closes, refresh_ohlcv
from trading.data.sector import fetch_all_sectors, load_sector_map
from trading.data.universe import load_candidate_universe
from trading.features.regime import Regime, snapshot_and_classify
from trading.features.sentiment import aggregate_daily, score_news_items
from trading.llm.context import ContextInputs, assemble_context
from trading.ops.logging_setup import configure_logging
from trading.paper.ledger import log_signal_and_open_trade
from trading.paper.positions import already_opened_today, deployed_by_symbol
from trading.paper.reconcile import compute_paper_cash
from trading.portfolio.fundamentals import load_fundamentals_map
from trading.portfolio.health import (
    HealthScore,
    score_holding,
)
from trading.portfolio.holding_context import build_holding_context
from trading.ranking.ranker import (
    ScoredCandidate,
    conviction_from_score,
    score_and_filter,
)
from trading.store.db import get_conn
from trading.store.fno_ban_store import get_fno_ban_symbols, replace_fno_ban_list
from trading.store.macro_store import get_macro_snapshot, upsert_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.news_store import (
    insert_news_items,
    list_critical_symbols,
    negative_news_count_7d,
)
from trading.store.ohlcv import read_ohlcv
from trading.store.repo import (
    EntryAttribution,
    Signal,
    insert_signal,
    matured_score_outcomes,
)
from trading.store.sector_store import upsert_sector_daily
from trading.strategy.calibration import build_score_calibration
from trading.strategy.daily_budget import BudgetCandidate, plan_daily_entries
from trading.strategy.exits import target_price
from trading.strategy.rules import Candidate, ScanContext, passing, scan

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
    pool_capital: float = 100_000.0,
    daily_deploy_cap: float = 7_000.0,
    risk_pct: float = 0.02,
    require_snapshot: bool = True,
) -> PreOpenResult:
    """Orchestrate Phases 1–12 for `as_of` and write the analyst bundle.

    Each step runs in dependency order. Failures in graceful-degradation
    steps (macro, news, portfolio) are collected as warnings; the bundle
    is written either way. Auto-opened paper-trades use D-1 close as
    entry price (the most recent bar in the parquet).

    `require_snapshot=False` (the unattended/gap-filler path, F-032) lets the
    broker-free spine run without a Kite snapshot: `_step_portfolio` degrades to
    a warning instead of raising `PreOpenAborted`, so macro/scan/auto-open/bundle
    still complete on operator-absent days. The holdings-health section is then
    simply empty.
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

        _step_fno_ban(conn, as_of, warnings)
        candidates = _step_scan(conn, p, as_of, warnings)
        passing_candidates = passing(candidates)
        scored = _step_rank(conn, p, as_of, passing_candidates, warnings)

        holdings = _step_portfolio(p, s, warnings, as_of=as_of, require_snapshot=require_snapshot)
        _step_cross_check(p, as_of, warnings)

        opened = _step_auto_open(
            conn,
            as_of,
            scored,
            regime,
            pool_capital,
            daily_deploy_cap,
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
    watched = sorted(default_aliases().keys())
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


def _step_fno_ban(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> int:
    """Fetch the NSE F&O ban list and persist it for `as_of`. Best-effort.

    `fetch_fno_ban_symbols` already swallows network errors (returns []). On an
    empty result — a real no-ban day or a feed outage — we still overwrite the
    date (clearing any stale rows) and warn that the gate degraded to a pass.
    Returns the number of banned symbols persisted.
    """
    symbols = fetch_fno_ban_symbols()
    replace_fno_ban_list(conn, as_of.isoformat(), symbols)
    if not symbols:
        warnings.append("F&O ban list empty/unavailable — ban gate degraded to pass")
    return len(symbols)


def build_scan_context(conn: sqlite3.Connection, as_of: date) -> ScanContext:
    """Assemble the Layer-A `ScanContext` from data already persisted this run.

    Re-enables the risk gates that were dead while the context was empty
    (F-019):
    - `india_vix` ← today's `macro_snapshot.vix` (drives the regime/VIX gate);
    - `critical_event_symbols` ← `sentiment_daily.has_critical` for `as_of`
      (drives the hard critical-news veto).

    `nifty200_drawdown_5d_pct` is left `None` (not yet computed/stored — the
    regime rule degrades gracefully). `fno_ban_symbols` ← today's `fno_ban_list`
    rows (F-010), reviving the `passes_not_fno_banned` veto. `t2t_symbols` still
    needs an NSE T2T feed (no table) and stays empty.
    """
    snap = get_macro_snapshot(conn, as_of.isoformat())
    india_vix = snap.vix if snap is not None else None
    critical = list_critical_symbols(conn, as_of.isoformat())
    ban = get_fno_ban_symbols(conn, as_of.isoformat())
    return ScanContext(
        scan_date=as_of,
        india_vix=india_vix,
        critical_event_symbols=frozenset(critical),
        fno_ban_symbols=frozenset(ban),
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
    require_snapshot: bool = True,
) -> list[HealthScore]:
    """Score each holding from today's Kite snapshot.

    Reads `data/raw/<as_of>/holdings.json` (written by the /kite-snapshot
    skill). A missing or stale snapshot raises `PreOpenAborted` — unless
    `require_snapshot=False` (the F-032 unattended path), in which case it
    degrades to a warning and returns `[]` (no holdings health). `settings`
    is unused today; kept on the signature for symmetry with other steps
    and forward-compat with future per-account config.
    """
    try:
        holdings = read_holdings(paths, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError) as e:
        if not require_snapshot:
            warnings.append(f"no Kite snapshot — holdings health skipped ({e!s})")
            return []
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
    pool_capital: float,
    daily_cap: float,
    risk_pct: float,
    warnings: list[str],
) -> int:
    """Persist a signal for every scored candidate; open paper-trades for the
    selected ones the daily-budget planner can fund.

    Entry price = `cand.close` (D-1's close — the most recent bar in the parquet
    at pre_open time, per spec §4.4 'limit order at close'). The planner caps
    total new buys at `daily_cap` notional and at available cash, ranking by
    expected value, so the book paces instead of deploying every top-K pick
    against a fixed ₹1L (the over-deployment bug). Non-funded selected
    candidates and non-selected ones are logged as visibility-only signals.
    """
    available_cash = compute_paper_cash(conn, as_of=as_of)
    deployed = deployed_by_symbol(conn)

    budget_cands: list[BudgetCandidate] = []
    signal_by_symbol: dict[str, Signal] = {}
    atr_by_symbol: dict[str, float] = {}
    for sc in scored:
        cand = sc.candidate
        stop_price = cand.close - 1.5 * cand.atr_14
        if cand.close <= stop_price:
            warnings.append(f"{cand.symbol}: ATR={cand.atr_14:.2f} ≥ close — skip")
            continue
        # Target = the exact price the exit engine aims for, min(+20%, 2.5R),
        # so signal.target no longer disagrees with the exit logic (F-029).
        signal_target = target_price(cand.close, stop_price)
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
            conviction=conviction_from_score(sc.ml_score),
            created_by="pre_open",
        )
        signal_by_symbol[cand.symbol] = signal
        atr_by_symbol[cand.symbol] = cand.atr_14
        if not sc.selected:
            # Visibility-only: log the signal (with ml_score) but don't trade.
            insert_signal(conn, signal)
            continue
        if already_opened_today(conn, cand.symbol, as_of):
            continue
        budget_cands.append(
            BudgetCandidate(
                symbol=cand.symbol,
                entry=cand.close,
                stop=stop_price,
                target=signal_target,
                ml_score=sc.ml_score,
            )
        )

    # F-041: correct p_win with realised win-rate per ml_score band (self-healing).
    p_win_calibration = build_score_calibration(matured_score_outcomes(conn))
    plan = plan_daily_entries(
        budget_cands,
        available_cash=available_cash,
        deployed_by_symbol=deployed,
        regime=regime,
        pool_capital=pool_capital,
        daily_cap=daily_cap,
        risk_pct=risk_pct,
        p_win_calibration=p_win_calibration,
    )

    opened = 0
    planned_symbols = {e.symbol for e in plan.entries}
    sector_map = load_sector_map()
    for entry in plan.entries:
        signal = signal_by_symbol[entry.symbol]
        log_signal_and_open_trade(
            conn,
            signal=signal,
            entry_ts=signal.ts,
            entry_price=entry.entry,
            qty=entry.qty,
            atr_at_entry=atr_by_symbol[entry.symbol],
            # predicted_return_pct defaults to the signal's implied target %
            # ((target - entry)/entry); signal.target is min(+20%, 2.5R) (F-029).
            # F-040: snapshot entry conditions so a matured outcome can be
            # attributed to *why* it opened (regime/sector/news cohorts).
            attribution=EntryAttribution(
                regime=regime,
                sector=sector_map.get(entry.symbol),
                neg_news_7d=negative_news_count_7d(conn, entry.symbol, as_of),
            ),
        )
        opened += 1

    # Visibility-only: any selected candidate the planner skipped logs a signal
    # plus its skip reason so the brief shows why it didn't open.
    for symbol, reason in plan.skipped:
        if symbol in planned_symbols:
            continue
        skip_signal = signal_by_symbol.get(symbol)
        if skip_signal is not None:
            insert_signal(conn, skip_signal)
        warnings.append(f"{symbol}: not opened — {reason}")

    return opened


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
