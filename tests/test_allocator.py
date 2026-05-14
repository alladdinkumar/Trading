"""Tests for trading.portfolio.allocator — SIP split + concurrency / sector caps."""

from __future__ import annotations

from trading.portfolio.allocator import (
    DEFAULT_BUDGET,
    DEFAULT_MAX_DEPLOYED_PCT,
    HoldingSnapshot,
    SipCandidate,
    allocate_sip,
)


def _candidates(specs: list[tuple[str, str, float, str | None, float]]) -> list[SipCandidate]:
    """(symbol, sector, entry_price, health, priority)."""
    return [
        SipCandidate(symbol=s, sector=sec, entry_price=ep, health=h, priority=pr)  # type: ignore[arg-type]
        for s, sec, ep, h, pr in specs
    ]


# ---------------------------------------------------------------------------
# Empty / no-op
# ---------------------------------------------------------------------------


def test_no_candidates_returns_all_cash() -> None:
    plan = allocate_sip(candidates=[], holdings=[])
    assert plan.deployed == 0.0
    assert plan.cash_reserve == DEFAULT_BUDGET
    assert any(a.action == "CASH" for a in plan.allocations)


def test_empty_holdings_treats_all_as_new_entries() -> None:
    cands = _candidates([("A", "BANK", 500, None, 1.0)])
    plan = allocate_sip(candidates=cands, holdings=[])
    new_rows = [a for a in plan.allocations if a.action == "NEW"]
    assert len(new_rows) == 1
    assert new_rows[0].symbol == "A"


# ---------------------------------------------------------------------------
# Bucket routing — TOPUP vs NEW vs skipped
# ---------------------------------------------------------------------------


def test_hold_rated_existing_routes_to_topup() -> None:
    holdings = [HoldingSnapshot(symbol="A", sector="BANK", current_value=10_000)]
    cands = _candidates([("A", "BANK", 100, "HOLD", 1.0)])
    plan = allocate_sip(candidates=cands, holdings=holdings)
    topup_rows = [a for a in plan.allocations if a.action == "TOPUP"]
    assert len(topup_rows) == 1
    assert topup_rows[0].symbol == "A"


def test_trim_rated_existing_is_skipped_for_topup() -> None:
    holdings = [HoldingSnapshot(symbol="A", sector="BANK", current_value=10_000)]
    cands = _candidates([("A", "BANK", 100, "TRIM", 1.0)])
    plan = allocate_sip(candidates=cands, holdings=holdings)
    assert not any(a.action == "TOPUP" for a in plan.allocations)
    assert any(s[0] == "A" and "TRIM" in s[1] for s in plan.skipped)


def test_exit_rated_existing_is_skipped() -> None:
    holdings = [HoldingSnapshot(symbol="A", sector="BANK", current_value=10_000)]
    cands = _candidates([("A", "BANK", 100, "EXIT", 1.0)])
    plan = allocate_sip(candidates=cands, holdings=holdings)
    assert not any(a.action == "TOPUP" for a in plan.allocations)


def test_new_symbol_routes_to_new_entry() -> None:
    plan = allocate_sip(
        candidates=_candidates([("NEW1", "IT", 200, None, 1.0)]),
        holdings=[],
    )
    assert any(a.action == "NEW" and a.symbol == "NEW1" for a in plan.allocations)


# ---------------------------------------------------------------------------
# Caps — bucket / deploy / stock / sector
# ---------------------------------------------------------------------------


def test_deploy_cap_limits_total() -> None:
    """5 cheap new-entries → each grabs whatever it can; total deployed
    must not exceed the 60% deploy cap."""
    plan = allocate_sip(
        candidates=_candidates([
            (f"NEW{i}", f"S{i}", 100, None, 1.0) for i in range(5)
        ]),
        holdings=[],
    )
    cap = DEFAULT_BUDGET * DEFAULT_MAX_DEPLOYED_PCT
    assert plan.deployed <= cap + 1e-6


def test_topup_bucket_capped_at_50pct() -> None:
    """Even with multiple HOLD topup candidates, topup cap binds at 50% of budget."""
    holdings = [
        HoldingSnapshot(symbol=f"A{i}", sector=f"S{i}", current_value=200_000)
        for i in range(3)
    ]
    cands = _candidates([
        (f"A{i}", f"S{i}", 100, "HOLD", 1.0) for i in range(3)
    ])
    plan = allocate_sip(candidates=cands, holdings=holdings)
    topup_total = sum(a.amount for a in plan.allocations if a.action == "TOPUP")
    assert topup_total <= DEFAULT_BUDGET * 0.50 + 1e-6


def test_new_entries_bucket_capped_at_50pct() -> None:
    cands = _candidates([
        (f"NEW{i}", f"S{i}", 100, None, 1.0) for i in range(5)
    ])
    plan = allocate_sip(candidates=cands, holdings=[])
    new_total = sum(a.amount for a in plan.allocations if a.action == "NEW")
    assert new_total <= DEFAULT_BUDGET * 0.50 + 1e-6


