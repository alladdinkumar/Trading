"""Tests for trading.portfolio.health — score_holding policy + technicals helper."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.portfolio.health import (
    ATH_DRAWDOWN_DEEP,
    ATH_DRAWDOWN_MILD,
    DIST_FROM_52W_HIGH_BEARISH,
    DIST_FROM_52W_HIGH_BULLISH,
    EXIT_NET_THRESHOLD,
    FUND_DEBT_EQUITY_HIGH,
    FUND_DEBT_EQUITY_LOW,
    FUND_PROFIT_GROWTH_BAD,
    FUND_PROFIT_GROWTH_GOOD,
    FUND_ROE_BAD,
    FUND_ROE_GOOD,
    HOLD_NET_THRESHOLD,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    SENTIMENT_BAD,
    SENTIMENT_GOOD,
    FundamentalsSnapshot,
    HoldingContext,
    SentimentSnapshot,
    TechnicalsSnapshot,
    score_holding,
    technicals_from_history,
)


def _ctx(
    *,
    technicals: TechnicalsSnapshot | None = None,
    fundamentals: FundamentalsSnapshot | None = None,
    sentiment: SentimentSnapshot | None = None,
    avg: float = 100.0,
    last: float = 100.0,
) -> HoldingContext:
    return HoldingContext(
        symbol="X",
        qty=10,
        avg_price=avg,
        last_price=last,
        technicals=technicals or TechnicalsSnapshot(),
        fundamentals=fundamentals or FundamentalsSnapshot(),
        sentiment=sentiment or SentimentSnapshot(),
    )


# ---------------------------------------------------------------------------
# Critical-event veto
# ---------------------------------------------------------------------------


def test_critical_event_short_circuits_to_exit() -> None:
    """Even with perfect fundamentals + technicals, a critical event → EXIT."""
    ctx = _ctx(
        technicals=TechnicalsSnapshot(
            above_200dma=True,
            drawdown_from_ath_pct=5.0,
            rsi_14=55.0,
            dist_to_52w_high_pct=3.0,
        ),
        fundamentals=FundamentalsSnapshot(
            profit_growth_yoy=0.5, roe=0.30, debt_to_equity=0.1,
            pe_percentile_5y=0.10, profit_cagr_3y=0.30,
        ),
        sentiment=SentimentSnapshot(score_30d=0.4, has_critical=True),
    )
    r = score_holding(ctx)
    assert r.verdict == "EXIT"
    assert "critical" in r.reasons[0].lower()


# ---------------------------------------------------------------------------
# Technicals axis
# ---------------------------------------------------------------------------


def test_below_200dma_votes_negative() -> None:
    r = score_holding(_ctx(technicals=TechnicalsSnapshot(above_200dma=False)))
    assert r.net_votes == -1
    assert any("below 200-DMA" in s for s in r.reasons)


def test_above_200dma_votes_positive() -> None:
    r = score_holding(_ctx(technicals=TechnicalsSnapshot(above_200dma=True)))
    assert r.net_votes == 1


def test_drawdown_mild_votes_positive() -> None:
    r = score_holding(_ctx(
        technicals=TechnicalsSnapshot(drawdown_from_ath_pct=ATH_DRAWDOWN_MILD - 1)))
    assert r.net_votes == 1


def test_drawdown_deep_votes_negative() -> None:
    r = score_holding(_ctx(
        technicals=TechnicalsSnapshot(drawdown_from_ath_pct=ATH_DRAWDOWN_DEEP + 1)))
    assert r.net_votes == -1


def test_rsi_band_healthy_votes_positive() -> None:
    r = score_holding(_ctx(technicals=TechnicalsSnapshot(rsi_14=50.0)))
    assert r.net_votes == 1


def test_rsi_oversold_extreme_votes_negative() -> None:
    """≤25 is extreme oversold; rule treats both extremes as warning signs."""
    r = score_holding(_ctx(technicals=TechnicalsSnapshot(rsi_14=RSI_OVERSOLD)))
    assert r.net_votes == -1


def test_rsi_overbought_borderline_is_neutral() -> None:
    """Between RSI_OVERBOUGHT (80) and RSI_DEEP_OVERBOUGHT (85) → 0 vote."""
    r = score_holding(_ctx(technicals=TechnicalsSnapshot(rsi_14=RSI_OVERBOUGHT + 1)))
    # Only one vote cast, value is 0
    assert r.votes_cast == 1
    assert r.net_votes == 0


def test_near_52w_high_votes_positive() -> None:
    r = score_holding(_ctx(
        technicals=TechnicalsSnapshot(dist_to_52w_high_pct=DIST_FROM_52W_HIGH_BULLISH - 1)))
    assert r.net_votes == 1


def test_far_from_52w_high_votes_negative() -> None:
    r = score_holding(_ctx(
        technicals=TechnicalsSnapshot(dist_to_52w_high_pct=DIST_FROM_52W_HIGH_BEARISH + 1)))
    assert r.net_votes == -1


# ---------------------------------------------------------------------------
# Fundamentals axis
# ---------------------------------------------------------------------------


def test_profit_growth_good_votes_positive() -> None:
    r = score_holding(_ctx(
        fundamentals=FundamentalsSnapshot(profit_growth_yoy=FUND_PROFIT_GROWTH_GOOD + 0.01)))
    assert r.net_votes == 1


def test_profit_growth_bad_votes_negative() -> None:
    r = score_holding(_ctx(
        fundamentals=FundamentalsSnapshot(profit_growth_yoy=FUND_PROFIT_GROWTH_BAD - 0.01)))
    assert r.net_votes == -1


def test_low_debt_votes_positive() -> None:
    r = score_holding(_ctx(
        fundamentals=FundamentalsSnapshot(debt_to_equity=FUND_DEBT_EQUITY_LOW - 0.05)))
    assert r.net_votes == 1


def test_high_debt_votes_negative() -> None:
    r = score_holding(_ctx(
        fundamentals=FundamentalsSnapshot(debt_to_equity=FUND_DEBT_EQUITY_HIGH + 0.05)))
    assert r.net_votes == -1


def test_high_roe_votes_positive() -> None:
    r = score_holding(_ctx(fundamentals=FundamentalsSnapshot(roe=FUND_ROE_GOOD + 0.01)))
    assert r.net_votes == 1


def test_low_roe_votes_negative() -> None:
    r = score_holding(_ctx(fundamentals=FundamentalsSnapshot(roe=FUND_ROE_BAD - 0.01)))
    assert r.net_votes == -1


# ---------------------------------------------------------------------------
# Sentiment axis
# ---------------------------------------------------------------------------


def test_good_sentiment_votes_positive() -> None:
    r = score_holding(_ctx(sentiment=SentimentSnapshot(score_30d=SENTIMENT_GOOD + 0.01)))
    assert r.net_votes == 1


def test_bad_sentiment_votes_negative() -> None:
    r = score_holding(_ctx(sentiment=SentimentSnapshot(score_30d=SENTIMENT_BAD - 0.01)))
    assert r.net_votes == -1


# ---------------------------------------------------------------------------
# Verdict bucketing
# ---------------------------------------------------------------------------


def test_all_strong_inputs_classify_hold() -> None:
    """8 of 8 positive votes → HOLD with max score."""
    ctx = _ctx(
        technicals=TechnicalsSnapshot(
            above_200dma=True,
            drawdown_from_ath_pct=8.0,
            rsi_14=55.0,
            dist_to_52w_high_pct=5.0,
        ),
        fundamentals=FundamentalsSnapshot(
            profit_growth_yoy=0.25, roe=0.20, debt_to_equity=0.30, profit_cagr_3y=0.15,
        ),
        sentiment=SentimentSnapshot(score_30d=0.30),
    )
    r = score_holding(ctx)
    assert r.verdict == "HOLD"
    assert r.score == 100  # all 9 votes positive


def test_all_weak_inputs_classify_exit() -> None:
    ctx = _ctx(
        technicals=TechnicalsSnapshot(
            above_200dma=False,
            drawdown_from_ath_pct=ATH_DRAWDOWN_DEEP + 5,
            rsi_14=20.0,
            dist_to_52w_high_pct=DIST_FROM_52W_HIGH_BEARISH + 5,
        ),
        fundamentals=FundamentalsSnapshot(
            profit_growth_yoy=-0.20, roe=0.02, debt_to_equity=2.5,
        ),
        sentiment=SentimentSnapshot(score_30d=-0.40),
    )
    r = score_holding(ctx)
    assert r.verdict == "EXIT"
    assert r.score == 0  # all votes negative


def test_low_evidence_defaults_to_trim() -> None:
    """Fewer than 3 votes available → don't classify HOLD/EXIT confidently."""
    ctx = _ctx(technicals=TechnicalsSnapshot(above_200dma=True))
    r = score_holding(ctx)
    assert r.verdict == "TRIM"
    assert any("insufficient evidence" in s.lower() for s in r.reasons)


