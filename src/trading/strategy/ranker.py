"""Phase 16 inference — score rules-passing candidates + top-K filter.

Cold-start: when no active model is registered, every candidate is returned
with `selected=True, ml_score=None` so pre_open behaviour is unchanged.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

from trading.features.technicals import add_indicators
from trading.store.macro_store import get_macro_snapshot
from trading.store.model_registry import (
    ActiveModel,
    RegistryFeatureMismatch,
)
from trading.store.model_registry import active as load_active
from trading.store.news_store import get_sentiment_daily, negative_news_count_7d
from trading.store.ohlcv import read_ohlcv
from trading.store.repo import Conviction
from trading.strategy.ranker_features import (
    FEATURE_NAMES,
    LiveContext,
    build_feature_row,
)
from trading.strategy.rules import Candidate

if TYPE_CHECKING:
    from trading.backtest.engine import BacktestConfig, Signal
    from trading.config import Paths
    from trading.strategy.rules import ScanContext


@dataclass(frozen=True)
class ScoredCandidate:
    """A rules-passing candidate after the ranker stage."""

    candidate: Candidate
    ml_score: float | None
    selected: bool


# ml_score is a LightGBM `predict_proba` in [0, 1] (calibrated P(win)). These
# bands turn it into the `signals.conviction` enum (F-038). Boundaries inclusive
# at the lower edge. `None` (cold-start — no active model) stays `None` rather
# than fabricating a LOW, so the dashboard shows "—" honestly.
CONVICTION_HIGH_MIN = 0.60
CONVICTION_MEDIUM_MIN = 0.50


def conviction_from_score(ml_score: float | None) -> Conviction | None:
    """Map an ml_score to a HIGH/MEDIUM/LOW conviction band (or None)."""
    if ml_score is None:
        return None
    if ml_score >= CONVICTION_HIGH_MIN:
        return "HIGH"
    if ml_score >= CONVICTION_MEDIUM_MIN:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Module-internal helpers
# ---------------------------------------------------------------------------


def _cold_start(candidates: list[Candidate]) -> list[ScoredCandidate]:
    return [ScoredCandidate(c, None, True) for c in candidates]


def _load_active_or_none(paths: Paths) -> ActiveModel | None:
    try:
        am = load_active(paths)
    except Exception as e:  # corrupt registry, IO error
        logger.warning("ranker: failed to load active model — cold start ({})", e)
        return None
    if am is None:
        return None
    if tuple(am.feature_names) != FEATURE_NAMES:
        logger.warning(
            "ranker: active model feature_names mismatch — cold start "
            "(model={} != current={})",
            am.feature_names,
            FEATURE_NAMES,
        )
        return None
    return am


def _macro_history_df(
    conn: sqlite3.Connection, as_of: date, lookback_days: int = 14
) -> pd.DataFrame:
    """Pull recent macro_snapshot rows as a DataFrame indexed by ISO date."""
    start = (as_of - timedelta(days=lookback_days)).isoformat()
    end = as_of.isoformat()
    rows = conn.execute(
        "SELECT date, vix, usdinr, fii_flow_cr FROM macro_snapshot "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["vix", "usdinr", "fii_flow_cr"])
    return pd.DataFrame(
        {
            "vix": [r["vix"] for r in rows],
            "usdinr": [r["usdinr"] for r in rows],
            "fii_flow_cr": [r["fii_flow_cr"] for r in rows],
        },
        index=[r["date"] for r in rows],
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def score_and_filter(
    candidates: list[Candidate],
    paths: Paths,
    conn: sqlite3.Connection,
    as_of: date,
    *,
    k: int = 5,
) -> list[ScoredCandidate]:
    """Score every rules-passing candidate and mark top-K as selected.

    Cold-start path (no active model, missing .pkl, feature-name mismatch,
    or any IO error) returns ScoredCandidate(c, None, True) for every input.
    """
    if not candidates:
        return []
    am = _load_active_or_none(paths)
    if am is None:
        return _cold_start(candidates)

    macro_snap = get_macro_snapshot(conn, as_of.isoformat())
    macro_hist = _macro_history_df(conn, as_of)
    as_of_ts = pd.Timestamp(as_of)

    rows: list[dict[str, float]] = []
    cand_index: list[Candidate] = []
    for cand in candidates:
        try:
            raw = read_ohlcv(cand.symbol, paths, end=as_of)
        except FileNotFoundError:
            rows.append(dict.fromkeys(FEATURE_NAMES, math.nan))
            cand_index.append(cand)
            continue
        as_of_actual = raw.index[-1] if as_of_ts not in raw.index else as_of_ts
        enriched = add_indicators(raw)
        sent = get_sentiment_daily(conn, as_of.isoformat(), cand.symbol)
        neg7 = negative_news_count_7d(conn, cand.symbol, as_of)
        ctx = LiveContext(
            macro=macro_snap,
            sentiment=sent,
            macro_history=macro_hist,
            negative_news_count_7d=neg7,
        )
        rows.append(build_feature_row(enriched, as_of_actual, ctx))
        cand_index.append(cand)

    X = pd.DataFrame(rows, columns=list(FEATURE_NAMES)).astype(float)
    proba = am.model.predict_proba(X.values)[:, 1]
    ranked_idx = sorted(range(len(proba)), key=lambda i: -proba[i])
    selected = set(ranked_idx[:k])
    return [
        ScoredCandidate(
            candidate=cand_index[i],
            ml_score=float(proba[i]),
            selected=i in selected,
        )
        for i in range(len(cand_index))
    ]


class RankerSignalProvider:
    """Engine-compatible signal_provider used inside walk-forward test folds.

    Two entry paths:
      __call__(d, enriched, ctx, config) — engine API.
      score_signals(signals, enriched, signal_date) — testable helper.
    """

    def __init__(
        self,
        model: object,
        feature_names: tuple[str, ...],
        paths: Paths,
        conn: sqlite3.Connection,
        top_k: int = 5,
    ) -> None:
        if tuple(feature_names) != FEATURE_NAMES:
            raise RegistryFeatureMismatch(
                f"model feature_names {feature_names} != current {FEATURE_NAMES}"
            )
        self._model = model
        self._paths = paths
        self._conn = conn
        self._top_k = top_k

    def score_signals(
        self,
        signals: list[Signal],
        enriched: Mapping[str, pd.DataFrame],
        signal_date: pd.Timestamp,
    ) -> list[Signal]:
        if not signals:
            return []
        macro_snap = get_macro_snapshot(self._conn, signal_date.strftime("%Y-%m-%d"))
        macro_hist = _macro_history_df(
            self._conn, signal_date.date(), lookback_days=14
        )
        rows: list[dict[str, float]] = []
        for sig in signals:
            df = enriched.get(sig.symbol)
            if df is None or signal_date not in df.index:
                rows.append(dict.fromkeys(FEATURE_NAMES, math.nan))
                continue
            sent = get_sentiment_daily(
                self._conn, signal_date.strftime("%Y-%m-%d"), sig.symbol
            )
            neg7 = negative_news_count_7d(
                self._conn, sig.symbol, signal_date.date()
            )
            ctx = LiveContext(
                macro=macro_snap,
                sentiment=sent,
                macro_history=macro_hist,
                negative_news_count_7d=neg7,
            )
            rows.append(build_feature_row(df, signal_date, ctx))

        X = pd.DataFrame(rows, columns=list(FEATURE_NAMES)).astype(float)
        proba = self._model.predict_proba(X.values)[:, 1]  # type: ignore[attr-defined]
        order = sorted(range(len(proba)), key=lambda i: -proba[i])
        kept = order[: self._top_k]
        return [signals[i] for i in kept]

    def __call__(
        self,
        d: pd.Timestamp,
        enriched: Mapping[str, pd.DataFrame],
        ctx: ScanContext,
        config: BacktestConfig,
    ) -> list[Signal]:
        from trading.backtest.engine import rule_signal_provider

        base = rule_signal_provider(d, enriched, ctx, config)
        return self.score_signals(base, enriched, d)
