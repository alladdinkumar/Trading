"""Zerodha equity-delivery cost model — spec §8.2.

All percentages are stored as decimals (0.1% = 0.001). Public API:

- `buy_charges(value, cfg)` — explicit charges on a buy. Cash debited = value + charges.total.
- `sell_charges(value, cfg)` — explicit charges on a sell. Cash credited = value − charges.total.
- `apply_slippage(price, side, cfg)` — slippage as a price shift, not a fee.

`CostConfig` defaults match Zerodha equity delivery as of the design date
(see spec §8.2). Brokerage is the documented free-tier safety value of
`min(₹20, 0.03% × turnover)` to give the backtest a non-zero floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class CostConfig:
    brokerage_pct: float = 0.0003  # 0.03% (capped by brokerage_cap)
    brokerage_cap: float = 20.0  # ₹20 per order
    stt_buy_pct: float = 0.001  # 0.1% on buy turnover
    stt_sell_pct: float = 0.001  # 0.1% on sell turnover
    exchange_tx_pct: float = 0.0000297  # 0.00297% per side
    sebi_pct: float = 0.000001  # 0.0001% per side
    stamp_duty_pct: float = 0.00015  # 0.015% on buy only
    gst_pct: float = 0.18  # 18% on (brokerage + exchange + sebi)
    slippage_pct: float = 0.001  # 0.1% per side, as price shift


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    total: float


def _brokerage(value: float, cfg: CostConfig) -> float:
    return min(cfg.brokerage_cap, value * cfg.brokerage_pct)


def buy_charges(value: float, cfg: CostConfig | None = None) -> CostBreakdown:
    if value <= 0:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    c = cfg or CostConfig()
    brokerage = _brokerage(value, c)
    stt = value * c.stt_buy_pct
    exchange = value * c.exchange_tx_pct
    sebi = value * c.sebi_pct
    stamp = value * c.stamp_duty_pct
    gst = c.gst_pct * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + sebi + stamp + gst
    return CostBreakdown(brokerage, stt, exchange, sebi, stamp, gst, total)


def sell_charges(value: float, cfg: CostConfig | None = None) -> CostBreakdown:
    if value <= 0:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    c = cfg or CostConfig()
    brokerage = _brokerage(value, c)
    stt = value * c.stt_sell_pct
    exchange = value * c.exchange_tx_pct
    sebi = value * c.sebi_pct
    # No stamp duty on sell side.
    gst = c.gst_pct * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + sebi + gst
    return CostBreakdown(brokerage, stt, exchange, sebi, 0.0, gst, total)


def apply_slippage(price: float, side: Side, cfg: CostConfig | None = None) -> float:
    """Adverse price shift — buy pays slightly more, sell receives slightly less."""
    c = cfg or CostConfig()
    if side == "buy":
        return price * (1.0 + c.slippage_pct)
    return price * (1.0 - c.slippage_pct)


def round_trip_cost_pct(value: float, cfg: CostConfig | None = None) -> float:
    """Total round-trip cost as a fraction of `value`. Includes slippage.

    For a flat trade (no P&L), this is the percentage drag from costs alone.
    Pinned by `test_round_trip_total_around_0_4_pct` at ~0.4%.
    """
    c = cfg or CostConfig()
    explicit = buy_charges(value, c).total + sell_charges(value, c).total
    slippage = 2 * value * c.slippage_pct
    return (explicit + slippage) / value
