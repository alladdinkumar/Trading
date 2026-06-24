from __future__ import annotations

import numpy as np
import pandas as pd

from trading.strategy.factors import (
    eligible_set,
    factor_score,
    momentum_12_1,
    realized_vol,
)


def _close_series(values: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range("2020-01-01", periods=len(values))
    return pd.DataFrame({"close": values}, index=idx)


def test_momentum_12_1_is_close_t21_over_close_t252() -> None:
    # 300 bars so as_of has 273+ bars of history through it.
    closes = list(np.linspace(100.0, 400.0, 300))
    df = _close_series(closes)
    as_of = df.index[-1]
    pos = df.index.get_loc(as_of)
    expected = closes[pos - 21] / closes[pos - 252] - 1.0
    assert momentum_12_1(df, as_of) == expected


def test_momentum_12_1_none_when_fewer_than_273_bars() -> None:
    df = _close_series(list(np.linspace(100.0, 200.0, 272)))
    assert momentum_12_1(df, df.index[-1]) is None


def test_realized_vol_constant_growth_is_near_zero() -> None:
    # Constant 1%/bar growth → constant log return → ~0 stdev.
    closes = [100.0 * (1.01 ** i) for i in range(120)]
    df = _close_series(closes)
    vol = realized_vol(df, df.index[-1], window=90)
    assert vol is not None
    assert vol < 1e-9


def test_realized_vol_matches_sample_stdev_of_log_returns() -> None:
    rng = np.random.default_rng(0)
    closes = list(100.0 * np.cumprod(1 + rng.normal(0, 0.02, size=200)))
    df = _close_series(closes)
    as_of = df.index[-1]
    log_ret = np.diff(np.log(df["close"].to_numpy()))
    expected = float(np.std(log_ret[-90:], ddof=1))
    assert realized_vol(df, as_of, window=90) == expected


def test_realized_vol_none_when_insufficient_history() -> None:
    df = _close_series([100.0] * 90)  # need window+1=91 bars
    assert realized_vol(df, df.index[-1], window=90) is None


def _trending_panel() -> dict[str, pd.DataFrame]:
    """3 symbols, 300 bars, differing drift+noise so momentum/vol differ."""
    idx = pd.bdate_range("2020-01-01", periods=300)
    panel: dict[str, pd.DataFrame] = {}
    for i, (drift, vol) in enumerate([(0.30, 0.005), (0.10, 0.02), (0.50, 0.05)]):
        rng = np.random.default_rng(i)
        steps = drift / 300 + rng.normal(0, vol, size=300)
        closes = 100.0 * np.cumprod(1 + steps)
        panel[f"SYM{i}"] = pd.DataFrame({"close": closes}, index=idx)
    return panel


def test_factor_score_is_zero_mean_unit_population_std_composite() -> None:
    panel = _trending_panel()
    as_of = panel["SYM0"].index[-1]
    scores = factor_score(panel, as_of, vol_window=90)
    assert set(scores) == {"SYM0", "SYM1", "SYM2"}
    # Composite is the mean of two z-scores, each population-standardized.
    vals = np.array(list(scores.values()))
    assert abs(float(vals.mean())) < 1e-9


def test_factor_score_drops_symbols_with_insufficient_history() -> None:
    panel = _trending_panel()
    short = pd.DataFrame(
        {"close": [100.0] * 50},
        index=pd.bdate_range("2020-01-01", periods=50),
    )
    panel["SHORT"] = short
    as_of = panel["SYM0"].index[-1]
    scores = factor_score(panel, as_of, vol_window=90)
    assert "SHORT" not in scores


def test_factor_score_rewards_low_vol_and_high_momentum() -> None:
    panel = _trending_panel()
    as_of = panel["SYM0"].index[-1]
    scores = factor_score(panel, as_of, vol_window=90)
    # SYM0 has the lowest vol; its low-vol z-score is the highest of the three.
    vols = {s: realized_vol(df, as_of, window=90) for s, df in panel.items()}
    lowest_vol = min(vols, key=lambda s: vols[s])  # type: ignore[arg-type]
    # The lowest-vol symbol gets a positive low-vol contribution → above-mean.
    assert scores[lowest_vol] > float(np.mean(list(scores.values())))


def test_factor_score_empty_when_fewer_than_two_survive() -> None:
    one = {"ONLY": _trending_panel()["SYM0"]}
    as_of = one["ONLY"].index[-1]
    assert factor_score(one, as_of, vol_window=90) == {}


def test_eligible_set_keeps_top_quantile_count() -> None:
    scores = {f"S{i}": float(i) for i in range(10)}  # S9 highest
    chosen = eligible_set(scores, top_quantile=0.30)  # 10 * 0.30 = 3
    assert chosen == {"S9", "S8", "S7"}


def test_eligible_set_ties_broken_deterministically_by_symbol() -> None:
    scores = {"B": 1.0, "A": 1.0, "C": 0.0}  # A and B tie at the top
    chosen = eligible_set(scores, top_quantile=0.34)  # 3 * 0.34 = 1 → keep 1
    assert chosen == {"A"}  # tie resolved by symbol ascending


def test_eligible_set_empty_input_returns_empty() -> None:
    assert eligible_set({}, top_quantile=0.30) == set()