def test_concurrency_cap_prevents_new_when_at_max() -> None:
    """Already at max_concurrent → every new candidate gets skipped with a
    concurrency reason (bucket-cap check happens before, so we set
    `new_cap_pct=1.0` to keep the bucket open)."""
    holdings = [
        HoldingSnapshot(symbol=f"H{i}", sector=f"S{i}", current_value=50_000)
        for i in range(4)
    ]
    cands = _candidates([
        (f"NEW{i}", f"X{i}", 100, None, 1.0) for i in range(3)
    ])
    plan = allocate_sip(
        candidates=cands, holdings=holdings, max_concurrent=4, new_cap_pct=1.0,
    )
    new_count = sum(1 for a in plan.allocations if a.action == "NEW")
    assert new_count == 0
    assert sum(1 for s in plan.skipped if "concurrency" in s[1]) == 3


def test_sector_cap_blocks_overconcentration() -> None:
    """Existing 70k in BANK → tight max_per_sector_pct=30% over (existing
    + deployable=60k) baseline gets the new BANK candidate skipped."""
    holdings = [HoldingSnapshot(symbol="A", sector="BANK", current_value=70_000)]
    cands = _candidates([("NEWBANK", "BANK", 100, None, 1.0)])
    plan = allocate_sip(
        candidates=cands,
        holdings=holdings,
        max_per_sector_pct=0.30,
    )
    new_rows = [a for a in plan.allocations if a.action == "NEW"]
    # 30% of (70k existing + 60k deploy) = 39k cap; BANK already at 70k → no room
    assert new_rows == []


def test_per_stock_cap_limits_topup_amount() -> None:
    """Existing huge BANK holding → 25% per-stock cap binds before bucket caps."""
    holdings = [HoldingSnapshot(symbol="A", sector="BANK", current_value=200_000)]
    cands = _candidates([("A", "BANK", 100, "HOLD", 1.0)])
    plan = allocate_sip(
        candidates=cands,
        holdings=holdings,
        max_per_stock_pct=0.25,
    )
    # 25% of (200k + 60k deploy) = 65k cap; existing 200k > cap → no room
    topup_rows = [a for a in plan.allocations if a.action == "TOPUP"]
    assert topup_rows == []
    # Either skipped or capped — either way no allocation
    assert plan.deployed == 0.0


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


def test_higher_priority_filled_first() -> None:
    """When the new-bucket cap can't fit both, higher-priority wins."""
    cands = _candidates([
        ("LOW",  "S1", 100, None, 0.1),
        ("HIGH", "S2", 100, None, 5.0),
    ])
    plan = allocate_sip(
        candidates=cands,
        holdings=[],
        budget=100_000,
        new_cap_pct=0.50,
        max_concurrent=1,
    )
    new_syms = [a.symbol for a in plan.allocations if a.action == "NEW"]
    assert new_syms == ["HIGH"]


# ---------------------------------------------------------------------------
# Cash reserve
# ---------------------------------------------------------------------------


def test_cash_reserve_is_budget_minus_deployed() -> None:
    cands = _candidates([("NEW1", "IT", 200, None, 1.0)])
    plan = allocate_sip(candidates=cands, holdings=[])
    assert plan.cash_reserve == plan.budget - plan.deployed
    cash_row = next(a for a in plan.allocations if a.action == "CASH")
    assert cash_row.amount == plan.cash_reserve


def test_cash_reserve_is_full_budget_when_nothing_deployed() -> None:
    plan = allocate_sip(candidates=[], holdings=[])
    assert plan.cash_reserve == plan.budget


# ---------------------------------------------------------------------------
# Whole-share rounding
# ---------------------------------------------------------------------------


def test_allocation_amounts_buy_at_least_one_share() -> None:
    """Allocation amount must always be ≥ entry_price (i.e. ≥1 share)."""
    cands = _candidates([("EXPENSIVE", "IT", 50_000, None, 1.0)])
    plan = allocate_sip(candidates=cands, holdings=[])
    new_rows = [a for a in plan.allocations if a.action == "NEW"]
    if new_rows:
        assert new_rows[0].amount >= 50_000


def test_unaffordable_share_is_skipped() -> None:
    """Entry price > deployable budget → no allocation, surface in skipped."""
    cands = _candidates([("ULTRA", "IT", 1_000_000, None, 1.0)])
    plan = allocate_sip(candidates=cands, holdings=[])
    assert not any(a.action == "NEW" for a in plan.allocations)
    assert any(s[0] == "ULTRA" for s in plan.skipped)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_allocator_is_deterministic() -> None:
    holdings = [HoldingSnapshot(symbol="A", sector="BANK", current_value=20_000)]
    cands = _candidates([
        ("A", "BANK", 100, "HOLD", 2.0),
        ("NEW", "IT", 200, None, 3.0),
    ])
    p1 = allocate_sip(candidates=cands, holdings=holdings)
    p2 = allocate_sip(candidates=cands, holdings=holdings)
    assert [a.amount for a in p1.allocations] == [a.amount for a in p2.allocations]
    assert [a.symbol for a in p1.allocations] == [a.symbol for a in p2.allocations]
