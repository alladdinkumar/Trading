"""Canonical IST clock — the single source of truth for "now" / "today".

India observes no daylight-saving time, so a fixed ``+05:30`` offset is exact
year-round and needs no ``tzdata`` lookup (which is unreliable on Windows).

Jobs, ops, and skills should import :data:`IST`, :func:`now_ist`, and
:func:`today_ist` from here rather than re-deriving the offset locally — that
drift is what finding F-004 flagged. ``freeze_time`` patches ``datetime.now``
globally, so freezegun-based tests control these helpers without any seam.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: Asia/Kolkata as a fixed UTC offset (no DST). Use this for ``tzinfo=`` and
#: ``astimezone()`` so every module shares one timezone object.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Current timezone-aware datetime in Asia/Kolkata."""
    return datetime.now(IST)


def today_ist() -> date:
    """Today's calendar date in Asia/Kolkata (rolls at IST midnight, not UTC)."""
    return now_ist().date()
