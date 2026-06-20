"""Canonical IST clock (F-004)."""

from datetime import date, timedelta

from freezegun import freeze_time

from trading.clock import IST, now_ist, today_ist


def test_ist_is_fixed_530_offset() -> None:
    # India has no DST, so the offset is a constant +05:30 year-round.
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


@freeze_time("2026-06-20 06:00:00")  # 06:00 UTC == 11:30 IST
def test_now_ist_is_timezone_aware_in_ist() -> None:
    now = now_ist()
    assert now.tzinfo is IST
    assert (now.hour, now.minute) == (11, 30)


@freeze_time("2026-06-19 20:00:00")  # 20:00 UTC == 01:30 IST *next* day
def test_today_ist_uses_the_ist_calendar_date_not_utc() -> None:
    # The host/UTC date is still the 19th; in IST it has already rolled to the 20th.
    assert today_ist() == date(2026, 6, 20)
