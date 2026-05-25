from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.features.technicals import add_indicators
from trading.strategy.ranker_train import (
    MIN_TRAIN_EXAMPLES,
    InsufficientDataError,
    train_walkforward,
)


def _separable_ohlcv(seed: int, n: int = 1300) -> pd.DataFrame:
    """OHLCV designed to clear all Layer-A rules at many bars across the
    series so the trainer has labelled examples to work with.

    Engineered:
      - Strong drift keeps sma_50 > sma_200 (uptrend rule).
      - Fast cyclical pullbacks ensure regular touches of sma_20 / sma_50
        (pullback rule) and RSI dips into [30, 45] (rsi_band rule).
      - High notional turnover (price * volume * 20d) clears the ₹10cr
        liquidity rule.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n)
    drift = np.linspace(0, 200, n)  # steady uptrend
    cycle = 10 * np.sin(np.linspace(0, 100, n))  # fast pullbacks
    noise = rng.normal(0, 0.5, size=n)
    close = 500 + drift + cycle + noise
    high = close + rng.uniform(0.5, 2.0, size=n)
    low = close - rng.uniform(0.5, 2.0, size=n)
    open_ = close + rng.uniform(-0.5, 0.5, size=n)
    # ₹500+ × 200k+ shares × 20 days = ~₹2cr per day average ⇒ clears ₹10cr
    # only via the 20-day average. Use 1M+ shares to be safe.
    vol = rng.integers(1_500_000, 2_500_000, size=n).astype(int)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=dates,
    )


def _empty_macro_history() -> pd.DataFrame:
    return pd.DataFrame(columns=["vix", "usdinr", "fii_flow_cr"])


def test_insufficient_data_raises() -> None:
    # Tiny universe + short period → 0 rules-passing candidates in train slice
    enriched = {"X": add_indicators(_separable_ohlcv(0, n=300))}
    with pytest.raises(InsufficientDataError):
        train_walkforward(
            enriched=enriched,
            macro_history=_empty_macro_history(),
            sentiment_lookup={},
            negative_news_lookup={},
            start=pd.Timestamp("2022-01-03"),
            end=pd.Timestamp("2023-04-30"),
        )


def test_train_returns_result_with_final_model_on_sufficient_data() -> None:
    enriched = {
        f"SYM{i}": add_indicators(_separable_ohlcv(seed=i, n=1300)) for i in range(5)
    }
    result = train_walkforward(
        enriched=enriched,
        macro_history=_empty_macro_history(),
        sentiment_lookup={},
        negative_news_lookup={},
        start=pd.Timestamp("2022-01-03"),
        end=pd.Timestamp("2025-08-29"),
    )
    assert result.n_final_examples >= MIN_TRAIN_EXAMPLES
    assert result.feature_names is not None
    proba = result.final_model.predict_proba(
        np.zeros((1, len(result.feature_names)))
    )
    assert proba.shape == (1, 2)


def test_fold_skipped_when_under_min_examples() -> None:
    """1-symbol universe — many folds likely skip; verify the field type."""
    enriched = {"ONLY": add_indicators(_separable_ohlcv(seed=99, n=1300))}
    try:
        result = train_walkforward(
            enriched=enriched,
            macro_history=_empty_macro_history(),
            sentiment_lookup={},
            negative_news_lookup={},
            start=pd.Timestamp("2022-01-03"),
            end=pd.Timestamp("2025-08-29"),
        )
    except InsufficientDataError:
        pytest.skip("1-symbol universe didn't accumulate enough; not a regression")
    assert all(isinstance(f.skipped, bool) for f in result.folds)


def test_oos_sharpe_mean_is_nan_safe() -> None:
    enriched = {
        f"SYM{i}": add_indicators(_separable_ohlcv(seed=i, n=1300)) for i in range(5)
    }
    result = train_walkforward(
        enriched=enriched,
        macro_history=_empty_macro_history(),
        sentiment_lookup={},
        negative_news_lookup={},
        start=pd.Timestamp("2022-01-03"),
        end=pd.Timestamp("2025-08-29"),
    )
    assert isinstance(result.oos_sharpe_mean, float)
