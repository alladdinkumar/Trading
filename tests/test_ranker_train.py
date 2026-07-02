from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.backtest.forward_return import LABEL_HORIZON_DAYS
from trading.features.technicals import add_indicators
from trading.ranking.ranker_train import (
    MIN_TRAIN_EXAMPLES,
    InsufficientDataError,
    _build_xy_for_window,
    _embargo_boundary,
    _evaluate_fold_oos,
    _train_signal_mask,
    calibrate_threshold,
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


class _SpyModel:
    """Records predict_proba calls; returns a constant P(win)=0.9 column."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def predict_proba(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        self.calls.append(len(X))
        n = len(X)
        return np.column_stack([np.full(n, 0.1), np.full(n, 0.9)])


def test_oos_eval_actually_uses_the_model() -> None:
    """Regression: the fold OOS evaluation must score candidates with the
    trained model, not just realise the label of every rules-passing
    candidate. If the model is ignored, the OOS Sharpe measures the Layer-A
    base rate — making the F-043 promotion gate blind to model skill, so no
    model is ever promoted and signals.ml_score/conviction stay NULL.
    """
    enriched = {
        f"SYM{i}": add_indicators(_separable_ohlcv(seed=i, n=1300)) for i in range(5)
    }
    spy = _SpyModel()
    n_trades, _sharpe, _hit, _realised = _evaluate_fold_oos(
        enriched=enriched,
        macro_history=_empty_macro_history(),
        sentiment_lookup={},
        negative_news_lookup={},
        model=spy,
        test_start=pd.Timestamp("2024-06-03"),
        test_end=pd.Timestamp("2024-12-02"),
    )
    assert n_trades > 0, "test window had no rules-passing candidates to evaluate"
    assert spy.calls, "model.predict_proba was never called — OOS eval ignores the model"


def _gather_oos_returns(enriched, test_start, test_end):  # type: ignore[no-untyped-def]
    """Independently replay realized returns of every rules-passing candidate
    in the window — the ground truth the OOS Sharpe should be computed over."""
    from trading.ranking.ranker_labels import realized_return
    from trading.strategy.rules import MIN_HISTORY_BARS, ScanContext, evaluate_symbol

    rets: list[float] = []
    for sym, df in enriched.items():
        mask = (df.index >= test_start) & (df.index < test_end)
        for sd in df.index[mask]:
            sub = df.loc[:sd]
            if len(sub) < MIN_HISTORY_BARS:
                continue
            if not evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date())).all_passed:
                continue
            r = realized_return(df, sd)
            if r is not None:
                rets.append(r)
    return rets


def test_oos_sharpe_is_real_return_sharpe_not_hitrate_proxy() -> None:
    """Tier 1: the fold OOS Sharpe must be the Sharpe of realized net returns of
    the selected trades — magnitude-aware — not `sharpe(label*2-1)`, which is a
    pure transform of hit rate and blind to the strategy's payoff asymmetry."""
    import math

    from trading.backtest.metrics import sharpe as _sharpe

    enriched = {
        f"SYM{i}": add_indicators(_separable_ohlcv(seed=i, n=1300)) for i in range(5)
    }
    ts, te = pd.Timestamp("2024-06-03"), pd.Timestamp("2024-12-02")
    rets = _gather_oos_returns(enriched, ts, te)
    assert rets, "no rules-passing candidates in window"

    expected = _sharpe(pd.Series(rets), periods_per_year=12)
    # top_k huge + constant proba ⇒ every candidate selected, so the function's
    # realised set equals `rets`.
    n_trades, sh, _hit, _realised = _evaluate_fold_oos(
        enriched=enriched,
        macro_history=_empty_macro_history(),
        sentiment_lookup={},
        negative_news_lookup={},
        model=_SpyModel(),
        test_start=ts,
        test_end=te,
        top_k=10**6,
    )
    assert n_trades == len(rets)
    assert sh == pytest.approx(expected)

    # And it must NOT collapse to the hit-rate proxy.
    p = sum(1 for r in rets if r > 0) / len(rets)
    proxy = (2 * p - 1) / (2 * math.sqrt(p * (1 - p))) * math.sqrt(12)
    assert sh != pytest.approx(proxy)


def test_build_xy_exposes_magnitude_weights_and_signed_returns() -> None:
    """Tier 2a: training examples carry `sample_weight ∝ |return|` so the model
    optimises expectancy, not hit rate; signed returns are exposed for threshold
    calibration."""
    enriched = {
        f"SYM{i}": add_indicators(_separable_ohlcv(seed=i, n=1300)) for i in range(5)
    }
    fx = _build_xy_for_window(
        enriched,
        _empty_macro_history(),
        {},
        {},
        pd.Timestamp("2022-06-01"),
        pd.Timestamp("2024-06-03"),
    )
    n = len(fx.X)
    assert n >= MIN_TRAIN_EXAMPLES
    assert len(fx.y) == n and len(fx.w) == n and len(fx.rets) == n
    assert (fx.w >= 0).all()
    assert fx.w.std() > 0, "weights are constant — magnitude not encoded"
    # weight is |return|; label is the sign of the same return
    assert fx.w == pytest.approx(np.abs(fx.rets))
    assert ((fx.rets > 0).astype(int) == fx.y).all()


def test_embargo_boundary_is_label_horizon_trading_days_before_train_end() -> None:
    """F-055: the embargo boundary must sit exactly LABEL_HORIZON_DAYS trading
    days before train_end, so a training signal 10 trading days before
    train_end (well inside the 25-day label horizon) is excluded, while one
    40 trading days before (well outside it) is included."""
    assert 10 < LABEL_HORIZON_DAYS < 40, "fixture offsets must straddle the label horizon"
    train_end = pd.Timestamp("2024-06-03")  # a Monday
    index = pd.bdate_range("2022-01-03", "2024-08-01")
    boundary_pos = index.get_loc(train_end)

    near_boundary = index[boundary_pos - 10]  # 10 trading days before train_end
    far_before = index[boundary_pos - 40]  # 40 trading days before train_end

    boundary = _embargo_boundary(train_end)
    assert near_boundary >= boundary, "10-trading-day-old signal must fall inside the embargo"
    assert far_before < boundary, "40-trading-day-old signal must fall outside the embargo"

    mask = _train_signal_mask(index, index[0], train_end)
    assert not mask[boundary_pos - 10], "signal 10 trading days before train_end must be excluded"
    assert mask[boundary_pos - 40], "signal 40 trading days before train_end must still be included"


def test_build_xy_embargoes_training_signals_near_train_end() -> None:
    """F-055: `_build_xy_for_window` must not include training signals whose
    label outcome window (up to LABEL_HORIZON_DAYS forward bars) reaches into
    the immediately-following OOS test fold. Replaying the same rules-pass +
    realized_return logic but truncated at the embargo boundary (instead of
    train_end) must yield the same example count `_build_xy_for_window`
    produces — proving the embargo is actually wired into training, not just
    available as a helper."""
    from trading.ranking.ranker_labels import realized_return
    from trading.strategy.rules import MIN_HISTORY_BARS, ScanContext, evaluate_symbol

    enriched = {
        f"SYM{i}": add_indicators(_separable_ohlcv(seed=i, n=1300)) for i in range(5)
    }
    train_start = pd.Timestamp("2022-06-01")
    train_end = pd.Timestamp("2024-06-03")

    def _count(upper_bound: pd.Timestamp) -> int:
        n = 0
        for sym, df in enriched.items():
            mask = (df.index >= train_start) & (df.index < upper_bound)
            for sd in df.index[mask]:
                sub = df.loc[:sd]
                if len(sub) < MIN_HISTORY_BARS:
                    continue
                if not evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date())).all_passed:
                    continue
                if realized_return(df, sd) is not None:
                    n += 1
        return n

    embargoed_count = _count(_embargo_boundary(train_end))
    unembargoed_count = _count(train_end)
    assert embargoed_count < unembargoed_count, (
        "fixture must have at least one rules-passing, resolvable candidate "
        "inside the embargo window for this test to be meaningful"
    )

    fx = _build_xy_for_window(
        enriched, _empty_macro_history(), {}, {}, train_start, train_end
    )
    assert len(fx.X) == embargoed_count


