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


def test_per_trade_metrics_on_known_returns() -> None:
    from trading.backtest.factor_eval import per_trade_metrics
    from trading.backtest.metrics import sharpe as _sharpe

    rets = [0.06, -0.04, 0.05, -0.03, 0.07]
    m = per_trade_metrics(rets)
    assert m.n == 5
    assert m.hit_rate == 3 / 5
    assert m.profit_factor == (0.06 + 0.05 + 0.07) / (0.04 + 0.03)
    assert m.payoff == ((0.06 + 0.05 + 0.07) / 3) / ((0.04 + 0.03) / 2)
    assert m.sharpe == _sharpe(pd.Series(rets), periods_per_year=12)


def test_per_trade_metrics_empty_is_zeroed() -> None:
    from trading.backtest.factor_eval import per_trade_metrics

    m = per_trade_metrics([])
    assert m.n == 0
    assert m.sharpe == 0.0
    assert m.profit_factor == 0.0


def test_information_coefficient_runs_over_a_window() -> None:
    from trading.backtest.factor_eval import information_coefficient
    from trading.features.technicals import add_indicators

    idx = pd.bdate_range("2020-01-01", periods=320)
    panel = {}
    for i in range(6):
        rng = np.random.default_rng(i)
        closes = 100.0 * np.cumprod(1 + rng.normal(0.0005 * i, 0.015, size=320))
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
        panel[f"SYM{i}"] = add_indicators(raw)
    res = information_coefficient(
        panel, start=idx[273], end=idx[280], vol_window=90, max_days=25, min_names=3
    )
    assert res.n_days >= 1
    assert isinstance(res.mean_ic, float)
