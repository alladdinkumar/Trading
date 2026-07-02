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
from typing import TYPE_CHECKING, Any, NamedTuple

import lightgbm as lgb
import numpy as np
import pandas as pd

from trading.backtest.forward_return import LABEL_HORIZON_DAYS
from trading.backtest.metrics import sharpe
from trading.backtest.walkforward import WalkForwardConfig, windows
from trading.ranking.ranker_features import (
    FEATURE_NAMES,
    LiveContext,
    build_feature_row,
)
from trading.ranking.ranker_labels import realized_return
from trading.store.news_store import SentimentDailyRow
from trading.strategy.rules import MIN_HISTORY_BARS, ScanContext, evaluate_symbol

if TYPE_CHECKING:
    from trading.backtest.engine import BacktestConfig

MIN_TRAIN_EXAMPLES = 30
# Mirror production inference: `score_and_filter` / `RankerSignalProvider` keep the
# model's top-K candidates per as_of date. OOS evaluation must use the same cap so
# the fold Sharpe measures what the live ranker would actually trade, not the
# Layer-A base rate of every rules-passing candidate (F-044).
OOS_TOP_K = 5
# Minimum trades a probability threshold must admit before it's a trustworthy
# calibration point — fewer than this and the Sharpe is noise (F-044).
MIN_THRESHOLD_TRADES = 20


class _FoldXY(NamedTuple):
    """Training matrix plus the economic outcome of each example.

    `w` is the per-sample weight (∝ |return|) so the classifier optimises
    expectancy rather than hit rate; `rets` is the signed realised return used
    to calibrate the act/pass threshold (F-044)."""

    X: pd.DataFrame
    y: np.ndarray[Any, Any]
    w: np.ndarray[Any, Any]
    rets: np.ndarray[Any, Any]


def _new_lgbm() -> lgb.LGBMClassifier:
    """Construct a fresh LightGBM classifier with Phase-16 small-data hyperparameters."""
    return lgb.LGBMClassifier(
        objective="binary",
        metric="binary_logloss",
        num_leaves=15,
        min_data_in_leaf=10,
        learning_rate=0.05,
        n_estimators=200,
        is_unbalance=True,
        feature_pre_filter=False,
        verbose=-1,
        random_state=42,
    )


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
    # Calibrated act/pass probability threshold for the final model (F-044).
    # None when no threshold admitted enough trades — production falls back to
    # top-K-only selection.
    threshold: float | None = None
    # Pooled (trade-weighted) OOS statistic — the honest promotion basis that
    # replaces the noise-prone mean of per-fold Sharpes (F-046). Defaulted so
    # existing TrainResult construction sites stay valid.
    oos_sharpe_pooled: float = float("nan")
    oos_hit_pooled: float = float("nan")
    n_oos_total: int = 0
    n_folds_positive: int = 0
    n_folds_total: int = 0


def _embargo_boundary(train_end: pd.Timestamp) -> pd.Timestamp:
    """The latest signal_date a training example may have — `LABEL_HORIZON_DAYS`
    trading days before `train_end`.

    A training label is the realised outcome of a trade held up to
    `LABEL_HORIZON_DAYS` forward bars; `windows()` sets `test_start ==
    train_end` with zero gap, so any training signal dated within that horizon
    of `train_end` has its label resolved on bars inside the fold's own OOS
    test window (F-055). Uses `numpy.busday_offset` (Mon–Fri trading-day
    arithmetic, consistent with the rest of the codebase's trading-day math,
    e.g. `trading.paper.journal`) rather than the exact bars of any one
    symbol's index, since the boundary must be a single date shared across the
    whole (multi-symbol) training set. `roll="forward"` rolls `train_end`
    itself onto a business day first when it isn't one, which only widens
    (never narrows) the embargo.
    """
    boundary = np.busday_offset(
        train_end.date().isoformat(), -LABEL_HORIZON_DAYS, roll="forward"
    )
    return pd.Timestamp(boundary)


def _train_signal_mask(
    index: pd.Index, train_start: pd.Timestamp, train_end: pd.Timestamp
) -> np.ndarray[Any, Any]:
    """Boolean mask selecting `index` entries eligible as training signals for
    a `[train_start, train_end)` fold, with the F-055 embargo applied on the
    upper bound (see `_embargo_boundary`)."""
    return (index >= train_start) & (index < _embargo_boundary(train_end))


