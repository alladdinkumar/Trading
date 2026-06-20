"""Tests for the score→win-rate calibration that closes the loop into p_win (F-041)."""

from __future__ import annotations

from trading.strategy.calibration import build_score_calibration, calibrated_p_win


def test_empty_calibration_falls_back_to_raw_score() -> None:
    cal = build_score_calibration([], min_n=3)
    # No observations → trust the model's own score.
    assert calibrated_p_win(cal, 0.62) == 0.62
    # No score at all → prior.
    assert calibrated_p_win(cal, None, prior=0.5) == 0.5


def test_well_populated_bin_overrides_optimistic_score() -> None:
    # The model says ~0.7 for this band, but only 2 of 10 actually won.
    obs = [(0.72, i < 2) for i in range(10)]
    cal = build_score_calibration(obs, n_bins=5, min_n=5)
    # 0.72 lands in the [0.6, 0.8) bin → realized hit-rate 0.2, not 0.72.
    assert calibrated_p_win(cal, 0.72) == 0.2


def test_thin_bin_below_min_n_falls_back_to_raw_score() -> None:
    obs = [(0.72, True), (0.74, False)]  # only 2 samples in the bin
    cal = build_score_calibration(obs, n_bins=5, min_n=5)
    # Not enough evidence to override → keep the raw score.
    assert calibrated_p_win(cal, 0.73) == 0.73


def test_bins_are_independent() -> None:
    obs = [(0.12, False)] * 6 + [(0.85, True)] * 6
    cal = build_score_calibration(obs, n_bins=5, min_n=5)
    assert calibrated_p_win(cal, 0.10) == 0.0  # low bin: never won
    assert calibrated_p_win(cal, 0.90) == 1.0  # high bin: always won
