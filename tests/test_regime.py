"""Tests for trading.features.regime — composite voter + position-size policy."""

from __future__ import annotations

import pytest

from trading.features.regime import (
    FII_DOWN_CR,
    FII_UP_CR,
    FUTURES_DOWN_PCT,
    FUTURES_UP_PCT,
    SIZE_MULTIPLIER,
    USDINR_APPRECIATION_PCT,
    USDINR_DEPRECIATION_PCT,
    VIX_HIGH,
    VIX_LOW,
    RegimeInput,
    classify_regime,
    position_size_multiplier,
    regime_input_from_quotes,
)

# ---------------------------------------------------------------------------
# Per-axis voting — each axis covers +1 / 0 / -1
# ---------------------------------------------------------------------------


def _neutral_baseline() -> RegimeInput:
    """Inputs that produce 0 on every axis — useful when isolating a single axis."""
    return RegimeInput(
        vix=(VIX_LOW + VIX_HIGH) / 2,
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )


def test_baseline_is_neutral() -> None:
    r = classify_regime(_neutral_baseline())
    assert r.regime == "NEUTRAL"
    assert r.composite_score == 0
    assert (r.vix_vote, r.futures_vote, r.fii_vote, r.usdinr_vote) == (0, 0, 0, 0)


# --- VIX axis ---


def test_vix_below_low_votes_plus_one() -> None:
    inp = _neutral_baseline()
    inp = RegimeInput(vix=VIX_LOW - 1, **{k: getattr(inp, k) for k in
        ("global_futures_chg_pct", "fii_flow_cr", "usdinr_chg_pct")})
    r = classify_regime(inp)
    assert r.vix_vote == 1


