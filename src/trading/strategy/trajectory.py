"""In-flight trade-trajectory classifier (F-039).

The exit machine (`strategy.exits`) only answers *"did stop/target/time fire?"*,
and the Paper Journal's `deviation_label` is calendar-only. Neither says whether an
open position is actually *progressing toward the target we intended*. This pure
module fills that gap: given the current mark and how far into the horizon we are,
it reports price-progress, time-elapsed, the pace ratio between them, and a
plain-language status — so a position stalling at −8% with "2d left" is visibly
**LAGGING**, not silently "on schedule".

Pure: no DB, no network. The caller (MTM loop) supplies entry/target/mark/days.
LONG-only, matching the rest of the system (`side="LONG"`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TrajectoryStatus = Literal["AHEAD", "ON_TRACK", "STALLING", "LAGGING"]

# Pace = progress / time_elapsed. ≥1 means price is beating the clock; below this
# floor the position is barely moving while the horizon burns.
PACE_AHEAD = 1.0
PACE_ON_TRACK_FLOOR = 0.4


@dataclass(frozen=True)
class TradeTrajectory:
    progress_to_target: float  # (mark − entry) / (target − entry); <0 underwater, ≥1 at target
    time_elapsed_frac: float  # days_held / horizon_days
    pace: float | None  # progress / time_frac; None on the entry day (time_frac == 0)
    status: TrajectoryStatus


def trade_trajectory(
    *,
    entry: float,
    target: float,
    mark: float,
    days_held: int,
    horizon_days: int,
) -> TradeTrajectory:
    """Classify how an open LONG position is tracking toward its target.

    - **LAGGING** — underwater (price has moved away from the target).
    - **AHEAD** — at/through target, or price progress outrunning the clock.
    - **ON_TRACK** — keeping reasonable pace with the elapsed horizon.
    - **STALLING** — positive but well behind pace; the clock is winning.
    """
    span = target - entry
    progress = (mark - entry) / span if span > 0 else 0.0
    time_frac = days_held / horizon_days if horizon_days > 0 else 0.0

    if progress < 0:
        status: TrajectoryStatus = "LAGGING"
        pace: float | None = progress / time_frac if time_frac > 0 else None
    elif progress >= 1.0:
        status = "AHEAD"
        pace = progress / time_frac if time_frac > 0 else None
    elif time_frac <= 0:
        status = "ON_TRACK"  # entry day — nothing has had time to lag yet
        pace = None
    else:
        pace = progress / time_frac
        if pace >= PACE_AHEAD:
            status = "AHEAD"
        elif pace >= PACE_ON_TRACK_FLOOR:
            status = "ON_TRACK"
        else:
            status = "STALLING"

    return TradeTrajectory(
        progress_to_target=progress,
        time_elapsed_frac=time_frac,
        pace=pace,
        status=status,
    )
