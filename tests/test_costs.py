"""Phase 7.1 — Zerodha cost model tests.

Pin charges to hand-computed values so an accidental rate edit shows up
immediately. Round-trip total on a flat trade should sit around 0.4% per
spec §8.2.
"""

from __future__ import annotations

import pytest

from trading.backtest.costs import (
    CostConfig,
    apply_slippage,
    buy_charges,
    round_trip_cost_pct,
    sell_charges,
)


def test_buy_charges_components() -> None:
    """₹100,000 buy. Hand-computed Zerodha-style components."""
    cfg = CostConfig()
    breakdown = buy_charges(100_000, cfg)
    # 0.03% × 100,000 = 30 → capped at 20
    assert breakdown.brokerage == pytest.approx(20.0)
    assert breakdown.stt == pytest.approx(100.0)  # 0.1%
    assert breakdown.exchange == pytest.approx(2.97)  # 0.00297%
    assert breakdown.sebi == pytest.approx(0.10)  # 0.0001%
    assert breakdown.stamp == pytest.approx(15.0)  # 0.015%
    # GST 18% on (brokerage + exchange + sebi) = 0.18 × 23.07
    assert breakdown.gst == pytest.approx(0.18 * (20 + 2.97 + 0.10))
    assert breakdown.total == pytest.approx(
        breakdown.brokerage
        + breakdown.stt
        + breakdown.exchange
        + breakdown.sebi
        + breakdown.stamp
        + breakdown.gst
    )


def test_sell_charges_no_stamp_duty() -> None:
    """Sell side: stamp duty is zero, everything else mirrors buy."""
    cfg = CostConfig()
    sell = sell_charges(100_000, cfg)
    assert sell.stamp == 0.0
    assert sell.stt == pytest.approx(100.0)
    assert sell.brokerage == pytest.approx(20.0)
    # Sell total = buy total − stamp.
    buy = buy_charges(100_000, cfg)
    assert sell.total == pytest.approx(buy.total - buy.stamp)


def test_brokerage_capped_at_20() -> None:
    """Large turnover: 0.03% > ₹20 → brokerage clamps to ₹20."""
    breakdown = buy_charges(1_000_000)
    # 0.03% × 1M = 300 → cap ₹20
    assert breakdown.brokerage == 20.0


def test_brokerage_below_cap() -> None:
    """Small turnover: 0.03% < ₹20 → use percentage."""
    breakdown = buy_charges(10_000)
    # 0.03% × 10,000 = 3
    assert breakdown.brokerage == pytest.approx(3.0)


def test_slippage_buy_increases_price() -> None:
    cfg = CostConfig()
    assert apply_slippage(100.0, "buy", cfg) == pytest.approx(100.1)


def test_slippage_sell_decreases_price() -> None:
    cfg = CostConfig()
    assert apply_slippage(100.0, "sell", cfg) == pytest.approx(99.9)


def test_round_trip_total_around_0_4_pct() -> None:
    """Flat round-trip on ₹100k ≈ 0.4% drag per spec §8.2."""
    pct = round_trip_cost_pct(100_000)
    # Lower bound ≈ slippage alone (0.2%) + STT both sides (0.2%) + stamp (0.015%) ≈ 0.4%
    assert 0.0035 < pct < 0.0050


def test_zero_value_returns_empty_breakdown() -> None:
    """Defensive: 0 turnover → all-zeros, no division surprise."""
    b = buy_charges(0)
    assert b.total == 0.0
    s = sell_charges(0)
    assert s.total == 0.0


def test_cost_config_is_frozen() -> None:
    cfg = CostConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.slippage_pct = 0.5  # type: ignore[misc]
