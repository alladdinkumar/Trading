"""Tests for src/trading/ops/runner.py."""

from __future__ import annotations

from datetime import date

import pytest


def test_schedule_has_12_slots():
    from trading.ops.runner import SCHEDULE

    assert len(SCHEDULE) == 12


def test_schedule_slot_names():
    from trading.ops.runner import SCHEDULE

    expected = {
        "pre_open_kite", "pre_open_scan", "pre_open_analyst", "pre_open_compile",
        "iep_quotes", "iep_filter",
        "mid_day_prepare", "mid_day_quotes", "mid_day_apply",
        "post_close_prepare", "post_close_quotes", "post_close_apply",
    }
    assert set(SCHEDULE.keys()) == expected


def test_schedule_times_are_sorted():
    from trading.ops.runner import SCHEDULE

    times = [slot.when for slot in SCHEDULE.values()]
    assert times == sorted(times)


def test_reminder_slot_is_frozen():
    from trading.ops.runner import ReminderSlot

    slot = ReminderSlot(when="08:30", title="t", body="b")
    with pytest.raises(Exception):
        slot.when = "09:00"  # type: ignore[misc]
