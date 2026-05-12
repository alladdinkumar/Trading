"""Position sizing — spec §4.4 & §4.5.

Pure function. Given a signal (entry, stop) and the account state, return the
integer share count that:

1. Caps loss at `risk_pct × capital` (default 2%) given the per-share risk.
2. Multiplies the risk budget by a regime factor (RISK_OFF 0.5×, NEUTRAL 0.75×).
3. Respects ≤25% capital in one stock and ≤30% capital in one sector.
4. Floors at zero — the caller decides whether to skip the trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

Regime = Literal["RISK_ON", "NEUTRAL", "RISK_OFF"]

REGIME_MULTIPLIER: dict[Regime, float] = {
    "RISK_ON": 1.0,
    "NEUTRAL": 0.75,
    "RISK_OFF": 0.5,
}


@dataclass(frozen=True)
class SizingInput:
    capital: float
    entry: float
    stop: float
    risk_pct: float = 0.02
    regime: Regime = "RISK_ON"
    deployed_in_symbol: float = 0.0
    deployed_in_sector: float = 0.0
    max_per_stock_pct: float = 0.25
    max_per_sector_pct: float = 0.30


@dataclass(frozen=True)
class SizingResult:
    qty: int
    notional: float
    capital_at_risk: float
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _validate(inp: SizingInput) -> None:
    if inp.capital <= 0:
        raise ValueError(f"capital must be > 0 (got {inp.capital})")
    if inp.entry <= 0:
        raise ValueError(f"entry must be > 0 (got {inp.entry})")
    if inp.entry <= inp.stop:
        raise ValueError(f"entry must be > stop (got entry={inp.entry}, stop={inp.stop})")
    if not (0 < inp.risk_pct <= 1):
        raise ValueError(f"risk_pct must be in (0, 1] (got {inp.risk_pct})")


def position_size(inp: SizingInput) -> SizingResult:
    """Return integer share count + the constraints that bound it.

    Order of operations:
        risk_qty   = floor(capital × risk_pct × regime_mult / (entry − stop))
        stock_qty  = floor((stock_cap × capital − deployed_in_symbol) / entry)
        sector_qty = floor((sector_cap × capital − deployed_in_sector) / entry)
        qty        = max(0, min(risk_qty, stock_qty, sector_qty))
    """
    _validate(inp)

    per_share_risk = inp.entry - inp.stop
    regime_mult = REGIME_MULTIPLIER[inp.regime]
    risk_budget = inp.capital * inp.risk_pct * regime_mult
    risk_qty = math.floor(risk_budget / per_share_risk)

    stock_budget = inp.max_per_stock_pct * inp.capital - inp.deployed_in_symbol
    stock_qty = math.floor(stock_budget / inp.entry) if stock_budget > 0 else 0

    sector_budget = inp.max_per_sector_pct * inp.capital - inp.deployed_in_sector
    sector_qty = math.floor(sector_budget / inp.entry) if sector_budget > 0 else 0

    qty = max(0, min(risk_qty, stock_qty, sector_qty))

    reasons: list[str] = []
    if qty == 0:
        if risk_qty <= 0:
            reasons.append(f"budget too small for 1 share (risk budget ₹{risk_budget:.0f})")
        if stock_qty <= 0:
            reasons.append(f"per-stock cap reached (deployed ₹{inp.deployed_in_symbol:.0f})")
        if sector_qty <= 0:
            reasons.append(f"per-sector cap reached (deployed ₹{inp.deployed_in_sector:.0f})")
    else:
        # Name only the binding constraint(s) — the one(s) equal to qty.
        if qty == risk_qty:
            reasons.append(
                f"risk budget binds (₹{risk_budget:.0f} / ₹{per_share_risk:.2f} per share)"
            )
        if qty == stock_qty:
            reasons.append(f"per-stock cap binds ({inp.max_per_stock_pct * 100:.0f}% of capital)")
        if qty == sector_qty:
            reasons.append(f"per-sector cap binds ({inp.max_per_sector_pct * 100:.0f}% of capital)")

    return SizingResult(
        qty=qty,
        notional=qty * inp.entry,
        capital_at_risk=qty * per_share_risk,
        reasons=tuple(reasons),
    )
