"""Daily capital-budget planner — paces new paper entries (design 2026-06-19).

Pure: no DB, no network. Ranks the day's selected signals by expected value
(`p_win × implied_return`), then greedy-fills a per-day notional budget
(default ₹7k) bounded by available cash, sizing each pick via
`strategy.sizing.position_size` so the 2%-risk and per-stock caps still apply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trading.paper.ledger import buy_side_cost
from trading.strategy.sizing import Regime, SizingInput, position_size

DEFAULT_POOL_CAPITAL = 100_000.0
DEFAULT_DAILY_DEPLOY_CAP = 7_000.0
DEFAULT_PWIN_PRIOR = 0.5  # ml_score fallback when the ranker is absent


@dataclass(frozen=True)
class BudgetCandidate:
    """A selected signal offered to the planner."""

    symbol: str
    entry: float
    stop: float
    target: float
    ml_score: float | None


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


def _ev(c: BudgetCandidate) -> float:
    p_win = c.ml_score if c.ml_score is not None else DEFAULT_PWIN_PRIOR
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
    risk_pct: float = 0.02,
) -> DailyPlan:
    """Plan today's entries: EV-ranked, greedy-filled within ₹daily_cap and cash."""
    budget_cap = min(daily_cap, max(0.0, available_cash))
    ranked = sorted(candidates, key=lambda c: (-_ev(c), c.symbol))

    entries: list[PlannedEntry] = []
    skipped: list[tuple[str, str]] = []
    running_budget = budget_cap
    running_cash = available_cash

    for c in ranked:
        ev = _ev(c)
        if running_budget <= 0:
            skipped.append((c.symbol, "daily budget exhausted"))
            continue
        base = position_size(
            SizingInput(
                capital=pool_capital,
                risk_pct=risk_pct,
                entry=c.entry,
                stop=c.stop,
                regime=regime,
                deployed_in_symbol=deployed_by_symbol.get(c.symbol, 0.0),
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

    return DailyPlan(
        entries=entries,
        skipped=skipped,
        budget_used=sum(e.notional for e in entries),
        budget_cap=budget_cap,
        cash_before=available_cash,
    )
