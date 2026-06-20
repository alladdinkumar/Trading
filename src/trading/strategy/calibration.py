"""Score→win-rate calibration — the self-healing edge that closes the loop (F-041).

The ranker's `ml_score` is a model probability; the weekly review repeatedly
*measures* how well it matches realised wins but nothing ever fed that back. This
pure module turns matured outcomes into a calibration map (realised hit-rate per
`ml_score` band) and corrects the EV planner's `p_win` with it: a band the model
is over-confident about gets pulled toward its true realised rate. Thin bands
(below `min_n`) fall back to the raw score — we only override the model where
there's enough evidence to trust the correction over its own opinion.

Pure: no DB, no network. The caller supplies `(ml_score, won)` observations.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_N_BINS = 5
DEFAULT_MIN_N = 5
DEFAULT_PRIOR = 0.5


@dataclass(frozen=True)
class ScoreBin:
    lo: float
    hi: float
    n: int
    hit_rate: float  # wins / n in this band


@dataclass(frozen=True)
class ScoreCalibration:
    """Realised hit-rate per equal-width `ml_score` band over [0, 1]."""

    bins: tuple[ScoreBin, ...]
    min_n: int

    def bin_for(self, score: float) -> ScoreBin | None:
        for b in self.bins:
            # Upper-inclusive only on the final band so 1.0 has a home.
            if b.lo <= score < b.hi or (score == b.hi == 1.0):
                return b
        return None


def build_score_calibration(
    observations: list[tuple[float, bool]],
    *,
    n_bins: int = DEFAULT_N_BINS,
    min_n: int = DEFAULT_MIN_N,
) -> ScoreCalibration:
    """Bucket `(ml_score, won)` pairs into `n_bins` equal-width bands over [0, 1].

    Each band records its sample count and realised hit-rate. Bands with fewer
    than `min_n` samples are kept (for visibility) but `calibrated_p_win` will
    not override the raw score from them.
    """
    width = 1.0 / n_bins
    counts = [0] * n_bins
    wins = [0] * n_bins
    for score, won in observations:
        clamped = min(max(score, 0.0), 1.0)
        idx = min(int(clamped / width), n_bins - 1)
        counts[idx] += 1
        if won:
            wins[idx] += 1
    bins = tuple(
        ScoreBin(
            lo=i * width,
            hi=(i + 1) * width,
            n=counts[i],
            hit_rate=(wins[i] / counts[i]) if counts[i] else 0.0,
        )
        for i in range(n_bins)
    )
    return ScoreCalibration(bins=bins, min_n=min_n)


def calibrated_p_win(
    cal: ScoreCalibration | None,
    ml_score: float | None,
    *,
    prior: float = DEFAULT_PRIOR,
) -> float:
    """Correct `ml_score` with realised hit-rate where there's enough evidence.

    - `ml_score is None` (cold-start) → `prior`.
    - the score's band has ≥ `min_n` samples → that band's realised hit-rate.
    - otherwise (no map, or a thin band) → the raw `ml_score`.
    """
    if ml_score is None:
        return prior
    if cal is None:
        return ml_score
    b = cal.bin_for(ml_score)
    if b is not None and b.n >= cal.min_n:
        return b.hit_rate
    return ml_score