def test_calibrate_threshold_excludes_losers() -> None:
    """Tier 2b: given probabilities that rank winners above losers, the calibrated
    act/pass threshold should admit the winners and lift the realised Sharpe over
    trading everything."""
    from trading.backtest.metrics import sharpe as _sharpe

    # 5 fat winners with high proba, 5 losers with low proba.
    proba = np.array([0.9, 0.85, 0.8, 0.75, 0.7, 0.4, 0.35, 0.3, 0.25, 0.2])
    rets = np.array([0.06, 0.05, 0.07, 0.04, 0.05, -0.04, -0.03, -0.05, -0.04, -0.03])
    tau = calibrate_threshold(proba, rets, min_trades=3)
    assert tau is not None
    kept = rets[proba >= tau]
    assert len(kept) >= 3
    assert (kept > 0).mean() > (rets > 0).mean()
    assert _sharpe(pd.Series(kept), periods_per_year=12) > _sharpe(
        pd.Series(rets), periods_per_year=12
    )


def test_pooled_oos_not_inflated_by_one_lucky_fold() -> None:
    """One lucky small fold must not drag the pooled stat positive when the
    rest of the realised trades are losers. Pooling is trade-weighted, so a
    3-trade winner can't outvote 30 losers the way a mean-of-folds can."""
    from trading.ranking.ranker_train import _pool_oos

    # fold A: 3 tight winners (a huge per-fold Sharpe → would dominate a
    # mean-of-fold-Sharpes); fold B: 30 losers that outweigh them in aggregate.
    fold_returns = [[0.20, 0.22, 0.24], [-0.05] * 30]
    pooled_sharpe, pooled_hit, n_total, n_pos, n_folds = _pool_oos(fold_returns)
    assert n_total == 33
    assert n_folds == 2
    assert n_pos == 1            # only fold A had a positive mean
    assert pooled_hit == pytest.approx(3 / 33)
    assert pooled_sharpe < 0.0   # trade-weighted: the 30 losers dominate the pool

    # Contrast: the naive mean of per-fold Sharpes would be strongly POSITIVE
    # (fold A's Sharpe is enormous), the exact F-046 inflation the pool fixes.
    from trading.backtest.metrics import sharpe as _sharpe

    mean_of_folds = float(
        np.mean([
            _sharpe(pd.Series(fold_returns[0]), periods_per_year=12),
            _sharpe(pd.Series(fold_returns[1]), periods_per_year=12),
        ])
    )
    assert mean_of_folds > 0.0


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
