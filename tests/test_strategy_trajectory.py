"""Tests for the in-flight trade-trajectory classifier (F-039)."""

from __future__ import annotations

import math

from trading.strategy.trajectory import trade_trajectory


def _t(mark: float, days_held: int):
    return trade_trajectory(
        entry=100.0, target=120.0, mark=mark, days_held=days_held, horizon_days=25
    )


def test_progress_and_time_fractions() -> None:
    traj = _t(110.0, 12)  # halfway to target, ~half the horizon elapsed
    assert math.isclose(traj.progress_to_target, 0.5, abs_tol=1e-9)
    assert math.isclose(traj.time_elapsed_frac, 12 / 25, abs_tol=1e-9)


def test_underwater_is_lagging() -> None:
    # Below entry → moving away from target.
    assert _t(95.0, 5).status == "LAGGING"


def test_at_or_through_target_is_ahead() -> None:
    assert _t(120.0, 5).status == "AHEAD"
    assert _t(125.0, 5).status == "AHEAD"


def test_flat_while_clock_runs_is_stalling() -> None:
    # Barely any price progress but most of the horizon gone.
    assert _t(101.0, 20).status == "STALLING"


def test_beating_the_clock_is_ahead() -> None:
    # 50% of the move in ~48% of the time → pace > 1.
    assert _t(110.0, 12).status == "AHEAD"


def test_keeping_pace_is_on_track() -> None:
    # 20% of the move in ~48% of the time → pace ~0.42 (>= 0.4).
    assert _t(104.0, 12).status == "ON_TRACK"


def test_day_zero_is_on_track() -> None:
    traj = _t(100.0, 0)
    assert traj.time_elapsed_frac == 0.0
    assert traj.pace is None
    assert traj.status == "ON_TRACK"


def test_degenerate_target_equals_entry_does_not_crash() -> None:
    traj = trade_trajectory(
        entry=100.0, target=100.0, mark=100.0, days_held=5, horizon_days=25
    )
    assert traj.progress_to_target == 0.0
    assert traj.status in {"ON_TRACK", "STALLING", "LAGGING", "AHEAD"}
