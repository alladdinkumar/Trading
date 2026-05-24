"""Reminder dispatcher for the Phase 17 Task Scheduler entries.

A `ReminderSlot` is a static row keyed by name in `SCHEDULE`. The CLI
command `trading remind --slot <name>` calls `fire_reminder` which:

1. Resolves today's date in IST.
2. Holiday-gates: if not a trading day, logs INFO and returns (no Slack).
3. Substitutes `<date>` in the slot body with today's ISO date.
4. Calls `ops.notify.notify("info", title, body)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Final

from loguru import logger

from trading.ops.calendar import is_trading_day
from trading.ops.notify import notify

_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class ReminderSlot:
    """A single scheduled reminder.

    `when` is informational — the wall-clock truth lives in the Task
    Scheduler XML entries under `docs/scheduler/`. Changing `when` here
    doesn't change when the reminder fires; it just keeps the spec
    documentation in sync.
    """

    when: str  # "HH:MM" IST
    title: str
    body: str = ""


SCHEDULE: Final[dict[str, ReminderSlot]] = {
    "pre_open_kite": ReminderSlot(
        "08:30", "\U0001f514 Pre-open step 1/4", "Run `/kite-snapshot` in Claude Code"
    ),
    "pre_open_scan": ReminderSlot(
        "08:35", "\U0001f514 Pre-open step 2/4", "Then `trading pre-open <date>`"
    ),
    "pre_open_analyst": ReminderSlot(
        "08:40", "\U0001f514 Pre-open step 3/4", "Run `/analyst` in Claude Code"
    ),
    "pre_open_compile": ReminderSlot(
        "08:45", "\U0001f514 Pre-open step 4/4", "Finally `trading brief compile <date>`"
    ),
    "iep_quotes": ReminderSlot("08:55", "\U0001f514 IEP step 1/2", "Run `/kite-quotes-snapshot`"),
    "iep_filter": ReminderSlot(
        "09:00", "\U0001f514 IEP step 2/2", "Then `trading pre-open-iep --date <date>`"
    ),
    "mid_day_prepare": ReminderSlot(
        "12:25", "\U0001f514 Mid-day step 1/3", "Run `trading mid-day <date>`"
    ),
    "mid_day_quotes": ReminderSlot(
        "12:30", "\U0001f514 Mid-day step 2/3", "Run `/kite-quotes-snapshot`"
    ),
    "mid_day_apply": ReminderSlot(
        "12:35", "\U0001f514 Mid-day step 3/3", "Then `trading mid-day <date> --apply`"
    ),
    "post_close_prepare": ReminderSlot(
        "16:05", "\U0001f514 Post-close step 1/3", "Run `trading post-close <date>`"
    ),
    "post_close_quotes": ReminderSlot(
        "16:10", "\U0001f514 Post-close step 2/3", "Run `/kite-quotes-snapshot`"
    ),
    "post_close_apply": ReminderSlot(
        "16:15", "\U0001f514 Post-close step 3/3", "Then `trading post-close <date> --apply`"
    ),
}


def _today_ist() -> date:
    """Today's date in Asia/Kolkata."""
    return datetime.now(_IST).date()


def fire_reminder(slot: str, today: date | None = None) -> None:
    """Look up `slot`, holiday-gate, substitute `<date>`, dispatch notify.

    Raises `KeyError` for unknown slot. Returns silently on non-trading
    days (no Slack message — operators enjoy their holiday).
    """
    spec = SCHEDULE[slot]
    today = today or _today_ist()
    if not is_trading_day(today):
        logger.info(f"reminder {slot} skipped: {today} is not a trading day")
        return
    body = spec.body.replace("<date>", today.isoformat())
    notify("info", spec.title, body)
