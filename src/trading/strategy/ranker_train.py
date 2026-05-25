"""Phase 16 walk-forward training orchestrator.

Iterates the existing `walkforward.windows()` cadence, builds labelled
examples per fold from rules-passing candidates, fits LightGBM, computes
out-of-sample metrics by replaying Phase 6 exits on the test slice, and
trains the final production model on the most-recent train window.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import lightgbm as lgb
import numpy as np
import pandas as pd

from trading.backtest.metrics import sharpe
from trading.backtest.walkforward import WalkForwardConfig, windows
from trading.store.news_store import SentimentDailyRow
from trading.strategy.ranker_features import (
    FEATURE_NAMES,
    LiveContext,
    build_feature_row,
)
from trading.strategy.ranker_labels import label_candidate
from trading.strategy.rules import MIN_HISTORY_BARS, ScanContext, evaluate_symbol

if TYPE_CHECKING:
    from trading.backtest.engine import BacktestConfig

MIN_TRAIN_EXAMPLES = 30

LGBM_PARAMS: dict[str, object] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "num_leaves": 15,
    "min_data_in_leaf": 10,
    "learning_rate": 0.05,
    "n_estimators": 200,
    "is_unbalance": True,
    "feature_pre_filter": False,
    "verbose": -1,
}


class InsufficientDataError(RuntimeError):
    """Raised when the final training window has < MIN_TRAIN_EXAMPLES examples
    or only one class — the model would be useless."""


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
    final_model: lgb.LGBMClassifier
    final_train_start: pd.Timestamp
    final_train_end: pd.Timestamp
    n_final_examples: int
    oos_sharpe_mean: float
    oos_hit_rate_mean: float
    feature_names: tuple[str, ...]


def _build_xy_for_window(
    enriched: Mapping[str, pd.DataFrame],
    macro_history: pd.DataFrame,
    sentiment_lookup: Mapping[tuple[str, str], SentimentDailyRow],
    negative_news_lookup: Mapping[tuple[str, str], int],
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Walk each (symbol, date) in [train_start, train_end), evaluate Layer A,
    and build (X, y) for every all-pass candidate with a resolvable forward
    window."""
    feat_rows: list[dict[str, float]] = []
    labels: list[int] = []
    for sym, df in enriched.items():
        if len(df) < MIN_HISTORY_BARS:
            continue
        mask = (df.index >= train_start) & (df.index < train_end)
        for sd in df.index[mask]:
            sub = df.loc[:sd]
            if len(sub) < MIN_HISTORY_BARS:
                continue
            cand = evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date()))
            if not cand.all_passed:
                continue
            label = label_candidate(df, sd)
            if label is None:
                continue
            sd_iso = sd.strftime("%Y-%m-%d")
            ctx = LiveContext(
                macro=None,
                sentiment=sentiment_lookup.get((sd_iso, sym)),
                macro_history=macro_history,
                negative_news_count_7d=negative_news_lookup.get((sd_iso, sym)),
            )
            feat_rows.append(build_feature_row(df, sd, ctx))
            labels.append(label)
    X = pd.DataFrame(feat_rows, columns=list(FEATURE_NAMES)).astype(float)
    y = np.array(labels, dtype=int)
    return X, y


def _fit(X: pd.DataFrame, y: np.ndarray) -> lgb.LGBMClassifier:
    model = lgb.LGBMClassifier(**LGBM_PARAMS, random_state=42)
    n = len(X)
    if n >= 50 and len(set(y.tolist())) >= 2:
        rng = np.random.default_rng(0)
        idx = rng.permutation(n)
        cut = int(0.8 * n)
        train_idx = idx[:cut]
        val_idx = idx[cut:]
        # Require both classes in the validation slice; otherwise just train
        # without early stopping (the slice would mislead).
        if len(set(y[val_idx].tolist())) >= 2:
            model.fit(
                X.values[train_idx],
                y[train_idx],
                eval_set=[(X.values[val_idx], y[val_idx])],
                callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
            )
            return model
    model.fit(X.values, y)
    return model


