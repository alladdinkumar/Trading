"""Paper Journal schedule helpers — bought / expected-target / deviation.

Pure, trading-day-based date math (`numpy.busday_offset` / `busday_count`),
consistent with the engine's time-stop and `mtm._days_held`. No DB or network.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np


def _as_date(iso: str) -> date:
    """Parse the date part of an ISO date or full timestamp."""
    return datetime.fromisoformat(iso).date()


def expected_target_date(entry_iso: str, horizon_days: int) -> date:
    """Bought date + `horizon_days` trading days (rolled forward off weekends)."""
    entry = _as_date(entry_iso)
    shifted = np.busday_offset(np.datetime64(entry, "D"), horizon_days, roll="forward")
    return shifted.astype("datetime64[D]").astype(date)


def deviation_label(target: date, *, exit_iso: str | None, as_of: date) -> str:
    """Signed trading-day deviation from the expected target date.

    Closed (`exit_iso` set): "-Nd early" / "+Nd late" / "on time".
    Open  (`exit_iso` None): "Nd left" (on/before target) / "+Nd overdue".
    """
    t = np.datetime64(target, "D")
    if exit_iso is not None:
        exit_d = np.datetime64(_as_date(exit_iso), "D")
        if exit_d < t:
            return f"-{int(np.busday_count(exit_d, t))}d early"
        if exit_d > t:
            return f"+{int(np.busday_count(t, exit_d))}d late"
        return "on time"
    a = np.datetime64(as_of, "D")
    if a > t:
        return f"+{int(np.busday_count(t, a))}d overdue"
    return f"{int(np.busday_count(a, t))}d left"
