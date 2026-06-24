from __future__ import annotations

import numpy as np
import pandas as pd

from trading.strategy.factors import momentum_12_1


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
