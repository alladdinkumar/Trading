"""Path A — offline, read-only factor evaluation.

Measures whether the cross-sectional factor composite has edge:
Information Coefficient (Spearman corr of score vs forward return) and a
factor-gated vs rules-only per-trade backtest. Forward returns reuse
``ranker_labels.realized_return`` so they resolve exactly as trades would.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading.ranking.ranker_labels import realized_return


def spearman_ic(
    scores: Mapping[str, float],
    fwd: Mapping[str, float],
    *,
    min_names: int = 5,
) -> float | None:
    """Spearman rank correlation between scores and forward returns.

    Uses only symbols present in both maps. Returns ``None`` when fewer than
    ``min_names`` overlap (too thin a cross-section to be meaningful).
    """
    common = sorted(set(scores) & set(fwd))
    if len(common) < min_names:
        return None
    s = pd.Series([scores[c] for c in common])
    f = pd.Series([fwd[c] for c in common])
    ic = s.corr(f, method="spearman")
    return None if pd.isna(ic) else float(ic)


@dataclass(frozen=True)
class ICResult:
    mean_ic: float
    ic_std: float
    ic_t_stat: float
    hit_rate_positive_days: float
    n_days: int


def aggregate_ic(ic_values: Sequence[float]) -> ICResult:
    """Aggregate per-day ICs into mean, stdev, t-stat and positive-day rate."""
    n = len(ic_values)
    if n == 0:
        return ICResult(0.0, 0.0, 0.0, 0.0, 0)
    arr = np.array(ic_values, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    t_stat = mean / (std / math.sqrt(n)) if std > 1e-12 else 0.0
    hit = float((arr > 0).mean())
    return ICResult(mean, std, t_stat, hit, n)


def forward_returns(
    panel: Mapping[str, pd.DataFrame],
    as_of: pd.Timestamp,
    *,
    max_days: int = 25,
) -> dict[str, float]:
    """Realized forward return per symbol from ``as_of`` (None entries dropped)."""
    out: dict[str, float] = {}
    for sym, df in panel.items():
        r = realized_return(df, as_of, max_days=max_days)
        if r is not None:
            out[sym] = r
    return out
