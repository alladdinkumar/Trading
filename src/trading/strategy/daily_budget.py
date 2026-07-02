"""Daily capital-budget planner — paces new paper entries (design 2026-06-19).

Pure: no DB, no network. Ranks the day's selected signals by expected value
(`p_win × implied_return`), then greedy-fills a per-day notional budget
(default ₹7k) bounded by available cash, sizing each pick via
`strategy.sizing.position_size` so the 2%-risk and per-stock caps still apply.

The per-stock cap here is 50% of pool capital (₹50k on the default ₹100k pool),
deliberately looser than the firm-wide 25% default in `strategy.sizing`: the
₹7k daily deploy cap already paces how fast any one name accumulates, so the
per-stock ceiling exists to bound total concentration, not daily pace.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trading.costs import buy_side_cost
from trading.strategy.calibration import ScoreCalibration, calibrated_p_win
from trading.strategy.sizing import Regime, SizingInput, position_size

DEFAULT_POOL_CAPITAL = 100_000.0
DEFAULT_DAILY_DEPLOY_CAP = 7_000.0
DEFAULT_RISK_PCT = 0.02
DEFAULT_MAX_PER_STOCK_PCT = 0.50  # ₹50k on the ₹100k pool; daily cap paces fill
DEFAULT_PWIN_PRIOR = 0.5  # ml_score fallback when the ranker is absent
# F-048: bound correlated concentration the notional caps above never catch at the
# ₹7k/day pace — one open lot per name (no day-over-day pyramiding) and at most two
# names per sector. Both count already-open lots *and* picks made earlier this run.
DEFAULT_MAX_LOTS_PER_SYMBOL = 1
DEFAULT_MAX_LOTS_PER_SECTOR = 2


@dataclass(frozen=True)
class BudgetCandidate:
    """A selected signal offered to the planner."""

    symbol: str
    entry: float
    stop: float
    target: float
    ml_score: float | None
    sector: str | None = None


@dataclass(frozen=True)
class PlannedEntry:
    """A sized entry the planner decided to open today."""

    symbol: str
    qty: int
    entry: float
    stop: float
    target: float
    notional: float
    ev_score: float
    reason: str


@dataclass(frozen=True)
class DailyPlan:
    """The day's plan: what to open, what was skipped (with reasons), budget used."""

    entries: list[PlannedEntry]
    skipped: list[tuple[str, str]]
    budget_used: float
    budget_cap: float
    cash_before: float


def _ev(c: BudgetCandidate, cal: ScoreCalibration | None) -> float:
    # F-041: correct the model score with realised hit-rate where there's enough
    # evidence; otherwise fall back to the raw ml_score (or the 0.5 prior).
    p_win = calibrated_p_win(cal, c.ml_score, prior=DEFAULT_PWIN_PRIOR)
    implied_return = (c.target - c.entry) / c.entry
    return p_win * implied_return


def plan_daily_entries(
    candidates: list[BudgetCandidate],
    *,
    available_cash: float,
    deployed_by_symbol: dict[str, float],
    regime: Regime,
    pool_capital: float = DEFAULT_POOL_CAPITAL,
    daily_cap: float = DEFAULT_DAILY_DEPLOY_CAP,
    risk_pct: float = DEFAULT_RISK_PCT,
    max_per_stock_pct: float = DEFAULT_MAX_PER_STOCK_PCT,
    p_win_calibration: ScoreCalibration | None = None,
    open_lots_by_symbol: dict[str, int] | None = None,
    open_lots_by_sector: dict[str, int] | None = None,
    max_lots_per_symbol: int = DEFAULT_MAX_LOTS_PER_SYMBOL,
    max_lots_per_sector: int = DEFAULT_MAX_LOTS_PER_SECTOR,
) -> DailyPlan:
    """Plan today's entries: EV-ranked, greedy-filled within ₹daily_cap and cash.

    `p_win_calibration` (F-041), when supplied, replaces a candidate's raw
    `ml_score` with the realised hit-rate of its score band — closing the loop
    between the weekly calibration measurement and the planner's EV ranking.

    `open_lots_by_symbol` / `open_lots_by_sector` (F-048) carry the count of
    currently-open paper lots per name and per sector; the planner refuses a
    candidate once its name is at `max_lots_per_symbol` (default 1 — no
    day-over-day pyramiding) or its sector at `max_lots_per_sector` (default 2),
    counting lots opened earlier in this same run. A candidate with `sector=None`
    is never gated by the sector cap.
    """
    budget_cap = min(daily_cap, max(0.0, available_cash))
    ranked = sorted(candidates, key=lambda c: (-_ev(c, p_win_calibration), c.symbol))

    entries: list[PlannedEntry] = []
    skipped: list[tuple[str, str]] = []
    running_budget = budget_cap
    running_cash = available_cash
    lots_by_symbol = dict(open_lots_by_symbol or {})
    lots_by_sector = dict(open_lots_by_sector or {})

    for c in ranked:
        ev = _ev(c, p_win_calibration)
        if running_budget <= 0:
            skipped.append((c.symbol, "daily budget exhausted"))
            continue
        held_lots = lots_by_symbol.get(c.symbol, 0)
        if held_lots >= max_lots_per_symbol:
            skipped.append(
                (c.symbol, f"already holds {held_lots} lot(s) ≥ max {max_lots_per_symbol}/symbol")
            )
            continue
        if c.sector is not None:
            sector_lots = lots_by_sector.get(c.sector, 0)
            if sector_lots >= max_lots_per_sector:
                skipped.append(
                    (
                        c.symbol,
                        f"sector {c.sector} at {sector_lots} ≥ max {max_lots_per_sector}/sector",
                    )
                )
                continue
        base = position_size(
            SizingInput(
                capital=pool_capital,
                risk_pct=risk_pct,
                entry=c.entry,
                stop=c.stop,
                regime=regime,
                deployed_in_symbol=deployed_by_symbol.get(c.symbol, 0.0),
                max_per_stock_pct=max_per_stock_pct,
            )
        )
        if base.qty == 0:
            skipped.append((c.symbol, "risk/stock cap → 0: " + "; ".join(base.reasons)))
            continue
        cap_notional = min(base.notional, running_budget, running_cash)
        qty = math.floor(cap_notional / c.entry)
        if qty < 1:
            skipped.append((c.symbol, "budget/cash < 1 share"))
            continue
        notional = qty * c.entry
        entries.append(
            PlannedEntry(
                symbol=c.symbol,
                qty=qty,
                entry=c.entry,
                stop=c.stop,
                target=c.target,
                notional=notional,
                ev_score=ev,
                reason=f"ev {ev:.3f}; qty {qty}; budget left ₹{running_budget - notional:,.0f}",
            )
        )
        running_budget -= notional
        running_cash -= notional + buy_side_cost(notional)
        lots_by_symbol[c.symbol] = held_lots + 1
        if c.sector is not None:
            lots_by_sector[c.sector] = lots_by_sector.get(c.sector, 0) + 1

    return DailyPlan(
        entries=entries,
        skipped=skipped,
        budget_used=sum(e.notional for e in entries),
        budget_cap=budget_cap,
        cash_before=available_cash,
    )