def test_score_is_50_at_zero_net_votes() -> None:
    ctx = _ctx(technicals=TechnicalsSnapshot(
        above_200dma=True, drawdown_from_ath_pct=25.0, rsi_14=50.0,
        dist_to_52w_high_pct=20.0,
    ))
    # above_200dma = +1, drawdown 25% = 0, rsi healthy = +1, dist 20% = 0 → net +2/4
    r = score_holding(ctx)
    assert r.score == 75  # 50 + (2/4)*50


def test_pnl_pct_is_computed() -> None:
    ctx = _ctx(avg=100.0, last=120.0,
               technicals=TechnicalsSnapshot(above_200dma=True))
    r = score_holding(ctx)
    assert r.pnl_pct == pytest.approx(20.0)


def test_hold_threshold_is_three() -> None:
    """Net of HOLD_NET_THRESHOLD must classify HOLD; one less stays TRIM."""
    # Use 4 axes with +3 net = HOLD
    ctx_hold = _ctx(
        technicals=TechnicalsSnapshot(above_200dma=True, drawdown_from_ath_pct=8.0,
                                       rsi_14=50.0, dist_to_52w_high_pct=5.0),
    )
    r = score_holding(ctx_hold)
    assert r.net_votes == 4
    assert r.verdict == "HOLD"

    # Same minus one of the positives
    ctx_trim = _ctx(
        technicals=TechnicalsSnapshot(above_200dma=True, drawdown_from_ath_pct=8.0,
                                       rsi_14=50.0),  # drop the 52w
    )
    r2 = score_holding(ctx_trim)
    assert r2.net_votes == HOLD_NET_THRESHOLD
    assert r2.verdict == "HOLD"