def _build_xy_for_window(
    enriched: Mapping[str, pd.DataFrame],
    macro_history: pd.DataFrame,
    sentiment_lookup: Mapping[tuple[str, str], SentimentDailyRow],
    negative_news_lookup: Mapping[tuple[str, str], int],
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
) -> _FoldXY:
    """Walk each (symbol, date) in [train_start, train_end), evaluate Layer A,
    and build (X, y, w, rets) for every all-pass candidate with a resolvable
    forward window. `w` (∝ |return|) weights training toward economically large
    outcomes; `rets` is the signed realised return (F-044).

    The upper bound is embargoed `LABEL_HORIZON_DAYS` trading days short of
    `train_end` (F-055): candidates dated later than that would have their
    label's outcome window reach into this fold's own OOS test period, which
    starts at `test_start == train_end` with zero gap.
    """
    feat_rows: list[dict[str, float]] = []
    rets: list[float] = []
    for sym, df in enriched.items():
        if len(df) < MIN_HISTORY_BARS:
            continue
        mask = _train_signal_mask(df.index, train_start, train_end)
        for sd in df.index[mask]:
            sub = df.loc[:sd]
            if len(sub) < MIN_HISTORY_BARS:
                continue
            cand = evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date()))
            if not cand.all_passed:
                continue
            ret = realized_return(df, sd)
            if ret is None:
                continue
            sd_iso = sd.strftime("%Y-%m-%d")
            ctx = LiveContext(
                macro=None,
                sentiment=sentiment_lookup.get((sd_iso, sym)),
                macro_history=macro_history,
                negative_news_count_7d=negative_news_lookup.get((sd_iso, sym)),
            )
            feat_rows.append(build_feature_row(df, sd, ctx))
            rets.append(ret)
    X = pd.DataFrame(feat_rows, columns=list(FEATURE_NAMES)).astype(float)
    ret_arr = np.array(rets, dtype=float)
    y = (ret_arr > 0).astype(int)
    w = np.abs(ret_arr)
    return _FoldXY(X=X, y=y, w=w, rets=ret_arr)


def _fit(
    X: pd.DataFrame,
    y: np.ndarray[Any, Any],
    sample_weight: np.ndarray[Any, Any] | None = None,
) -> lgb.LGBMClassifier:
    model = _new_lgbm()
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
                sample_weight=None if sample_weight is None else sample_weight[train_idx],
                eval_set=[(X.values[val_idx], y[val_idx])],
                callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
            )
            return model
    model.fit(X.values, y, sample_weight=sample_weight)
    return model


def calibrate_threshold(
    proba: np.ndarray[Any, Any],
    rets: np.ndarray[Any, Any],
    *,
    min_trades: int = MIN_THRESHOLD_TRADES,
) -> float | None:
    """Pick the act/pass probability threshold τ that maximises the realised-return
    Sharpe of admitted trades (`proba ≥ τ`), requiring ≥ `min_trades` admitted.

    This is the meta-labeling gate: it lets the model *decline* low-conviction
    setups (trade nothing on a weak day) instead of being forced to trade every
    rules-passing candidate. Returns None when no threshold admits enough trades —
    the caller then falls back to top-K-only selection (F-044).
    """
    p = np.asarray(proba, dtype=float)
    r = np.asarray(rets, dtype=float)
    if len(p) == 0:
        return None
    best_tau: float | None = None
    best_sh = -math.inf
    for tau in np.unique(p):
        kept = r[p >= tau]
        if len(kept) < min_trades:
            continue
        sh = sharpe(pd.Series(kept), periods_per_year=12)
        if sh > best_sh:
            best_sh = sh
            best_tau = float(tau)
    return best_tau


