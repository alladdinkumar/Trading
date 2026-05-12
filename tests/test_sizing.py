"""Phase 6 — position-sizing tests.

Spec §4.4: max loss per trade ≤ 2% of capital; ≤25% capital per stock;
≤30% capital per sector. Regime multiplier per §4.5.
"""

from __future__ import annotations

import math

import pytest

from trading.strategy.sizing import (
    REGIME_MULTIPLIER,
    SizingInput,
    SizingResult,
    position_size,
)

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_basic_size_risk_budget_drives_qty() -> None:
    """capital=100k, risk_pct=2% → budget=2000. entry-stop=10 → 200 shares.

    Per-share risk must be ≥ 8% of entry for risk budget to bind ahead of the
    25%-per-stock cap (`0.02 × entry / per_share_risk ≤ 0.25` ⇒ risk ≥ 8% of entry).
    """
    result = position_size(SizingInput(capital=100_000, entry=100, stop=90, risk_pct=0.02))
    assert result.qty == 200
    assert result.notional == pytest.approx(20_000)
    assert result.capital_at_risk == pytest.approx(2000)


def test_capital_at_risk_invariant() -> None:
    """result.capital_at_risk == qty × per-share-risk, always."""
    result = position_size(SizingInput(capital=200_000, entry=250, stop=235, risk_pct=0.02))
    per_share_risk = 250 - 235
    assert result.capital_at_risk == pytest.approx(result.qty * per_share_risk)


# ---------------------------------------------------------------------------
# Regime multiplier (spec §4.5)
# ---------------------------------------------------------------------------


def test_regime_multiplier_table() -> None:
    """RISK_ON 1.0×, NEUTRAL 0.75×, RISK_OFF 0.5×."""
    assert REGIME_MULTIPLIER["RISK_ON"] == 1.0
    assert REGIME_MULTIPLIER["NEUTRAL"] == 0.75
    assert REGIME_MULTIPLIER["RISK_OFF"] == 0.5


def test_neutral_regime_reduces_qty() -> None:
    """RISK_ON → 200 shares; NEUTRAL → 150; RISK_OFF → 100. (entry-stop=10, ₹100K capital.)"""
    base = SizingInput(capital=100_000, entry=100, stop=90)
    assert position_size(base).qty == 200
    assert (
        position_size(SizingInput(capital=100_000, entry=100, stop=90, regime="NEUTRAL")).qty == 150
    )
    assert (
        position_size(SizingInput(capital=100_000, entry=100, stop=90, regime="RISK_OFF")).qty
        == 100
    )


# ---------------------------------------------------------------------------
# Concurrency caps (§4.4)
# ---------------------------------------------------------------------------


def test_stock_cap_binds_with_tight_stop() -> None:
    """Tight stop → risk-qty huge; per-stock cap (25% of 100k = 25k / 100 = 250) binds."""
    result = position_size(
        SizingInput(capital=100_000, entry=100, stop=99, risk_pct=0.02)
        # risk budget 2000 / per-share risk 1 = 2000 shares; per-stock cap = 250.
    )
    assert result.qty == 250
    assert any("per-stock cap" in r for r in result.reasons)


def test_sector_cap_binds() -> None:
    """deployed_in_sector eats into 30% sector budget so sector cap < per-stock cap."""
    # 30% of 100k = 30k sector budget. 20k already deployed → 10k available.
    # 10k / entry 100 = 100 shares. Tight stop so risk budget would otherwise allow far more.
    result = position_size(
        SizingInput(
            capital=100_000,
            entry=100,
            stop=99,
            deployed_in_sector=20_000,
        )
    )
    assert result.qty == 100
    assert any("per-sector cap" in r for r in result.reasons)


def test_already_capped_in_symbol_returns_zero() -> None:
    """If deployed_in_symbol already at 25% cap, no more room."""
    result = position_size(
        SizingInput(capital=100_000, entry=100, stop=95, deployed_in_symbol=25_000)
    )
    assert result.qty == 0


def test_reasons_lists_binding_constraint_for_each_cap() -> None:
    """Each cap, when binding, should be named in reasons."""
    # Sector binds first (smaller available).
    r1 = position_size(SizingInput(capital=100_000, entry=100, stop=99, deployed_in_sector=25_000))
    # 30k − 25k = 5k; 5k/100 = 50 shares.
    assert r1.qty == 50
    assert any("per-sector" in r for r in r1.reasons)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_entry_equals_stop_raises() -> None:
    with pytest.raises(ValueError, match="entry must be > stop"):
        position_size(SizingInput(capital=100_000, entry=100, stop=100))


def test_entry_below_stop_raises() -> None:
    with pytest.raises(ValueError, match="entry must be > stop"):
        position_size(SizingInput(capital=100_000, entry=95, stop=100))


def test_non_positive_capital_raises() -> None:
    with pytest.raises(ValueError, match="capital must be > 0"):
        position_size(SizingInput(capital=0, entry=100, stop=95))


def test_risk_pct_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="risk_pct"):
        position_size(SizingInput(capital=100_000, entry=100, stop=95, risk_pct=0))
    with pytest.raises(ValueError, match="risk_pct"):
        position_size(SizingInput(capital=100_000, entry=100, stop=95, risk_pct=1.5))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_budget_too_small_returns_zero() -> None:
    """Tiny capital + wide stop → can't afford even 1 share at this risk."""
    # capital 1000, risk_pct 0.02 → budget 20; per-share risk 50 → 0 shares.
    result = position_size(SizingInput(capital=1000, entry=100, stop=50))
    assert result.qty == 0
    assert any("budget" in r.lower() for r in result.reasons)


def test_result_is_frozen_dataclass() -> None:
    """SizingResult should be immutable."""
    result = position_size(SizingInput(capital=100_000, entry=100, stop=95))
    with pytest.raises((AttributeError, TypeError)):
        result.qty = 999  # type: ignore[misc]


def test_qty_is_integer_floor() -> None:
    """qty is always a non-negative integer (no fractional shares).

    100,000 × 0.02 = 2000; per-share risk = 7 → 285.71... → floor 285.
    Use entry=50 so per-share risk (14%) exceeds the 8% threshold for risk-budget
    to bind ahead of the per-stock cap.
    """
    result = position_size(SizingInput(capital=100_000, entry=50, stop=43))
    assert result.qty == 285
    assert isinstance(result.qty, int)


def test_qty_never_negative() -> None:
    """Over-deployed sector can yield negative-room; clamped at 0."""
    result = position_size(
        SizingInput(capital=100_000, entry=100, stop=95, deployed_in_sector=50_000)
    )
    assert result.qty == 0


def test_qty_matches_manual_calc_for_realistic_trade() -> None:
    """₹100k capital, 2% risk, entry 100, stop 85 (15% per-share risk → risk binds)."""
    result = position_size(SizingInput(capital=100_000, entry=100, stop=85))
    expected_qty = math.floor(2000 / 15)  # 133
    assert result.qty == expected_qty


def test_sizing_input_defaults_match_spec() -> None:
    """Spec §4.4 defaults: 2% risk, 25% per stock, 30% per sector, RISK_ON."""
    inp = SizingInput(capital=100_000, entry=100, stop=95)
    assert inp.risk_pct == 0.02
    assert inp.max_per_stock_pct == 0.25
    assert inp.max_per_sector_pct == 0.30
    assert inp.regime == "RISK_ON"


def test_result_type_is_sizing_result() -> None:
    """API contract: position_size returns a SizingResult."""
    result = position_size(SizingInput(capital=100_000, entry=100, stop=95))
    assert isinstance(result, SizingResult)
