"""Per-holding HOLD / TRIM / EXIT scorer (spec §7.1).

A pure vote-based classifier — each criterion contributes +1 / 0 / -1; a
`has_critical` sentiment flag triggers an immediate EXIT veto regardless of
score. Inputs arrive as a `HoldingContext` (fundamentals / technicals /
sentiment); the function never touches Kite or yfinance, so the policy is
testable against synthetic data and the data side stays swappable.

The fundamentals snapshot fields are all Optional — a missing value
contributes nothing to the vote rather than penalising the holding. The
scorer scales the verdict thresholds by the number of *available* votes
(`REFERENCE_BALLOT_SIZE`) so the verdict scales gracefully when half the
fields are None — a technicals-only holding can still reach HOLD/EXIT
instead of always defaulting to TRIM (F-022).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["HOLD", "TRIM", "EXIT"]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundamentalsSnapshot:
    """Per-holding fundamentals. All Optional — None contributes 0 votes."""

    profit_growth_yoy: float | None = None  # decimal: 0.15 = +15% YoY
    profit_cagr_3y: float | None = None  # decimal: 0.10 = 10%/yr
    debt_to_equity: float | None = None
    roe: float | None = None  # decimal: 0.18 = 18% ROE
    pe_percentile_5y: float | None = None  # 0..1 (rank in 5yr P/E history)


@dataclass(frozen=True)
class TechnicalsSnapshot:
    """Per-holding technicals derived from OHLCV history."""

    above_200dma: bool | None = None
    drawdown_from_ath_pct: float | None = None  # positive number, e.g. 25.0 means -25% from ATH
    rsi_14: float | None = None
    dist_to_52w_high_pct: float | None = None  # positive number


@dataclass(frozen=True)
class SentimentSnapshot:
    """Trailing-30d sentiment rollup pulled from sentiment_daily."""

    score_30d: float | None = None
    has_critical: bool = False


@dataclass(frozen=True)
class HoldingContext:
    """All inputs the health scorer needs for one position."""

    symbol: str
    qty: int
    avg_price: float
    last_price: float
    fundamentals: FundamentalsSnapshot = field(default_factory=FundamentalsSnapshot)
    technicals: TechnicalsSnapshot = field(default_factory=TechnicalsSnapshot)
    sentiment: SentimentSnapshot = field(default_factory=SentimentSnapshot)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthScore:
    """Verdict + 0..100 score + structured reasoning for the daily brief."""

    symbol: str
    verdict: Verdict
    score: int
    net_votes: int
    votes_cast: int
    reasons: list[str] = field(default_factory=list)
    pnl_pct: float | None = None


# ---------------------------------------------------------------------------
# Thresholds — single source of truth, tweakable
# ---------------------------------------------------------------------------

# Drawdown from ATH
ATH_DRAWDOWN_MILD = 15.0  # ≤ → +1 (mild correction)
ATH_DRAWDOWN_DEEP = 35.0  # ≥ → -1 (real damage)

# RSI bands
RSI_OVERSOLD = 25.0
RSI_OVERBOUGHT = 80.0
RSI_DEEP_OVERBOUGHT = 85.0

DIST_FROM_52W_HIGH_BULLISH = 10.0  # ≤ → +1 (near highs)
DIST_FROM_52W_HIGH_BEARISH = 30.0  # ≥ → -1 (well below highs)

FUND_PROFIT_GROWTH_GOOD = 0.10  # YoY ≥ → +1
FUND_PROFIT_GROWTH_BAD = -0.05  # YoY ≤ → -1
FUND_CAGR_3Y_GOOD = 0.10
FUND_CAGR_3Y_BAD = 0.0
FUND_DEBT_EQUITY_LOW = 0.50  # ≤ → +1
FUND_DEBT_EQUITY_HIGH = 1.50  # ≥ → -1
FUND_ROE_GOOD = 0.15
FUND_ROE_BAD = 0.05
FUND_PE_PCTILE_CHEAP = 0.40  # ≤ → +1 (bottom 40% of 5yr range)
FUND_PE_PCTILE_RICH = 0.85  # ≥ → -1 (top 15%)

SENTIMENT_GOOD = 0.10
SENTIMENT_BAD = -0.20

# Verdict thresholds — net votes (HOLD)/(EXIT) on a typical 8-axis ballot
HOLD_NET_THRESHOLD = 3
EXIT_NET_THRESHOLD = -3

# F-022: the ±3 cut above was tuned for a full ballot. The reference size lets
# `score_holding` scale the cut by the ballot *actually* cast, so a
# technicals-only holding (≈4 axes when fundamentals/sentiment are unpopulated)
# can still reach HOLD/EXIT instead of always defaulting to TRIM. On a full
# 8-axis ballot the scaled cut reduces to the original net ≥ 3 / ≤ −3.
REFERENCE_BALLOT_SIZE = 8
MIN_VOTES_FOR_VERDICT = 3
_HOLD_FRACTION = HOLD_NET_THRESHOLD / REFERENCE_BALLOT_SIZE  # +0.375
_EXIT_FRACTION = EXIT_NET_THRESHOLD / REFERENCE_BALLOT_SIZE  # −0.375


# ---------------------------------------------------------------------------
# Per-axis voters — `vote()` returns (vote ∈ {-1, 0, +1}, reason str)
# ---------------------------------------------------------------------------


def _band_vote(
    value: float | None,
    *,
    good_at_or_above: float | None = None,
    good_at_or_below: float | None = None,
    bad_at_or_below: float | None = None,
    bad_at_or_above: float | None = None,
    label: str,
    fmt: str = "{:.2f}",
) -> tuple[int, str] | None:
    """Vote helper for a numeric input with up to 4 threshold knobs.

    Returns None when the input is None (so the caller skips that axis
    entirely rather than counting it as a neutral 0). Returns (1, ...) if
    *good* threshold met, (-1, ...) if *bad* threshold met, (0, ...) else.
    """
    if value is None:
        return None
    val_str = fmt.format(value)
    if good_at_or_above is not None and value >= good_at_or_above:
        return 1, f"{label}: {val_str} (strong)"
    if good_at_or_below is not None and value <= good_at_or_below:
        return 1, f"{label}: {val_str} (favourable)"
    if bad_at_or_below is not None and value <= bad_at_or_below:
        return -1, f"{label}: {val_str} (weak)"
    if bad_at_or_above is not None and value >= bad_at_or_above:
        return -1, f"{label}: {val_str} (concerning)"
    return 0, f"{label}: {val_str}"


def _vote_technicals(t: TechnicalsSnapshot) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    if t.above_200dma is not None:
        v = 1 if t.above_200dma else -1
        out.append((v, f"trend: {'above' if v > 0 else 'below'} 200-DMA"))

    res = _band_vote(
        t.drawdown_from_ath_pct,
        good_at_or_below=ATH_DRAWDOWN_MILD,
        bad_at_or_above=ATH_DRAWDOWN_DEEP,
        label="drawdown from ATH",
        fmt="{:.1f}%",
    )
    if res is not None:
        out.append(res)

    if t.rsi_14 is not None:
        if RSI_OVERSOLD < t.rsi_14 < RSI_OVERBOUGHT:
            out.append((1, f"RSI {t.rsi_14:.1f} (healthy band)"))
        elif t.rsi_14 >= RSI_DEEP_OVERBOUGHT or t.rsi_14 <= RSI_OVERSOLD:
            out.append((-1, f"RSI {t.rsi_14:.1f} (extreme)"))
        else:
            out.append((0, f"RSI {t.rsi_14:.1f}"))

    res = _band_vote(
        t.dist_to_52w_high_pct,
        good_at_or_below=DIST_FROM_52W_HIGH_BULLISH,
        bad_at_or_above=DIST_FROM_52W_HIGH_BEARISH,
        label="dist to 52w high",
        fmt="{:.1f}%",
    )
    if res is not None:
        out.append(res)

    return out


def _vote_fundamentals(f: FundamentalsSnapshot) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for value, good, bad, label, fmt in (
        (
            f.profit_growth_yoy,
            FUND_PROFIT_GROWTH_GOOD,
            FUND_PROFIT_GROWTH_BAD,
            "profit growth (YoY)",
            "{:+.1%}",
        ),
        (f.profit_cagr_3y, FUND_CAGR_3Y_GOOD, FUND_CAGR_3Y_BAD, "profit CAGR (3y)", "{:+.1%}"),
        (f.roe, FUND_ROE_GOOD, FUND_ROE_BAD, "ROE", "{:.1%}"),
    ):
        res = _band_vote(
            value,
            good_at_or_above=good,
            bad_at_or_below=bad,
            label=label,
            fmt=fmt,
        )
        if res is not None:
            out.append(res)

    # Debt: low is good
    res = _band_vote(
        f.debt_to_equity,
        good_at_or_below=FUND_DEBT_EQUITY_LOW,
        bad_at_or_above=FUND_DEBT_EQUITY_HIGH,
        label="debt/equity",
        fmt="{:.2f}",
    )
    if res is not None:
        out.append(res)

    # P/E percentile: low (cheap relative to history) is good
    res = _band_vote(
        f.pe_percentile_5y,
        good_at_or_below=FUND_PE_PCTILE_CHEAP,
        bad_at_or_above=FUND_PE_PCTILE_RICH,
        label="P/E pctile vs 5y",
        fmt="{:.0%}",
    )
    if res is not None:
        out.append(res)

    return out


def _vote_sentiment(s: SentimentSnapshot) -> list[tuple[int, str]]:
    """Note: `has_critical` is handled separately as a hard EXIT veto."""
    out: list[tuple[int, str]] = []
    res = _band_vote(
        s.score_30d,
        good_at_or_above=SENTIMENT_GOOD,
        bad_at_or_below=SENTIMENT_BAD,
        label="30d sentiment",
        fmt="{:+.2f}",
    )
    if res is not None:
        out.append(res)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_holding(ctx: HoldingContext) -> HealthScore:
    """Vote across every available axis and bucket into HOLD / TRIM / EXIT.

    Critical-event flag short-circuits to EXIT — that's the SEBI/SAT/fraud
    veto from spec §4.1 reused here. If too few axes have data
    (`votes_cast < 3`) we return TRIM by default rather than misclassify
    a thinly-evidenced position.
    """
    pnl_pct = (
        (ctx.last_price - ctx.avg_price) / ctx.avg_price * 100.0 if ctx.avg_price > 0 else None
    )

    if ctx.sentiment.has_critical:
        return HealthScore(
            symbol=ctx.symbol,
            verdict="EXIT",
            score=0,
            net_votes=0,
            votes_cast=0,
            reasons=["critical sentiment event in last 30d — EXIT veto"],
            pnl_pct=pnl_pct,
        )

    votes = (
        _vote_technicals(ctx.technicals)
        + _vote_fundamentals(ctx.fundamentals)
        + _vote_sentiment(ctx.sentiment)
    )
    votes_cast = len(votes)
    net = sum(v for v, _ in votes)
    reasons = [r for _, r in votes]

    if votes_cast < MIN_VOTES_FOR_VERDICT:
        verdict: Verdict = "TRIM"
        reasons.append("insufficient evidence — defaulting to TRIM")
    else:
        # Scale the ±3 cut by the ballot actually cast (F-022). `frac` is the
        # same net/votes_cast ratio the 0..100 score uses below, so verdict and
        # score never disagree. On 8 axes this is exactly net ≥ 3 / ≤ −3.
        frac = net / votes_cast
        if frac >= _HOLD_FRACTION:
            verdict = "HOLD"
        elif frac <= _EXIT_FRACTION:
            verdict = "EXIT"
        else:
            verdict = "TRIM"

    # 0..100 score: 50 at neutral, scaled by net / votes_cast.
    if votes_cast > 0:
        score = round(50 + (net / votes_cast) * 50)
        score = max(0, min(100, score))
    else:
        score = 50

    return HealthScore(
        symbol=ctx.symbol,
        verdict=verdict,
        score=score,
        net_votes=net,
        votes_cast=votes_cast,
        reasons=reasons,
        pnl_pct=pnl_pct,
    )


# ---------------------------------------------------------------------------
# Technicals computation from OHLCV — convenience helper
# ---------------------------------------------------------------------------


def technicals_from_history(history: object) -> TechnicalsSnapshot:
    """Compute the technicals slice from an enriched OHLCV DataFrame.

    Expects the column shape produced by `features.technicals.add_indicators`
    (`close`, `sma_200`, `rsi_14`). If the indicator columns are absent we
    fall back to whatever is computable; missing values become None.
    """
    import pandas as pd

    if not isinstance(history, pd.DataFrame) or history.empty:
        return TechnicalsSnapshot()

    # yfinance sometimes appends an empty bar for an incomplete trading day —
    # find the most recent row whose close is populated.
    valid = history.dropna(subset=["close"]) if "close" in history.columns else history
    if valid.empty:
        return TechnicalsSnapshot()

    last = valid.iloc[-1]
    close = float(last["close"])

    sma_200 = (
        float(last["sma_200"])
        if "sma_200" in valid.columns and last["sma_200"] == last["sma_200"]
        else None
    )
    above = (close > sma_200) if sma_200 is not None else None

    ath = float(valid["high"].max()) if "high" in valid.columns else None
    dd = ((ath - close) / ath * 100.0) if ath and ath > 0 else None

    rsi = (
        float(last["rsi_14"])
        if "rsi_14" in valid.columns and last["rsi_14"] == last["rsi_14"]
        else None
    )

    # 52w high: last 252 valid trading bars
    if "high" in valid.columns and len(valid) >= 1:
        recent = valid["high"].tail(252)
        hi52 = float(recent.max())
        dist_52 = ((hi52 - close) / hi52 * 100.0) if hi52 > 0 else None
    else:
        dist_52 = None

    return TechnicalsSnapshot(
        above_200dma=above,
        drawdown_from_ath_pct=dd,
        rsi_14=rsi,
        dist_to_52w_high_pct=dist_52,
    )
