from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trading.backtest.factor_eval import (
    aggregate_ic,
    forward_returns,
    spearman_ic,
)


def test_spearman_ic_perfect_rank_is_one() -> None:
    scores = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.0, "E": -1.0}
    fwd = {"A": 0.10, "B": 0.08, "C": 0.05, "D": 0.01, "E": -0.02}
    assert spearman_ic(scores, fwd, min_names=5) == pytest.approx(1.0)


def test_spearman_ic_inverted_rank_is_minus_one() -> None:
    scores = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.0, "E": -1.0}
    fwd = {"A": -0.10, "B": -0.08, "C": -0.05, "D": -0.01, "E": 0.02}
    assert spearman_ic(scores, fwd, min_names=5) == pytest.approx(-1.0)


def test_spearman_ic_none_when_too_few_overlapping_names() -> None:
    assert spearman_ic({"A": 1.0}, {"A": 0.1}, min_names=5) is None


def test_aggregate_ic_computes_tstat_and_hit_rate() -> None:
    ics = [0.1, 0.2, -0.05, 0.15]
    res = aggregate_ic(ics)
    assert res.n_days == 4
    assert res.mean_ic == np.mean(ics)
    expected_t = float(np.mean(ics)) / (float(np.std(ics, ddof=1)) / math.sqrt(4))
    assert res.ic_t_stat == expected_t
    assert res.hit_rate_positive_days == 0.75


def test_forward_returns_matches_realized_return() -> None:
    from trading.features.technicals import add_indicators
    from trading.ranking.ranker_labels import realized_return

    rng = np.random.default_rng(1)
    closes = 100.0 * np.cumprod(1 + rng.normal(0.001, 0.02, size=320))
    idx = pd.bdate_range("2020-01-01", periods=320)
    raw = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": [1_000_000] * 320,
        },
        index=idx,
    )
    df = add_indicators(raw)
    panel = {"SYM": df}
    as_of = df.index[250]
    fwd = forward_returns(panel, as_of, max_days=25)
    assert fwd["SYM"] == realized_return(df, as_of, max_days=25)
