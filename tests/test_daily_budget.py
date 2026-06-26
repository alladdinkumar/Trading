"""Tests for the pure daily-budget planner."""

from __future__ import annotations

import math

from trading.strategy.daily_budget import (
    DEFAULT_DAILY_DEPLOY_CAP,
    BudgetCandidate,
    plan_daily_entries,
)


def _cand(
    symbol: str,
    *,
    entry: float,
    target: float,
    ml: float | None,
    stop_frac: float = 0.97,
    sector: str | None = None,
):
    return BudgetCandidate(
        symbol=symbol,
        entry=entry,
        stop=entry * stop_frac,
        target=target,
        ml_score=ml,
        sector=sector,
    )


def test_skips_symbol_already_open_at_lot_cap() -> None:
    # F-048: a name already holding the max open lots (default 1) is not re-entered.
    a = _cand("AAA", entry=100.0, target=130.0, ml=0.9)
    plan = plan_daily_entries(
        [a],
        available_cash=1_000_000.0,
        deployed_by_symbol={},
        regime="RISK_ON",
        open_lots_by_symbol={"AAA": 1},
    )
    assert plan.entries == []
    assert any(sym == "AAA" and "lot" in reason.lower() for sym, reason in plan.skipped)


def test_skips_sector_at_lot_cap() -> None:
    # F-048: a sector already at the cap (default 2) blocks a further name in it.
    a = _cand("AAA", entry=100.0, target=130.0, ml=0.9, sector="Power")
    plan = plan_daily_entries(
        [a],
        available_cash=1_000_000.0,
        deployed_by_symbol={},
        regime="RISK_ON",
        open_lots_by_sector={"Power": 2},
    )
    assert plan.entries == []
    assert any(sym == "AAA" and "sector" in reason.lower() for sym, reason in plan.skipped)


def test_sector_cap_counts_within_run() -> None:
    # F-048: one Power lot already open + cap 2 ⇒ only one new Power name fits;
    # the higher-EV one fills, the next is skipped on the sector cap.
    a = _cand("AAA", entry=100.0, target=130.0, ml=0.9, sector="Power")
    b = _cand("BBB", entry=100.0, target=130.0, ml=0.8, sector="Power")
    plan = plan_daily_entries(
        [a, b],
        available_cash=1_000_000.0,
        deployed_by_symbol={},
        regime="RISK_ON",
        daily_cap=1_000_000.0,  # headroom so the sector cap, not the budget, binds
        open_lots_by_sector={"Power": 1},
    )
    assert [e.symbol for e in plan.entries] == ["AAA"]
    assert any(sym == "BBB" and "sector" in reason.lower() for sym, reason in plan.skipped)


def test_none_sector_exempt_from_sector_cap() -> None:
    # Regression guard: sector-less candidates are never gated by the sector cap.
    cands = [_cand(f"S{i}", entry=100.0, target=130.0, ml=0.7) for i in range(3)]
    plan = plan_daily_entries(
        cands,
        available_cash=1_000_000.0,
        deployed_by_symbol={},
        regime="RISK_ON",
        open_lots_by_sector={None: 5},
    )
    assert not any("sector" in reason.lower() for _, reason in plan.skipped)


def test_calibration_corrects_optimistic_p_win() -> None:
    """F-041: a band the model is over-confident about is pulled to its realised
    hit-rate, so EV reflects reality instead of the raw ml_score."""
    import pytest

    from trading.strategy.calibration import build_score_calibration

    # Model says ~0.72 for this band, but only 2 of 10 actually won.
    cal = build_score_calibration([(0.72, i < 2) for i in range(10)], n_bins=5, min_n=5)
    c = _cand("AAA", entry=100.0, target=110.0, ml=0.72)  # implied_return 0.10
    plan = plan_daily_entries(
        [c],
        available_cash=1_000_000.0,
        deployed_by_symbol={},
        regime="RISK_ON",
        p_win_calibration=cal,
    )
    # p_win corrected 0.72 → 0.20; ev = 0.20 * 0.10 = 0.02 (not 0.072).
    assert plan.entries[0].ev_score == pytest.approx(0.02)


def test_calibration_none_keeps_raw_score() -> None:
    import pytest

    c = _cand("AAA", entry=100.0, target=110.0, ml=0.72)
    plan = plan_daily_entries(
        [c], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert plan.entries[0].ev_score == pytest.approx(0.072)


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
    # 50% of 100k = 50k already deployed in AAA → stock cap leaves 0 room.
    a = _cand("AAA", entry=100.0, target=130.0, ml=0.9)
    plan = plan_daily_entries(
        [a], available_cash=1_000_000.0, deployed_by_symbol={"AAA": 50_000.0}, regime="RISK_ON"
    )
    assert plan.entries == []
    assert any(sym == "AAA" for sym, _ in plan.skipped)


def test_per_stock_cap_allows_topup_below_50pct() -> None:
    # 25k deployed is under the 50% (₹50k) cap, so a top-up still sizes.
    a = _cand("AAA", entry=100.0, target=130.0, ml=0.9)
    plan = plan_daily_entries(
        [a], available_cash=1_000_000.0, deployed_by_symbol={"AAA": 25_000.0}, regime="RISK_ON"
    )
    assert plan.entries and plan.entries[0].symbol == "AAA"


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