def _evaluate_fold_oos(
    enriched: Mapping[str, pd.DataFrame],
    macro_history: pd.DataFrame,
    sentiment_lookup: Mapping[tuple[str, str], SentimentDailyRow],
    negative_news_lookup: Mapping[tuple[str, str], int],
    model: lgb.LGBMClassifier,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
    *,
    top_k: int = OOS_TOP_K,
    threshold: float | None = None,
) -> tuple[int, float, float, list[float]]:
    """Replay the live ranker over [test_start, test_end) and measure its OOS edge.

    For every rules-passing candidate in the window we build the same feature row
    used in production, score it with `model`, then — per signal date — select the
    model's picks: those with `predict_proba ≥ threshold` (if given; lets the
    model *abstain*, the meta-labeling act/pass gate) capped at the top `top_k`
    (mirroring `score_and_filter`). The Sharpe + hit rate are computed over the
    **realised net returns** of the selected trades (Phase 6 exit replay) — a
    magnitude-aware metric, not `sharpe(label*2-1)` which is a pure transform of
    hit rate and blind to the strategy's payoff asymmetry (F-044). This is the
    basis F-043 gates promotion on. Returns (n_trades, sharpe, hit_rate).
    """
    feat_rows: list[dict[str, float]] = []
    meta: list[tuple[pd.Timestamp, float]] = []  # (signal_date, realised_return)
    for sym, df in enriched.items():
        mask = (df.index >= test_start) & (df.index < test_end)
        for sd in df.index[mask]:
            sub = df.loc[:sd]
            if len(sub) < MIN_HISTORY_BARS:
                continue
            cand = evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date()))
            if not cand.all_passed:
                continue
            ret = realized_return(df, sd)
            if ret is None:
                continue
            sd_iso = sd.strftime("%Y-%m-%d")
            ctx = LiveContext(
                macro=None,
                sentiment=sentiment_lookup.get((sd_iso, sym)),
                macro_history=macro_history,
                negative_news_count_7d=negative_news_lookup.get((sd_iso, sym)),
            )
            feat_rows.append(build_feature_row(df, sd, ctx))
            meta.append((sd, ret))

    if not feat_rows:
        return 0, float("nan"), float("nan"), []

    X = pd.DataFrame(feat_rows, columns=list(FEATURE_NAMES)).astype(float)
    proba = model.predict_proba(X.values)[:, 1]

    # Group (proba, return) by signal date; per date, drop sub-threshold picks
    # then keep the model's top-K.
    by_date: dict[pd.Timestamp, list[tuple[float, float]]] = {}
    for (sd, ret), p in zip(meta, proba.tolist(), strict=True):
        by_date.setdefault(sd, []).append((float(p), ret))

    realised: list[float] = []
    for items in by_date.values():
        items.sort(key=lambda t: -t[0])
        picks = items if threshold is None else [it for it in items if it[0] >= threshold]
        realised.extend(ret for _p, ret in picks[:top_k])

    if not realised:
        return 0, float("nan"), float("nan"), []
    returns = pd.Series(realised, dtype=float)
    # Fold-level Sharpe over realised per-trade returns. The absolute scale isn't
    # load-bearing (sign vs the F-043 floor is); periods_per_year keeps continuity.
    return (
        len(realised),
        float(sharpe(returns, periods_per_year=12)),
        float((returns > 0).mean()),
        realised,
    )


