"""Tests for the pure daily-budget planner."""

from __future__ import annotations

import math

from trading.strategy.daily_budget import (
    DEFAULT_DAILY_DEPLOY_CAP,
    BudgetCandidate,
    plan_daily_entries,
)


def _cand(symbol: str, *, entry: float, target: float, ml: float | None, stop_frac: float = 0.97):
    return BudgetCandidate(
        symbol=symbol, entry=entry, stop=entry * stop_frac, target=target, ml_score=ml
    )


def test_ev_ordering_best_first() -> None:
    # B has higher ev (0.8 * 0.20) than A (0.5 * 0.05). B must be sized first.
    a = _cand("AAA", entry=100.0, target=105.0, ml=0.5)
    b = _cand("BBB", entry=100.0, target=120.0, ml=0.8)
    plan = plan_daily_entries(
        [a, b], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert plan.entries[0].symbol == "BBB"


def test_daily_cap_binds_on_notional() -> None:
    cands = [_cand(f"S{i}", entry=100.0, target=130.0, ml=0.7) for i in range(20)]
    plan = plan_daily_entries(
        cands, available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert sum(e.notional for e in plan.entries) <= DEFAULT_DAILY_DEPLOY_CAP + 1e-6


def test_cash_gate_binds_when_cash_below_cap() -> None:
    cands = [_cand(f"S{i}", entry=100.0, target=130.0, ml=0.7) for i in range(20)]
    plan = plan_daily_entries(
        cands, available_cash=350.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert sum(e.notional for e in plan.entries) <= 350.0


def test_per_stock_cap_honored_via_deployed() -> None:
    # 25% of 100k = 25k already deployed in AAA → stock cap leaves 0 room.
    a = _cand("AAA", entry=100.0, target=130.0, ml=0.9)
    plan = plan_daily_entries(
        [a], available_cash=1_000_000.0, deployed_by_symbol={"AAA": 25_000.0}, regime="RISK_ON"
    )
    assert plan.entries == []
    assert any(sym == "AAA" for sym, _ in plan.skipped)


def test_expensive_top_ev_clipped_cheaper_later_fills() -> None:
    # Top-EV share costs 6000 (1 share fits in 7k); a 100-rupee name then fills the rest.
    pricey = _cand("PRICEY", entry=6_000.0, target=9_000.0, ml=0.9)
    cheap = _cand("CHEAP", entry=100.0, target=140.0, ml=0.6)
    plan = plan_daily_entries(
        [pricey, cheap], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    symbols = {e.symbol for e in plan.entries}
    assert "PRICEY" in symbols
    assert "CHEAP" in symbols
    assert sum(e.notional for e in plan.entries) <= DEFAULT_DAILY_DEPLOY_CAP + 1e-6


def test_skip_when_budget_cannot_afford_one_share() -> None:
    # One pricey name eats the budget; a second pricey name can't afford a share.
    a = _cand("AAA", entry=6_000.0, target=9_000.0, ml=0.9)
    b = _cand("BBB", entry=6_500.0, target=9_000.0, ml=0.8)
    plan = plan_daily_entries(
        [a, b], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert any(sym == "BBB" and "1 share" in reason for sym, reason in plan.skipped)


def test_ml_none_uses_prior() -> None:
    a = _cand("AAA", entry=100.0, target=120.0, ml=None)
    plan = plan_daily_entries(
        [a], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    # 0.5 prior * 0.20 implied = 0.10 ev — rankable, sized.
    assert math.isclose(plan.entries[0].ev_score, 0.10, rel_tol=1e-6)


def test_no_cash_means_empty_plan() -> None:
    a = _cand("AAA", entry=100.0, target=120.0, ml=0.7)
    plan = plan_daily_entries(
        [a], available_cash=0.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert plan.entries == []
    assert plan.skipped and all("budget" in r for _, r in plan.skipped)


def test_empty_candidates_empty_plan() -> None:
    plan = plan_daily_entries(
        [], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert plan.entries == []
    assert plan.skipped == []
    assert plan.budget_used == 0.0
