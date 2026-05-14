"""Monthly SIP allocator — split ₹1L across topups / new entries / cash (spec §7.3).

Pure function. The caller passes:
  - the monthly budget (default ₹1,00,000)
  - current holdings (symbol → current value)
  - candidates: this month's fresh signals + their HOLD/TRIM/EXIT verdict
  - concurrency caps (defaults from spec §4.4)

The output is a list of `Allocation` rows the brief renders directly,
together with an aggregate `SipPlan` summarising what got deployed.

Allocation rules (from the spec):
  1. Topping up = HOLD-rated existing holdings that fire a fresh dip signal
  2. New entries = signals on stocks not currently held
  3. Topups capped at 50% of budget; new entries also capped at 50%
  4. ≤60% of budget deployed per batch (rest = cash reserve)
  5. ≤25% of (current portfolio + this batch) in any one stock
  6. ≤30% in any one sector
  7. Max 5 concurrent open positions across the portfolio

Within each bucket we rank by `priority` (caller-supplied: conviction
score from the ranker, today's RSI, whatever); top-down greedy fill until
the bucket cap binds or no more candidates remain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AllocationAction = Literal["TOPUP", "NEW", "CASH"]


# ---------------------------------------------------------------------------
# Defaults — from spec §4.4 + §7.3
# ---------------------------------------------------------------------------

DEFAULT_BUDGET = 100_000.0
DEFAULT_MAX_PER_STOCK_PCT = 0.25
DEFAULT_MAX_PER_SECTOR_PCT = 0.30
DEFAULT_MAX_DEPLOYED_PCT = 0.60
DEFAULT_TOPUP_CAP_PCT = 0.50
DEFAULT_NEW_CAP_PCT = 0.50
DEFAULT_MAX_CONCURRENT = 5


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldingSnapshot:
    """Just what the allocator needs about a current position."""

    symbol: str
    sector: str
    current_value: float  # in ₹ (qty × last_price)


@dataclass(frozen=True)
class SipCandidate:
    """A signal eligible for allocation this batch.

    `health` is the existing-holding verdict from `portfolio.health.score_holding`;
    None means the stock isn't in the portfolio yet (so it goes into the
    new-entries bucket). `priority` orders candidates within the bucket —
    higher = filled first.
    """

    symbol: str
    sector: str
    entry_price: float
    health: Literal["HOLD", "TRIM", "EXIT"] | None = None
    priority: float = 0.0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allocation:
    """One line of the allocation plan."""

    action: AllocationAction
    symbol: str | None
    amount: float
    rationale: str


@dataclass(frozen=True)
class SipPlan:
    """Aggregated allocator output for the markdown brief."""

    budget: float
    deployed: float
    cash_reserve: float
    allocations: list[Allocation] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (symbol, reason)


# ---------------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------------


def allocate_sip(
    candidates: list[SipCandidate],
    holdings: list[HoldingSnapshot],
    *,
    budget: float = DEFAULT_BUDGET,
    max_per_stock_pct: float = DEFAULT_MAX_PER_STOCK_PCT,
    max_per_sector_pct: float = DEFAULT_MAX_PER_SECTOR_PCT,
    max_deployed_pct: float = DEFAULT_MAX_DEPLOYED_PCT,
    topup_cap_pct: float = DEFAULT_TOPUP_CAP_PCT,
    new_cap_pct: float = DEFAULT_NEW_CAP_PCT,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> SipPlan:
    """Compute this month's ₹-allocation plan. Pure, deterministic.

    All percentage caps are taken against `(total portfolio value + this
    month's deployable budget)` — that's the post-investment portfolio
    we'd actually hold, and the spec's caps apply to that snapshot.
    """
    deployable = budget * max_deployed_pct
    topup_cap = budget * topup_cap_pct
    new_cap = budget * new_cap_pct

    by_symbol = {h.symbol: h for h in holdings}
    sector_value: dict[str, float] = {}
    for h in holdings:
        sector_value[h.sector] = sector_value.get(h.sector, 0.0) + h.current_value

    portfolio_value = sum(h.current_value for h in holdings)
    # Post-investment baseline used for the % caps
    post_invest_baseline = portfolio_value + deployable

    open_positions = len([h for h in holdings if h.current_value > 0])

    allocations: list[Allocation] = []
    skipped: list[tuple[str, str]] = []
    deployed = 0.0
    topup_spent = 0.0
    new_spent = 0.0

    # Split candidates into buckets; topups must reference an existing,
    # HOLD-rated position. EXITs are skipped (we don't add to a fading name).
    topups = [
        c for c in candidates
        if c.symbol in by_symbol and c.health == "HOLD"
    ]
    new_entries = [c for c in candidates if c.symbol not in by_symbol]

    # Diagnostic: surface why a candidate didn't make it into either bucket
    for c in candidates:
        if c.symbol in by_symbol and c.health != "HOLD":
            reason = (
                f"existing holding rated {c.health}; no top-up"
                if c.health is not None
                else "existing holding with no health verdict"
            )
            skipped.append((c.symbol, reason))

    topups.sort(key=lambda c: c.priority, reverse=True)
    new_entries.sort(key=lambda c: c.priority, reverse=True)

    def _per_stock_cap(symbol: str) -> float:
        """Remaining ₹ we can put into `symbol` without breaching the cap."""
        existing = by_symbol[symbol].current_value if symbol in by_symbol else 0.0
        cap_total = post_invest_baseline * max_per_stock_pct
        return max(0.0, cap_total - existing)

    def _per_sector_remaining(sector: str) -> float:
        cap_total = post_invest_baseline * max_per_sector_pct
        return max(0.0, cap_total - sector_value.get(sector, 0.0))

    def _take(cand: SipCandidate, bucket_remaining: float, action: AllocationAction) -> float:
        nonlocal deployed, open_positions
        if bucket_remaining <= 0:
            skipped.append((cand.symbol, f"{action.lower()} bucket exhausted"))
            return 0.0
        if action == "NEW" and open_positions >= max_concurrent:
            skipped.append((cand.symbol, f"concurrency cap ({max_concurrent}) reached"))
            return 0.0

        stock_cap = _per_stock_cap(cand.symbol)
        sector_cap = _per_sector_remaining(cand.sector)
        amount = min(bucket_remaining, stock_cap, sector_cap, deployable - deployed)
        # Round to whole rupees — fractional ₹ is meaningless on NSE
        amount = float(int(amount))
        if amount < cand.entry_price:
            # Can't even buy 1 share — skip rather than emit a useless row
            why = []
            if stock_cap < cand.entry_price:
                why.append("per-stock cap")
            if sector_cap < cand.entry_price:
                why.append(f"sector cap ({cand.sector})")
            if (deployable - deployed) < cand.entry_price:
                why.append("deploy cap")
            skipped.append((cand.symbol, ", ".join(why) or "amount < 1 share"))
            return 0.0

        deployed += amount
        sector_value[cand.sector] = sector_value.get(cand.sector, 0.0) + amount
        if action == "NEW":
            by_symbol[cand.symbol] = HoldingSnapshot(
                symbol=cand.symbol, sector=cand.sector, current_value=amount
            )
            open_positions += 1
        else:
            prev = by_symbol[cand.symbol]
            by_symbol[cand.symbol] = HoldingSnapshot(
                symbol=prev.symbol, sector=prev.sector,
                current_value=prev.current_value + amount,
            )

        rationale = (
            f"{action.lower()} {cand.symbol}: priority {cand.priority:.2f}, "
            f"qty ~{int(amount / cand.entry_price)}"
        )
        allocations.append(Allocation(action=action, symbol=cand.symbol,
                                       amount=amount, rationale=rationale))
        return amount

    for cand in topups:
        if topup_spent >= topup_cap:
            break
        spent = _take(cand, topup_cap - topup_spent, "TOPUP")
        topup_spent += spent

    for cand in new_entries:
        if new_spent >= new_cap:
            break
        spent = _take(cand, new_cap - new_spent, "NEW")
        new_spent += spent

    cash_reserve = budget - deployed
    if cash_reserve > 0:
        allocations.append(
            Allocation(
                action="CASH",
                symbol=None,
                amount=cash_reserve,
                rationale=(
                    f"hold {cash_reserve / budget:.0%} as reserve — "
                    f"deploy cap {max_deployed_pct:.0%}, "
                    f"or quality signal shortfall"
                ),
            )
        )

    return SipPlan(
        budget=budget,
        deployed=deployed,
        cash_reserve=cash_reserve,
        allocations=allocations,
        skipped=skipped,
    )
