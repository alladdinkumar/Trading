"""Phase 18 — monthly_sip job: 1st-of-month ₹1L SIP allocation plan.

Spec: docs/superpowers/specs/2026-06-11-phase-18-support-tooling-design.md
Reminder-driven: the user runs /kite-snapshot, then `trading sip --date`.
The plan is a markdown menu (data/research/<date>/sip_plan.md) the user
executes manually over the month — no orders are placed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from trading.config import Paths, get_paths
from trading.data.kite import Holding
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_holdings,
)
from trading.data.sector import load_sector_map
from trading.ops.calendar import is_trading_day
from trading.ops.notify import notify
from trading.portfolio.allocator import (
    HoldingSnapshot,
    SipCandidate,
    SipPlan,
    allocate_sip,
)
from trading.portfolio.health import (
    FundamentalsSnapshot,
    HoldingContext,
    SentimentSnapshot,
    Verdict,
    score_holding,
    technicals_from_history,
)
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.ohlcv import read_ohlcv

CANDIDATE_WINDOW_TRADING_DAYS = 10
DEFAULT_SIP_BUDGET = 100_000.0
SECTOR_WARN_PCT = 0.30
UNKNOWN_SECTOR = "UNKNOWN"
_MAX_LOOKBACK_CALENDAR_DAYS = 40


class MonthlySipAborted(RuntimeError):  # noqa: N818 — "Aborted" is a state
    """Missing/stale Kite snapshot — /kite-snapshot must run first."""


@dataclass(frozen=True)
class MonthlySipResult:
    as_of: date
    budget: float
    holdings_count: int
    candidates_considered: int
    deployed: float
    cash_reserve: float
    allocations: int  # non-CASH allocation lines
    plan_path: Path | None  # None on dry_run
    warnings: list[str]


def trailing_trading_window(
    as_of: date, n: int = CANDIDATE_WINDOW_TRADING_DAYS
) -> tuple[date, date]:
    """(oldest, as_of): the last `n` trading days ending at and including
    `as_of` (when `as_of` itself is a trading day). Bounded at 40 calendar
    days so a broken calendar can't loop forever."""
    found: list[date] = []
    d = as_of
    while len(found) < n and (as_of - d).days <= _MAX_LOOKBACK_CALENDAR_DAYS:
        if is_trading_day(d):
            found.append(d)
        d = d - timedelta(days=1)
    oldest = found[-1] if found else as_of
    return oldest, as_of


def _score_holdings(
    paths: Paths, holdings: list[Holding], warnings: list[str]
) -> dict[str, Verdict]:
    """HOLD/TRIM/EXIT per holding — same scoring path pre_open uses
    (enriched parquet technicals; fundamentals/sentiment default-empty)."""
    verdicts: dict[str, Verdict] = {}
    for h in holdings:
        try:
            history = read_ohlcv(h.tradingsymbol, paths)
        except FileNotFoundError:
            warnings.append(f"no parquet for holding {h.tradingsymbol} — health unknown")
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
        verdicts[h.tradingsymbol] = score_holding(ctx).verdict
    return verdicts