def test_vix_above_high_votes_minus_one() -> None:
    inp = RegimeInput(
        vix=VIX_HIGH + 1.0,
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    r = classify_regime(inp)
    assert r.vix_vote == -1


def test_vix_inside_band_votes_zero() -> None:
    inp = RegimeInput(
        vix=(VIX_LOW + VIX_HIGH) / 2,
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    assert classify_regime(inp).vix_vote == 0


def test_vix_missing_votes_zero() -> None:
    inp = RegimeInput(
        vix=None,
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    r = classify_regime(inp)
    assert r.vix_vote == 0
    assert "unknown" in r.reasons[0]


# --- Futures axis ---


def test_futures_above_threshold_votes_plus_one() -> None:
    inp = RegimeInput(
        vix=17.0,
        global_futures_chg_pct=FUTURES_UP_PCT + 0.1,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    assert classify_regime(inp).futures_vote == 1


def test_futures_below_threshold_votes_minus_one() -> None:
    inp = RegimeInput(
        vix=17.0,
        global_futures_chg_pct=FUTURES_DOWN_PCT - 0.1,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    assert classify_regime(inp).futures_vote == -1


# --- FII axis ---


def test_fii_above_threshold_votes_plus_one() -> None:
    inp = RegimeInput(
        vix=17.0,
        global_futures_chg_pct=0.0,
        fii_flow_cr=FII_UP_CR + 100,
        usdinr_chg_pct=0.0,
    )
    assert classify_regime(inp).fii_vote == 1


def test_fii_below_threshold_votes_minus_one() -> None:
    inp = RegimeInput(
        vix=17.0,
        global_futures_chg_pct=0.0,
        fii_flow_cr=FII_DOWN_CR - 100,
        usdinr_chg_pct=0.0,
    )
    assert classify_regime(inp).fii_vote == -1


# --- USDINR axis (note: rupee weak = -1) ---


def test_usdinr_depreciation_votes_minus_one() -> None:
    inp = RegimeInput(
        vix=17.0,
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=USDINR_DEPRECIATION_PCT + 0.1,
    )
    assert classify_regime(inp).usdinr_vote == -1


def test_usdinr_appreciation_votes_plus_one() -> None:
    inp = RegimeInput(
        vix=17.0,
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=USDINR_APPRECIATION_PCT - 0.1,
    )
    assert classify_regime(inp).usdinr_vote == 1


# ---------------------------------------------------------------------------
# Bucket boundaries
# ---------------------------------------------------------------------------


def test_all_bullish_inputs_classify_risk_on() -> None:
    inp = RegimeInput(
        vix=VIX_LOW - 1,
        global_futures_chg_pct=FUTURES_UP_PCT + 0.1,
        fii_flow_cr=FII_UP_CR + 100,
        usdinr_chg_pct=USDINR_APPRECIATION_PCT - 0.1,
    )
    r = classify_regime(inp)
    assert r.regime == "RISK_ON"
    assert r.composite_score == 4


def test_all_bearish_inputs_classify_risk_off() -> None:
    inp = RegimeInput(
        vix=VIX_HIGH + 1,
        global_futures_chg_pct=FUTURES_DOWN_PCT - 0.1,
        fii_flow_cr=FII_DOWN_CR - 100,
        usdinr_chg_pct=USDINR_DEPRECIATION_PCT + 0.1,
    )
    r = classify_regime(inp)
    assert r.regime == "RISK_OFF"
    assert r.composite_score == -4


def test_score_of_one_stays_neutral() -> None:
    """Below RISK_ON threshold (which is +2)."""
    inp = RegimeInput(
        vix=VIX_LOW - 1,   # +1
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    r = classify_regime(inp)
    assert r.composite_score == 1
    assert r.regime == "NEUTRAL"


def test_score_of_minus_one_stays_neutral() -> None:
    inp = RegimeInput(
        vix=VIX_HIGH + 1,  # -1
        global_futures_chg_pct=0.0,
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    r = classify_regime(inp)
    assert r.composite_score == -1
    assert r.regime == "NEUTRAL"


def test_score_of_plus_two_classifies_risk_on() -> None:
    inp = RegimeInput(
        vix=VIX_LOW - 1,  # +1
        global_futures_chg_pct=FUTURES_UP_PCT + 0.1,  # +1
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    r = classify_regime(inp)
    assert r.composite_score == 2
    assert r.regime == "RISK_ON"


def test_score_of_minus_two_classifies_risk_off() -> None:
    inp = RegimeInput(
        vix=VIX_HIGH + 1,  # -1
        global_futures_chg_pct=FUTURES_DOWN_PCT - 0.1,  # -1
        fii_flow_cr=0.0,
        usdinr_chg_pct=0.0,
    )
    r = classify_regime(inp)
    assert r.composite_score == -2
    assert r.regime == "RISK_OFF"


def test_mixed_signals_with_all_unknown_stays_neutral() -> None:
    """All None inputs → all votes 0 → NEUTRAL."""
    r = classify_regime(RegimeInput(None, None, None, None))
    assert r.regime == "NEUTRAL"
    assert r.composite_score == 0
    assert all("unknown" in reason for reason in r.reasons)


# ---------------------------------------------------------------------------
# Position-size multiplier (spec §4.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "regime,expected",
    [
        ("RISK_ON", 1.0),
        ("NEUTRAL", 0.75),
        ("RISK_OFF", 0.5),
    ],
)
def test_position_size_multiplier(regime: str, expected: float) -> None:
    assert position_size_multiplier(regime) == expected  # type: ignore[arg-type]


def test_size_multiplier_table_matches_spec() -> None:
    """RISK_OFF must reduce size by half — the kill-switch from spec §4.5."""
    assert SIZE_MULTIPLIER["RISK_OFF"] == 0.5
    assert SIZE_MULTIPLIER["NEUTRAL"] == 0.75
    assert SIZE_MULTIPLIER["RISK_ON"] == 1.0


# ---------------------------------------------------------------------------
# regime_input_from_quotes — adapter to data.macro.YfQuote
# ---------------------------------------------------------------------------


def test_regime_input_from_quotes_averages_global_futures() -> None:
    """The three US indices should be averaged into a single pct number."""
    from trading.data.macro import YfQuote

    quotes = {
        "vix": YfQuote(ticker="^INDIAVIX", close=12.0, pct_change_1d=None),
        "sp500": YfQuote(ticker="^GSPC", close=5000, pct_change_1d=1.0),
        "nasdaq": YfQuote(ticker="^IXIC", close=18000, pct_change_1d=2.0),
        "dow": YfQuote(ticker="^DJI", close=40000, pct_change_1d=0.0),
        "usdinr": YfQuote(ticker="INR=X", close=83.5, pct_change_1d=-0.3),
    }
    inp = regime_input_from_quotes(quotes, fii_flow_cr=3000.0)
    assert inp.vix == 12.0
    assert inp.global_futures_chg_pct == pytest.approx(1.0)
    assert inp.usdinr_chg_pct == pytest.approx(-0.3)
    assert inp.fii_flow_cr == 3000.0


def test_regime_input_from_quotes_handles_missing() -> None:
    """A missing yfinance fetch should flow through as None without crashing."""
    inp = regime_input_from_quotes({}, fii_flow_cr=None)
    assert inp == RegimeInput(None, None, None, None)


def test_regime_input_from_quotes_partial_futures() -> None:
    """If only one of the three US indices has data, that's the mean."""
    from trading.data.macro import YfQuote

    quotes = {
        "sp500": YfQuote(ticker="^GSPC", close=None, pct_change_1d=None),
        "nasdaq": YfQuote(ticker="^IXIC", close=18000, pct_change_1d=1.5),
        "dow": YfQuote(ticker="^DJI", close=None, pct_change_1d=None),
    }
    inp = regime_input_from_quotes(quotes, fii_flow_cr=0.0)
    assert inp.global_futures_chg_pct == pytest.approx(1.5)
