"""Phase 16 walk-forward training orchestrator.

Iterates the existing `walkforward.windows()` cadence, builds labelled
examples per fold, fits LightGBM, runs the engine on each test slice with
`RankerSignalProvider`, and trains the final production model on the most
recent train window.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from trading.backtest.walkforward import WalkForwardConfig

if TYPE_CHECKING:
    import lightgbm as lgb

    from trading.backtest.engine import BacktestConfig

MIN_TRAIN_EXAMPLES = 30


class InsufficientDataError(RuntimeError):
    """Raised when no fold (and the final window) can produce ≥ MIN_TRAIN_EXAMPLES."""


@dataclass(frozen=True)
class FoldMetrics:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    n_train_examples: int
    n_trades_oos: int
    sharpe_oos: float
    hit_rate_oos: float
    skipped: bool


@dataclass(frozen=True)
class TrainResult:
    folds: tuple[FoldMetrics, ...]
    final_model: "lgb.LGBMClassifier"
    final_train_start: pd.Timestamp
    final_train_end: pd.Timestamp
    n_final_examples: int
    oos_sharpe_mean: float
    oos_hit_rate_mean: float
    feature_names: tuple[str, ...]


def train_walkforward(
    enriched: Mapping[str, pd.DataFrame],
    macro_history: pd.DataFrame,
    sentiment_lookup: Mapping[tuple[str, str], object],
    negative_news_lookup: Mapping[tuple[str, str], int],
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    wf_cfg: WalkForwardConfig | None = None,
    bt_cfg: "BacktestConfig | None" = None,
) -> TrainResult:
    raise NotImplementedError