def test_exit_threshold_constant_matches_implementation() -> None:
    assert EXIT_NET_THRESHOLD == -3
    assert HOLD_NET_THRESHOLD == 3


# ---------------------------------------------------------------------------
# technicals_from_history helper
# ---------------------------------------------------------------------------


def _history(n: int = 300, *, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    sma_200 = pd.Series(close).rolling(200).mean().to_numpy()
    rsi_14 = np.full(n, 55.0)  # constant so we can assert without recomputing
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"close": close, "high": high, "low": low, "sma_200": sma_200, "rsi_14": rsi_14},
        index=idx,
    )


def test_technicals_from_history_populates_all_fields() -> None:
    df = _history()
    snap = technicals_from_history(df)
    assert snap.above_200dma is not None
    assert snap.drawdown_from_ath_pct is not None
    assert snap.rsi_14 == pytest.approx(55.0)
    assert snap.dist_to_52w_high_pct is not None


def test_technicals_from_history_returns_empty_on_missing_df() -> None:
    snap = technicals_from_history(None)
    assert snap.above_200dma is None
    assert snap.drawdown_from_ath_pct is None


def test_technicals_from_history_handles_short_history() -> None:
    """<200 bars → sma_200 is NaN → above_200dma falls to None."""
    df = _history(n=50)
    snap = technicals_from_history(df)
    assert snap.above_200dma is None


def test_technicals_from_history_skips_trailing_nan_close() -> None:
    """yfinance occasionally appends an empty bar for an incomplete trading
    day; the snapshot must use the last *valid* close instead of None-ing out.
    Regression: caught on real Kite data 2026-05-15."""
    df = _history(n=300)
    # Add an empty bar at the end (close=NaN), as if today's bar isn't done
    next_day = df.index[-1] + pd.Timedelta(days=1)
    df.loc[next_day] = {col: np.nan for col in df.columns}
    snap = technicals_from_history(df)
    assert snap.above_200dma is not None  # not skipped
    assert snap.dist_to_52w_high_pct is not None
    assert snap.rsi_14 == pytest.approx(55.0)
