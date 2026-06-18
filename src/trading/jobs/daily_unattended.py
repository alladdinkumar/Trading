"""Phase 19 — unattended broker-free daily gap-filler (F-032).

The daily pipeline is otherwise human-run: a day the operator is unavailable
leaves no macro snapshot, no scan, no bundle, and a hole in the track record.
This job runs the **broker-free spine** of `pre_open` (macro, sector, news,
OHLCV refresh, scan, rank, auto-open, bundle) unattended — like `weekly_train`
— so the macro/scan/track-record spine stays continuous on missed days.

It is a *gap-filler*, not a duplicate of the operator's flow:
- holiday-gated (`is_trading_day`);
- skips when the operator (or an earlier run) already produced today's bundle,
  detected via the same on-disk artifact probe `trading status` uses
  (`run_status` — the `pre_open_scan` checkpoint / `_context.md`); `force=True`
  overrides;
- otherwise calls `run_pre_open(require_snapshot=False)` so a missing Kite
  snapshot degrades to "no holdings health" instead of aborting.

**Scope:** this keeps the *pre-open spine* continuous. MTM / exit-management of
open trades still needs live Kite quotes and stays in the interactive flow — a
missed afternoon is not back-filled here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading.config import Paths, Settings, get_paths
from trading.jobs.pre_open import PreOpenResult, run_pre_open
from trading.ops.calendar import is_trading_day
from trading.ops.notify import notify
from trading.ops.run_status import compute_status


@dataclass(frozen=True)
class DailyUnattendedResult:
    """What the gap-filler did for `as_of`.

    `ran=True` means the broker-free pre_open spine executed (and `pre_open`
    carries its result); `ran=False` means it was skipped, with `skipped_reason`
    explaining why (non-trading day, or the spine had already run).
    """

    as_of: date
    ran: bool
    skipped_reason: str | None
    pre_open: PreOpenResult | None


def _spine_already_ran(paths: Paths, as_of: date) -> bool:
    """True if the pre-open scan checkpoint already produced today's bundle.

    Reuses the `run_status` artifact probe (the `pre_open_scan` step → presence
    of `_context.md`) so this and `trading status` agree on what "ran" means.
    """
    statuses = compute_status(paths, as_of)
    return any(s.key == "pre_open_scan" and s.ran for s in statuses)


def run_daily_unattended(
    as_of: date,
    *,
    paths: Paths | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> DailyUnattendedResult:
    """Run the broker-free pre_open spine unattended, as a missed-day gap-filler.

    Skips silently on non-trading days and on days the operator already ran the
    spine (unless `force`). Otherwise runs `pre_open` with `require_snapshot=False`
    and posts an info notification so the gap-fill leaves a trace.
    """
    p = paths if paths is not None else get_paths()

    if not is_trading_day(as_of):
        return DailyUnattendedResult(as_of, False, "non-trading day", None)

    if not force and _spine_already_ran(p, as_of):
        return DailyUnattendedResult(
            as_of, False, "pre-open spine already ran today", None
        )

    result = run_pre_open(as_of, paths=p, settings=settings, require_snapshot=False)
    notify(
        "info",
        f"📈 Unattended daily run {as_of.isoformat()}",
        (
            f"{result.candidates_passing} passing, "
            f"{result.paper_trades_opened} paper-trades opened, "
            f"{len(result.warnings)} warning(s)"
        ),
    )
    return DailyUnattendedResult(as_of, True, None, result)
