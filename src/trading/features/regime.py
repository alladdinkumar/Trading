"""Composite risk-regime classifier (spec §5.3).

A pure function over four axes: India VIX level, global-equity-futures
direction (mean pct change across S&P / Nasdaq / Dow), today's FII net flow,
and 1-day USDINR change. Each axis votes -1 / 0 / +1; the sum decides the
regime bucket. Thresholds are module constants so future-us can tune them
without touching the algorithm.

Output also includes per-axis votes and human-readable reasons — the daily
markdown brief uses those directly, and tests assert on them rather than
on the final bucket alone.

The position-size multiplier comes from spec §4.5: RISK_ON 1.0× / NEUTRAL
0.75× / RISK_OFF 0.5×.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trading.domain import MacroSnapshot

Regime = Literal["RISK_ON", "NEUTRAL", "RISK_OFF"]


# ---------------------------------------------------------------------------
# Thresholds — single source of truth, tweakable in one place
# ---------------------------------------------------------------------------

VIX_LOW = 14.0    # below → +1 (calm market)
VIX_HIGH = 20.0   # above → -1 (fearful market)

FUTURES_UP_PCT = 0.30    # mean futures pct change ≥ this → +1
FUTURES_DOWN_PCT = -0.30  # ≤ this → -1

FII_UP_CR = 2000.0      # net buy ≥ this → +1
FII_DOWN_CR = -2000.0   # net sell ≤ this → -1

USDINR_DEPRECIATION_PCT = 0.50   # rupee weakens by ≥ this → -1
USDINR_APPRECIATION_PCT = -0.50  # rupee strengthens by ≥ this → +1

RISK_ON_THRESHOLD = 2    # sum of votes ≥ → RISK_ON
RISK_OFF_THRESHOLD = -2  # sum of votes ≤ → RISK_OFF

# Multiplier on per-trade risk budget — see spec §4.5
SIZE_MULTIPLIER: dict[Regime, float] = {
    "RISK_ON": 1.0,
    "NEUTRAL": 0.75,
    "RISK_OFF": 0.5,
}


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeInput:
    """Pre-computed inputs to the regime classifier.

    Decouples the classifier from yfinance/NSE so synthetic inputs can
    exhaustively cover every vote combination in tests. All fields are
    Optional — a missing reading contributes a 0 vote (the neutral choice
    when we genuinely don't know).
    """

    vix: float | None
    global_futures_chg_pct: float | None
    fii_flow_cr: float | None
    usdinr_chg_pct: float | None


@dataclass(frozen=True)
class RegimeResult:
    """Output of `classify_regime`. `composite_score` ∈ [-4, +4]."""

    regime: Regime
    composite_score: int
    vix_vote: int
    futures_vote: int
    fii_vote: int
    usdinr_vote: int
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-axis voters
# ---------------------------------------------------------------------------


def _vix_vote(vix: float | None) -> tuple[int, str]:
    if vix is None:
        return 0, "vix: unknown"
    if vix < VIX_LOW:
        return 1, f"vix {vix:.1f} < {VIX_LOW} (calm)"
    if vix > VIX_HIGH:
        return -1, f"vix {vix:.1f} > {VIX_HIGH} (fear)"
    return 0, f"vix {vix:.1f} in normal band"


def _futures_vote(pct: float | None) -> tuple[int, str]:
    if pct is None:
        return 0, "global futures: unknown"
    if pct >= FUTURES_UP_PCT:
        return 1, f"global futures {pct:+.2f}% (up)"
    if pct <= FUTURES_DOWN_PCT:
        return -1, f"global futures {pct:+.2f}% (down)"
    return 0, f"global futures {pct:+.2f}% (flat)"


def _fii_vote(flow_cr: float | None) -> tuple[int, str]:
    if flow_cr is None:
        return 0, "FII flow: unknown"
    if flow_cr >= FII_UP_CR:
        return 1, f"FII +Rs {flow_cr:,.0f} cr (buying)"
    if flow_cr <= FII_DOWN_CR:
        return -1, f"FII Rs {flow_cr:,.0f} cr (selling)"
    return 0, f"FII Rs {flow_cr:,.0f} cr (flat)"


def _usdinr_vote(pct: float | None) -> tuple[int, str]:
    if pct is None:
        return 0, "USDINR: unknown"
    # Rupee weakening = USDINR rising = risk-off signal
    if pct >= USDINR_DEPRECIATION_PCT:
        return -1, f"USDINR {pct:+.2f}% (rupee weak)"
    if pct <= USDINR_APPRECIATION_PCT:
        return 1, f"USDINR {pct:+.2f}% (rupee strong)"
    return 0, f"USDINR {pct:+.2f}% (flat)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_regime(inp: RegimeInput) -> RegimeResult:
    """Bucket the inputs into RISK_ON / NEUTRAL / RISK_OFF."""
    vix_v, vix_r = _vix_vote(inp.vix)
    fut_v, fut_r = _futures_vote(inp.global_futures_chg_pct)
    fii_v, fii_r = _fii_vote(inp.fii_flow_cr)
    usd_v, usd_r = _usdinr_vote(inp.usdinr_chg_pct)
    score = vix_v + fut_v + fii_v + usd_v

    regime: Regime
    if score >= RISK_ON_THRESHOLD:
        regime = "RISK_ON"
    elif score <= RISK_OFF_THRESHOLD:
        regime = "RISK_OFF"
    else:
        regime = "NEUTRAL"

    return RegimeResult(
        regime=regime,
        composite_score=score,
        vix_vote=vix_v,
        futures_vote=fut_v,
        fii_vote=fii_v,
        usdinr_vote=usd_v,
        reasons=[vix_r, fut_r, fii_r, usd_r],
    )


def position_size_multiplier(regime: Regime) -> float:
    """Risk-budget multiplier per spec §4.5."""
    return SIZE_MULTIPLIER[regime]


# ---------------------------------------------------------------------------
# Adapter: build RegimeInput from data.macro fetch output
# ---------------------------------------------------------------------------


def regime_input_from_quotes(
    quotes: Mapping[str, object],  # data.macro.YfQuote, but avoid hard import for typing
    fii_flow_cr: float | None,
) -> RegimeInput:
    """Build a RegimeInput from `data.macro.fetch_all_yf_quotes` output.

    Mean global-futures pct change is averaged across whichever of
    S&P/Nasdaq/Dow have data. VIX uses the close-as-level; USDINR uses
    1-day pct change. None values flow through.
    """
    sp = _attr(quotes.get("sp500"), "pct_change_1d")
    nq = _attr(quotes.get("nasdaq"), "pct_change_1d")
    dj = _attr(quotes.get("dow"), "pct_change_1d")
    available = [x for x in (sp, nq, dj) if x is not None]
    global_pct = sum(available) / len(available) if available else None

    return RegimeInput(
        vix=_attr(quotes.get("vix"), "close"),
        global_futures_chg_pct=global_pct,
        fii_flow_cr=fii_flow_cr,
        usdinr_chg_pct=_attr(quotes.get("usdinr"), "pct_change_1d"),
    )


def _attr(obj: object, name: str) -> float | None:
    val = getattr(obj, name, None) if obj is not None else None
    return float(val) if val is not None else None


def snapshot_and_classify(as_of: date) -> tuple[MacroSnapshot, RegimeResult]:
    """End-to-end macro pipeline: fetch once, build snapshot, classify regime.

    Returns the snapshot row with the regime bucket already filled in, plus the
    full `RegimeResult` (per-axis votes + reasons) so callers can surface the
    narrative into the daily briefing.

    This orchestration lives in `features` (the analysis layer), not in
    `data.macro`: fetching is a data concern but classifying is a decision one,
    so the data layer stays fetch-only and this is the single place the two are
    composed (finding F-007). The `data.macro` import is function-local only to
    avoid paying its yfinance import cost for unrelated regime callers.
    """
    from trading.data.macro import build_snapshot, fetch_all_yf_quotes, fetch_fii_dii

    quotes = fetch_all_yf_quotes()
    fii_cr, dii_cr = fetch_fii_dii()
    snap = build_snapshot(as_of, quotes=quotes, fii_dii=(fii_cr, dii_cr))
    regime_inp = regime_input_from_quotes(quotes, fii_cr)
    result = classify_regime(regime_inp)
    # Replace `regime` on the frozen dataclass via constructor:
    classified = MacroSnapshot(**{**snap.__dict__, "regime": result.regime})
    return classified, result