def _pool_oos(
    fold_returns: list[list[float]],
) -> tuple[float, float, int, int, int]:
    """Pool realised per-trade returns across folds into one honest statistic.

    Returns (pooled_sharpe, pooled_hit, n_oos_total, n_folds_positive,
    n_folds_total). `n_folds_total` counts non-empty folds; `n_folds_positive`
    counts folds whose realised mean is > 0 (the breadth check). Trade-weighted
    pooling stops a lucky small fold from inflating a mean-of-fold-Sharpes
    (F-046)."""
    non_empty = [f for f in fold_returns if f]
    pooled = [r for f in non_empty for r in f]
    if not pooled:
        return float("nan"), float("nan"), 0, 0, len(non_empty)
    s = pd.Series(pooled, dtype=float)
    n_folds_positive = sum(1 for f in non_empty if float(np.mean(f)) > 0.0)
    return (
        float(sharpe(s, periods_per_year=12)),
        float((s > 0).mean()),
        len(pooled),
        n_folds_positive,
        len(non_empty),
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
    bt_cfg: BacktestConfig | None = None,
) -> TrainResult:
    wf = wf_cfg or WalkForwardConfig()
    folds_out: list[FoldMetrics] = []
    fold_returns: list[list[float]] = []
    for win in windows(start, end, wf):
        fx = _build_xy_for_window(
            enriched,
            macro_history,
            sentiment_lookup,
            negative_news_lookup,
            win.train_start,
            win.train_end,
        )
        if len(fx.X) < MIN_TRAIN_EXAMPLES or len(set(fx.y.tolist())) < 2:
            folds_out.append(
                FoldMetrics(
                    train_start=win.train_start,
                    train_end=win.train_end,
                    test_start=win.test_start,
                    test_end=win.test_end,
                    n_train_examples=len(fx.X),
                    n_trades_oos=0,
                    sharpe_oos=float("nan"),
                    hit_rate_oos=float("nan"),
                    skipped=True,
                )
            )
            continue
        model = _fit(fx.X, fx.y, fx.w)
        # Calibrate the act/pass threshold on the in-sample fold, then judge it
        # out-of-sample — no look-ahead.
        tau = calibrate_threshold(model.predict_proba(fx.X.values)[:, 1], fx.rets)
        n_trades, sh, hr, realised = _evaluate_fold_oos(
            enriched,
            macro_history,
            sentiment_lookup,
            negative_news_lookup,
            model,
            win.test_start,
            win.test_end,
            threshold=tau,
        )
        fold_returns.append(realised)
        folds_out.append(
            FoldMetrics(
                train_start=win.train_start,
                train_end=win.train_end,
                test_start=win.test_start,
                test_end=win.test_end,
                n_train_examples=len(fx.X),
                n_trades_oos=n_trades,
                sharpe_oos=sh,
                hit_rate_oos=hr,
                skipped=False,
            )
        )

    train_delta = pd.DateOffset(years=int(wf.train_years))
    final_train_start = pd.Timestamp(end) - train_delta
    final_train_end = pd.Timestamp(end)
    fxf = _build_xy_for_window(
        enriched,
        macro_history,
        sentiment_lookup,
        negative_news_lookup,
        final_train_start,
        final_train_end,
    )
    if len(fxf.X) < MIN_TRAIN_EXAMPLES or len(set(fxf.y.tolist())) < 2:
        raise InsufficientDataError(
            f"final window {final_train_start.date()}-{final_train_end.date()} "
            f"yielded {len(fxf.X)} labelled examples (< {MIN_TRAIN_EXAMPLES}) — "
            "expand universe, extend the date range, or wait for more data."
        )
    final_model = _fit(fxf.X, fxf.y, fxf.w)
    final_threshold = calibrate_threshold(
        final_model.predict_proba(fxf.X.values)[:, 1], fxf.rets
    )

    non_skipped = [f for f in folds_out if not f.skipped and f.n_trades_oos > 0]
    if non_skipped:
        sharpes = [f.sharpe_oos for f in non_skipped if not math.isnan(f.sharpe_oos)]
        hits = [f.hit_rate_oos for f in non_skipped if not math.isnan(f.hit_rate_oos)]
        oos_sharpe_mean = float(np.mean(sharpes)) if sharpes else float("nan")
        oos_hit_rate_mean = float(np.mean(hits)) if hits else float("nan")
    else:
        oos_sharpe_mean = float("nan")
        oos_hit_rate_mean = float("nan")

    pooled_sharpe, pooled_hit, n_oos_total, n_folds_pos, n_folds_tot = _pool_oos(
        fold_returns
    )

    return TrainResult(
        folds=tuple(folds_out),
        final_model=final_model,
        final_train_start=final_train_start,
        final_train_end=final_train_end,
        n_final_examples=len(fxf.X),
        oos_sharpe_mean=oos_sharpe_mean,
        oos_hit_rate_mean=oos_hit_rate_mean,
        feature_names=FEATURE_NAMES,
        threshold=final_threshold,
        oos_sharpe_pooled=pooled_sharpe,
        oos_hit_pooled=pooled_hit,
        n_oos_total=n_oos_total,
        n_folds_positive=n_folds_pos,
        n_folds_total=n_folds_tot,
    )