def gather_candidates(
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    *,
    window: tuple[date, date],
    sector_map: dict[str, str],
    held: set[str],
    verdicts: dict[str, Verdict],
    warnings: list[str],
) -> list[SipCandidate]:
    """Distinct symbols with a signals row inside `window`. Priority =
    max ml_score (NULL → 0), entry = latest parquet close ≤ as_of."""
    oldest, newest = window
    rows = conn.execute(
        "SELECT symbol, MAX(COALESCE(ml_score, 0.0)) AS priority "
        "FROM signals "
        "WHERE substr(ts, 1, 10) >= ? AND substr(ts, 1, 10) <= ? "
        "GROUP BY symbol ORDER BY symbol",
        (oldest.isoformat(), newest.isoformat()),
    ).fetchall()
    out: list[SipCandidate] = []
    for r in rows:
        symbol = r["symbol"]
        try:
            df = read_ohlcv(symbol, paths, end=as_of)
        except FileNotFoundError:
            warnings.append(f"candidate {symbol}: no parquet — dropped")
            continue
        if df.empty:
            warnings.append(f"candidate {symbol}: no history ≤ {as_of} — dropped")
            continue
        out.append(
            SipCandidate(
                symbol=symbol,
                sector=sector_map.get(symbol, UNKNOWN_SECTOR),
                entry_price=float(df["close"].iloc[-1]),
                health=verdicts.get(symbol) if symbol in held else None,
                priority=float(r["priority"]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def sector_weights_after_plan(
    holdings: list[HoldingSnapshot],
    plan: SipPlan,
    symbol_sector: dict[str, str],
) -> list[tuple[str, float, float]]:
    """[(sector, value, weight)] for the post-plan portfolio, weight desc.
    Spec §7.4 concentration warning input."""
    by_sector: dict[str, float] = {}
    for h in holdings:
        by_sector[h.sector] = by_sector.get(h.sector, 0.0) + h.current_value
    for a in plan.allocations:
        if a.action in ("TOPUP", "NEW") and a.symbol:
            sec = symbol_sector.get(a.symbol, UNKNOWN_SECTOR)
            by_sector[sec] = by_sector.get(sec, 0.0) + a.amount
    total = sum(by_sector.values())
    if total <= 0:
        return []
    return sorted(
        ((sec, val, val / total) for sec, val in by_sector.items()),
        key=lambda t: t[2],
        reverse=True,
    )


def render_sip_plan(
    as_of: date,
    budget: float,
    plan: SipPlan,
    weights: list[tuple[str, float, float]],
    holdings_count: int,
    window: tuple[date, date],
) -> str:
    """Pure markdown renderer for sip_plan.md."""
    lines: list[str] = [
        f"# SIP plan — {as_of.isoformat()}",
        "",
        f"_Budget ₹{budget:,.0f} · deployed ₹{plan.deployed:,.0f} · "
        f"cash reserve ₹{plan.cash_reserve:,.0f}_",
        "",
        "## Allocations",
        "",
    ]
    if plan.allocations:
        lines += ["| Action | Symbol | Amount | Rationale |", "|---|---|---|---|"]
        lines += [
            f"| {a.action} | {a.symbol or '—'} | ₹{a.amount:,.0f} | {a.rationale} |"
            for a in plan.allocations
        ]
    else:
        lines.append("_(no allocations)_")

    lines += ["", "## Skipped", ""]
    if plan.skipped:
        lines += ["| Symbol | Reason |", "|---|---|"]
        lines += [f"| {sym} | {reason} |" for sym, reason in plan.skipped]
    else:
        lines.append("_(none)_")

    lines += ["", "## Post-plan sector weights", ""]
    if weights:
        lines += ["| Sector | Value | Weight | Flag |", "|---|---|---|---|"]
        lines += [
            f"| {sec} | ₹{val:,.0f} | {w:.0%} | {'⚠️ over 30%' if w > SECTOR_WARN_PCT else ''} |"
            for sec, val, w in weights
        ]
    else:
        lines.append("_(no data)_")

    lines += [
        "",
        f"_Inputs: {holdings_count} holdings; candidate window "
        f"{window[0].isoformat()} → {window[1].isoformat()} "
        f"({CANDIDATE_WINDOW_TRADING_DAYS} trading days)._",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_monthly_sip(
    as_of: date,
    *,
    paths: Paths | None = None,
    budget: float = DEFAULT_SIP_BUDGET,
    dry_run: bool = False,
) -> MonthlySipResult:
    """Compute and persist the month's SIP plan.

    Hard dependency: today's Kite snapshot (raises MonthlySipAborted).
    Everything else degrades gracefully into `warnings`.
    """
    p = paths if paths is not None else get_paths()
    warnings: list[str] = []

    try:
        holdings = read_holdings(p, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError) as e:
        raise MonthlySipAborted(str(e)) from e

    sector_map = load_sector_map(p)
    held = {h.tradingsymbol for h in holdings}
    verdicts = _score_holdings(p, holdings, warnings)
    holding_snaps = [
        HoldingSnapshot(
            symbol=h.tradingsymbol,
            sector=sector_map.get(h.tradingsymbol, UNKNOWN_SECTOR),
            current_value=h.quantity * h.last_price,
        )
        for h in holdings
    ]

    window = trailing_trading_window(as_of)
    with get_conn(p.db_path) as conn:
        run_migrations(conn)
        candidates = gather_candidates(
            conn,
            p,
            as_of,
            window=window,
            sector_map=sector_map,
            held=held,
            verdicts=verdicts,
            warnings=warnings,
        )

    plan = allocate_sip(candidates, holding_snaps, budget=budget)
    weights = sector_weights_after_plan(holding_snaps, plan, sector_map)

    plan_path: Path | None = None
    if not dry_run:
        out_dir = p.research_dir / as_of.isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)
        plan_path = out_dir / "sip_plan.md"
        plan_path.write_text(
            render_sip_plan(as_of, budget, plan, weights, len(holdings), window),
            encoding="utf-8",
        )
        top = [a for a in plan.allocations if a.action != "CASH"][:3]
        top_txt = ", ".join(f"{a.symbol} ₹{a.amount:,.0f}" for a in top) or "none"
        notify(
            "info",
            f"💰 SIP plan {as_of.isoformat()}",
            f"Deployed ₹{plan.deployed:,.0f} of ₹{budget:,.0f} "
            f"(cash ₹{plan.cash_reserve:,.0f})\nTop: {top_txt}",
        )

    return MonthlySipResult(
        as_of=as_of,
        budget=budget,
        holdings_count=len(holdings),
        candidates_considered=len(candidates),
        deployed=plan.deployed,
        cash_reserve=plan.cash_reserve,
        allocations=sum(1 for a in plan.allocations if a.action != "CASH"),
        plan_path=plan_path,
        warnings=warnings,
    )
