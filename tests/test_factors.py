from __future__ import annotations

import numpy as np
import pandas as pd

from trading.strategy.factors import momentum_12_1, realized_vol


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
