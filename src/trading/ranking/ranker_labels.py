"""Phase 16 label builder — replays Phase 6 exit logic to derive binary labels.

Returns 1 if a simulated trade entered next-day at signal_date+1's open and
exited via Phase 6's evaluate_exit produces net P&L > 0, else 0. Returns
None if there aren't `max_days` forward bars available to resolve.

The forward-return replay itself lives in :mod:`trading.backtest.forward_return`
(the architecturally correct home — it depends only on the cost model and exit
logic). This module re-exports it and adds the ML-specific sign label on top.
"""

from __future__ import annotations

import pandas as pd

from trading.backtest.costs import CostConfig
from trading.backtest.forward_return import realized_return

__all__ = ["label_candidate", "realized_return"]


def label_candidate(
    enriched_df: pd.DataFrame,
    signal_date: pd.Timestamp,
    *,
    atr_stop_multiple: float = 1.5,
    max_days: int = 25,
    cost_config: CostConfig | None = None,
) -> int | None:
    """Binary win/loss label — the sign of `realized_return`.

    Returns 1 if net P&L > 0 at exit, 0 otherwise, None when the forward
    window is too short to resolve.
    """
    r = realized_return(
        enriched_df,
        signal_date,
        atr_stop_multiple=atr_stop_multiple,
        max_days=max_days,
        cost_config=cost_config,
    )
    if r is None:
        return None
    return 1 if r > 0 else 0
