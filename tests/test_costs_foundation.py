"""F-045 — the pure cost model lives in the neutral foundation `trading.costs`.

Locks the relocation: the cost symbols must be importable from `trading.costs`,
and the legacy import paths (`trading.backtest.costs`, `trading.paper.ledger`)
must re-export the *same* objects so existing call sites keep working.
"""

from __future__ import annotations


def test_pure_cost_model_importable_from_foundation() -> None:
    from trading.costs import (  # noqa: F401
        CostBreakdown,
        CostConfig,
        apply_slippage,
        buy_charges,
        buy_side_cost,
        round_trip_cost_pct,
        sell_charges,
        sell_side_cost,
    )


def test_backtest_costs_reexports_same_objects() -> None:
    import trading.backtest.costs as bt_costs
    import trading.costs as costs

    assert bt_costs.CostConfig is costs.CostConfig
    assert bt_costs.buy_charges is costs.buy_charges
    assert bt_costs.sell_charges is costs.sell_charges
    assert bt_costs.apply_slippage is costs.apply_slippage


def test_ledger_reexports_same_side_cost_helpers() -> None:
    import trading.costs as costs
    from trading.paper import ledger

    assert ledger.buy_side_cost is costs.buy_side_cost
    assert ledger.sell_side_cost is costs.sell_side_cost
