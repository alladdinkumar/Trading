"""Phase 7 — backtest engine, costs, metrics, walk-forward.

Public API:

- `run_backtest(enriched, config, start, end)` — single-period event-loop backtest.
- `run_walkforward(enriched, bt_cfg, wf_cfg, start, end)` — rolling test-window aggregation.
- `compute_metrics(result, benchmark)` — bundles CAGR / Sharpe / DD / hit-rate / etc.
"""

from trading.backtest.costs import (
    CostBreakdown,
    CostConfig,
    apply_slippage,
    buy_charges,
    round_trip_cost_pct,
    sell_charges,
)
from trading.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    Signal,
    SignalProvider,
    Trade,
    rule_signal_provider,
    run_backtest,
)
from trading.backtest.metrics import (
    MetricsBundle,
    alpha_beta,
    avg_r_multiple,
    cagr,
    compute_metrics,
    expectancy,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
)
from trading.backtest.walkforward import (
    WalkForwardConfig,
    Window,
    run_walkforward,
    windows,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "CostBreakdown",
    "CostConfig",
    "MetricsBundle",
    "Signal",
    "SignalProvider",
    "Trade",
    "WalkForwardConfig",
    "Window",
    "alpha_beta",
    "apply_slippage",
    "avg_r_multiple",
    "buy_charges",
    "cagr",
    "compute_metrics",
    "expectancy",
    "hit_rate",
    "max_drawdown",
    "profit_factor",
    "round_trip_cost_pct",
    "rule_signal_provider",
    "run_backtest",
    "run_walkforward",
    "sell_charges",
    "sharpe",
    "sortino",
    "windows",
]
