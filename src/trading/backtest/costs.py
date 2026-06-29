"""Backward-compatible re-export of the pure cost model (F-045).

The cost model was relocated to the neutral foundation module `trading.costs`
so decision code (`strategy.daily_budget`) can import it **down** instead of
reaching up into `paper.ledger`. This shim keeps the historical
`trading.backtest.costs` import path — and the `backtest` public API in
`backtest/__init__.py` — working unchanged.
"""

from __future__ import annotations

from trading.costs import (
    CostBreakdown,
    CostConfig,
    Side,
    apply_slippage,
    buy_charges,
    buy_side_cost,
    round_trip_cost_pct,
    sell_charges,
    sell_side_cost,
)

__all__ = [
    "CostBreakdown",
    "CostConfig",
    "Side",
    "apply_slippage",
    "buy_charges",
    "buy_side_cost",
    "round_trip_cost_pct",
    "sell_charges",
    "sell_side_cost",
]
