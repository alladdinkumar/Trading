"""Phase 16 label builder — replays Phase 6 exit logic to derive binary labels.

Returns 1 if a simulated trade entered next-day at signal_date+1's open and
exited via Phase 6's evaluate_exit produces net P&L > 0, else 0. Returns
None if there aren't `max_days` forward bars available to resolve.
"""

from __future__ import annotations

import pandas as pd

from trading.backtest.costs import CostConfig


def label_candidate(
    enriched_df: pd.DataFrame,
    signal_date: pd.Timestamp,
    *,
    atr_stop_multiple: float = 1.5,
    max_days: int = 25,
    cost_config: CostConfig | None = None,
) -> int | None:
    raise NotImplementedError
