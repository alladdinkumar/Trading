"""Tests for trading.portfolio.gtt — GBM simulator + GTT projector."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.data.kite import GttOrder
from trading.portfolio.gtt import (
    DEFAULT_HORIZON_DAYS,
    GttViability,
    project_all_gtts,
    project_gtt_viability,
    simulate_target_hit,
)


def _gtt(
    *,
    gtt_id: int = 1,
    symbol: str = "X",
    trigger: list[float] | None = None,
    last_price: float | None = 100.0,
) -> GttOrder:
    return GttOrder(
        id=gtt_id,
        type="single",
        status="active",
        tradingsymbol=symbol,
        exchange="NSE",
        trigger_values=trigger if trigger is not None else [110.0],
        last_price=last_price,
        created_at="2026-01-01",
        orders=[],
    )


def _history(n: int = 200, *, seed: int = 0, vol: float = 0.02) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, vol, n)
    closes = 100.0 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"close": closes}, index=idx)


# ---------------------------------------------------------------------------
# simulate_target_hit — directional + bounds + determinism
# ---------------------------------------------------------------------------


def test_simulator_is_deterministic_with_seed() -> None:
    prob1, days1 = simulate_target_hit(
        start_price=100, target=110, mu_daily=0.0005, sigma_daily=0.02,
        horizon_days=60, n_paths=2000, seed=42,
    )
    prob2, days2 = simulate_target_hit(
        start_price=100, target=110, mu_daily=0.0005, sigma_daily=0.02,
        horizon_days=60, n_paths=2000, seed=42,
    )
    assert prob1 == prob2
    assert days1 == days2


def test_simulator_higher_vol_higher_hit_prob() -> None:
    """Same target distance, more volatility → more paths reach it."""
    p_low, _ = simulate_target_hit(
        start_price=100, target=110, mu_daily=0.0, sigma_daily=0.01,
        horizon_days=60, n_paths=5000, seed=7,
    )
    p_high, _ = simulate_target_hit(
        start_price=100, target=110, mu_daily=0.0, sigma_daily=0.03,
        horizon_days=60, n_paths=5000, seed=7,
    )
    assert p_high > p_low


def test_simulator_handles_downside_target() -> None:
    """Target below start → checks `<=`. With strong negative drift it should
    fire often; with positive drift it should fire less."""
    p_drift_down, _ = simulate_target_hit(
        start_price=100, target=90, mu_daily=-0.005, sigma_daily=0.02,
        horizon_days=60, n_paths=2000, seed=11,
    )
    p_drift_up, _ = simulate_target_hit(
        start_price=100, target=90, mu_daily=0.005, sigma_daily=0.02,
        horizon_days=60, n_paths=2000, seed=11,
    )
    assert p_drift_down > p_drift_up


def test_simulator_unreachable_target_low_prob() -> None:
    """Tiny vol + far-away target + flat drift → near-zero hit probability."""
    p, _ = simulate_target_hit(
        start_price=100, target=200, mu_daily=0.0, sigma_daily=0.001,
        horizon_days=60, n_paths=2000, seed=3,
    )
    assert p < 0.05


def test_simulator_inevitable_target_high_prob() -> None:
    """Tiny upward distance + high vol → most paths hit (Itô correction
    keeps it shy of 1.0 even with µ=0, hence the 0.85 floor)."""
    p, _ = simulate_target_hit(
        start_price=100, target=101, mu_daily=0.0, sigma_daily=0.04,
        horizon_days=60, n_paths=2000, seed=5,
    )
    assert p > 0.85


def test_simulator_returns_zero_on_degenerate_inputs() -> None:
    assert simulate_target_hit(100, 110, 0, 0, horizon_days=60, n_paths=100) == (0.0, None)
    assert simulate_target_hit(100, 110, 0, 0.02, horizon_days=0, n_paths=100) == (0.0, None)
    assert simulate_target_hit(0, 110, 0, 0.02, horizon_days=60, n_paths=100) == (0.0, None)


def test_simulator_no_hit_returns_none_for_days() -> None:
    p, days = simulate_target_hit(
        start_price=100, target=1_000_000, mu_daily=0.0, sigma_daily=0.01,
        horizon_days=10, n_paths=100, seed=1,
    )
    assert p == 0.0
    assert days is None


def test_expected_days_is_positive_and_within_horizon() -> None:
    p, days = simulate_target_hit(
        start_price=100, target=110, mu_daily=0.001, sigma_daily=0.025,
        horizon_days=60, n_paths=2000, seed=9,
    )
    assert p > 0.1
    assert 1 <= days <= 60  # type: ignore[operator]


# ---------------------------------------------------------------------------
# project_gtt_viability — wiring around the simulator
# ---------------------------------------------------------------------------


def test_project_gtt_viability_happy_path() -> None:
    df = _history(n=200, seed=42)
    gtt = _gtt(symbol="X", trigger=[df["close"].iloc[-1] * 1.05])
    v = project_gtt_viability(gtt, df, n_paths=1000, seed=123)
    assert v.note is None
    assert v.probability_hit is not None
    assert 0.0 <= v.probability_hit <= 1.0
    assert v.last_price is not None


def test_project_gtt_viability_insufficient_history() -> None:
    df = _history(n=20)
    v = project_gtt_viability(_gtt(), df, seed=1)
    assert v.probability_hit is None
    assert v.note is not None
    assert "insufficient" in v.note


def test_project_gtt_viability_no_trigger() -> None:
    df = _history()
    gtt = _gtt(trigger=[0.0])  # 0 is not usable
    v = project_gtt_viability(gtt, df)
    assert v.probability_hit is None
    assert v.note == "no usable trigger value"


def test_project_gtt_viability_zero_vol() -> None:
    """All-constant history → realised sigma 0 → graceful skip."""
    idx = pd.date_range("2024-01-01", periods=80, freq="B")
    df = pd.DataFrame({"close": [100.0] * 80}, index=idx)
    v = project_gtt_viability(_gtt(), df)
    assert v.probability_hit is None
    assert v.note == "zero realised volatility"


def test_project_gtt_viability_uses_last_price_when_present() -> None:
    df = _history()
    gtt = _gtt(last_price=120.0, trigger=[125.0])
    v = project_gtt_viability(gtt, df, seed=1)
    assert v.last_price == pytest.approx(120.0)


def test_project_gtt_viability_defaults_horizon() -> None:
    v = project_gtt_viability(_gtt(), _history())
    assert v.horizon_days == DEFAULT_HORIZON_DAYS


# ---------------------------------------------------------------------------
# project_all_gtts — orchestrator
# ---------------------------------------------------------------------------


def test_project_all_gtts_handles_missing_history() -> None:
    out = project_all_gtts([_gtt(symbol="HAS_DATA"), _gtt(gtt_id=2, symbol="MISSING")],
                            {"HAS_DATA": _history()}, seed=1)
    assert len(out) == 2
    by_sym = {v.symbol: v for v in out}
    assert by_sym["HAS_DATA"].probability_hit is not None
    assert by_sym["MISSING"].probability_hit is None
    assert by_sym["MISSING"].note == "no OHLCV history on disk"


def test_project_all_gtts_empty_input() -> None:
    assert project_all_gtts([], {}) == []


def test_project_all_gtts_passes_seed_to_each_call() -> None:
    """Same seed + same inputs → deterministic across runs."""
    df = _history(seed=1)
    gtts = [_gtt(symbol="X"), _gtt(gtt_id=2, symbol="Y")]
    out1 = project_all_gtts(gtts, {"X": df, "Y": df}, seed=42)
    out2 = project_all_gtts(gtts, {"X": df, "Y": df}, seed=42)
    assert [v.probability_hit for v in out1] == [v.probability_hit for v in out2]


def test_gtt_viability_dataclass_fields() -> None:
    """Snapshot test: ensure the dataclass keeps the fields the brief renders."""
    v = GttViability(gtt_id=1, symbol="X", type="single")
    assert v.trigger_values == []
    assert v.probability_hit is None
    assert v.note is None