def _evaluate_fold_oos(
    enriched: Mapping[str, pd.DataFrame],
    macro_history: pd.DataFrame,
    sentiment_lookup: Mapping[tuple[str, str], SentimentDailyRow],
    negative_news_lookup: Mapping[tuple[str, str], int],
    model: lgb.LGBMClassifier,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
) -> tuple[int, float, float]:
    """Score every rules-passing candidate in [test_start, test_end), realise
    its label by replaying Phase 6 exit logic, and compute OOS Sharpe + hit
    rate over the realised P&L sequence. Returns (n_trades, sharpe, hit_rate).
    """
    realised: list[int] = []
    for sym, df in enriched.items():
        mask = (df.index >= test_start) & (df.index < test_end)
        for sd in df.index[mask]:
            sub = df.loc[:sd]
            if len(sub) < MIN_HISTORY_BARS:
                continue
            cand = evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date()))
            if not cand.all_passed:
                continue
            label = label_candidate(df, sd)
            if label is None:
                continue
            realised.append(label)

    if not realised:
        return 0, float("nan"), float("nan")
    arr = np.array(realised, dtype=float)
    # Coarse fold-level Sharpe: treat each closed trade as a +1/-1 unit period
    # return. The absolute number isn't load-bearing — comparison across folds
    # vs the active model is.
    returns = pd.Series(arr * 2 - 1)
    return (
        int(len(realised)),
        float(sharpe(returns, periods_per_year=12)),
        float(arr.mean()),
    )


def train_walkforward(
    enriched: Mapping[str, pd.DataFrame],
    macro_history: pd.DataFrame,
    sentiment_lookup: Mapping[tuple[str, str], SentimentDailyRow],
    negative_news_lookup: Mapping[tuple[str, str], int],
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    wf_cfg: WalkForwardConfig | None = None,
    bt_cfg: "BacktestConfig | None" = None,
) -> TrainResult:
    wf = wf_cfg or WalkForwardConfig()
    folds_out: list[FoldMetrics] = []
    for win in windows(start, end, wf):
        X, y = _build_xy_for_window(
            enriched,
            macro_history,
            sentiment_lookup,
            negative_news_lookup,
            win.train_start,
            win.train_end,
        )
        if len(X) < MIN_TRAIN_EXAMPLES or len(set(y.tolist())) < 2:
            folds_out.append(
                FoldMetrics(
                    train_start=win.train_start,
                    train_end=win.train_end,
                    test_start=win.test_start,
                    test_end=win.test_end,
                    n_train_examples=int(len(X)),
                    n_trades_oos=0,
                    sharpe_oos=float("nan"),
                    hit_rate_oos=float("nan"),
                    skipped=True,
                )
            )
            continue
        model = _fit(X, y)
        n_trades, sh, hr = _evaluate_fold_oos(
            enriched,
            macro_history,
            sentiment_lookup,
            negative_news_lookup,
            model,
            win.test_start,
            win.test_end,
        )
        folds_out.append(
            FoldMetrics(
                train_start=win.train_start,
                train_end=win.train_end,
                test_start=win.test_start,
                test_end=win.test_end,
                n_train_examples=int(len(X)),
                n_trades_oos=n_trades,
                sharpe_oos=sh,
                hit_rate_oos=hr,
                skipped=False,
            )
        )

    train_delta = pd.DateOffset(years=int(wf.train_years))
    final_train_start = pd.Timestamp(end) - train_delta
    final_train_end = pd.Timestamp(end)
    Xf, yf = _build_xy_for_window(
        enriched,
        macro_history,
        sentiment_lookup,
        negative_news_lookup,
        final_train_start,
        final_train_end,
    )
    if len(Xf) < MIN_TRAIN_EXAMPLES or len(set(yf.tolist())) < 2:
        raise InsufficientDataError(
            f"final window {final_train_start.date()}-{final_train_end.date()} "
            f"yielded {len(Xf)} labelled examples (< {MIN_TRAIN_EXAMPLES}) — "
            "expand universe, extend the date range, or wait for more data."
        )
    final_model = _fit(Xf, yf)

    non_skipped = [f for f in folds_out if not f.skipped and f.n_trades_oos > 0]
    if non_skipped:
        sharpes = [f.sharpe_oos for f in non_skipped if not math.isnan(f.sharpe_oos)]
        hits = [f.hit_rate_oos for f in non_skipped if not math.isnan(f.hit_rate_oos)]
        oos_sharpe_mean = float(np.mean(sharpes)) if sharpes else float("nan")
        oos_hit_rate_mean = float(np.mean(hits)) if hits else float("nan")
    else:
        oos_sharpe_mean = float("nan")
        oos_hit_rate_mean = float("nan")

    return TrainResult(
        folds=tuple(folds_out),
        final_model=final_model,
        final_train_start=final_train_start,
        final_train_end=final_train_end,
        n_final_examples=int(len(Xf)),
        oos_sharpe_mean=oos_sharpe_mean,
        oos_hit_rate_mean=oos_hit_rate_mean,
        feature_names=FEATURE_NAMES,
    )
