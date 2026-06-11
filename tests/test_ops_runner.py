"""Tests for src/trading/ops/runner.py."""

from __future__ import annotations

from datetime import date

import pytest


def test_schedule_has_13_slots():
    from trading.ops.runner import SCHEDULE

    assert len(SCHEDULE) == 13


def test_schedule_slot_names():
    from trading.ops.runner import SCHEDULE

    expected = {
        "pre_open_kite",
        "pre_open_scan",
        "pre_open_analyst",
        "pre_open_compile",
        "iep_quotes",
        "iep_filter",
        "monthly_sip",
        "mid_day_prepare",
        "mid_day_quotes",
        "mid_day_apply",
        "post_close_prepare",
        "post_close_quotes",
        "post_close_apply",
    }
    assert set(SCHEDULE.keys()) == expected


def test_schedule_times_are_sorted():
    from trading.ops.runner import SCHEDULE

    times = [slot.when for slot in SCHEDULE.values()]
    assert times == sorted(times)


def test_reminder_slot_is_frozen():
    from dataclasses import FrozenInstanceError

    from trading.ops.runner import ReminderSlot

    slot = ReminderSlot(when="08:30", title="t", body="b")
    with pytest.raises(FrozenInstanceError):
        slot.when = "09:00"  # type: ignore[misc]


def test_fire_reminder_holiday_short_circuits(monkeypatch):
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: False)
    monkeypatch.setattr(
        runner,
        "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    runner.fire_reminder("pre_open_kite", today=date(2026, 1, 26))
    assert calls == []


def test_fire_reminder_substitutes_date(monkeypatch):
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: True)
    monkeypatch.setattr(
        runner,
        "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    runner.fire_reminder("pre_open_scan", today=date(2026, 5, 25))
    assert len(calls) == 1
    _, _, body = calls[0]
    assert "2026-05-25" in body
    assert "<date>" not in body


def test_fire_reminder_unknown_slot_raises(monkeypatch):
    from trading.ops import runner

    monkeypatch.setattr(runner, "is_trading_day", lambda d: True)
    monkeypatch.setattr(runner, "notify", lambda *a, **kw: None)
    with pytest.raises(KeyError):
        runner.fire_reminder("does_not_exist", today=date(2026, 5, 25))


def test_fire_reminder_uses_today_when_no_arg(monkeypatch):
    from trading.ops import runner

    monkeypatch.setattr(runner, "_today_ist", lambda: date(2026, 5, 25))
    monkeypatch.setattr(runner, "is_trading_day", lambda d: True)
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        runner,
        "notify",
        lambda level, title, body="": captured.update(body=body),
    )

    runner.fire_reminder("pre_open_scan")
    assert "2026-05-25" in captured["body"]


def test_monthly_sip_slot_bypasses_holiday_gate(monkeypatch):
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: False)
    monkeypatch.setattr(
        runner,
        "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )
    runner.fire_reminder("monthly_sip", today=date(2026, 8, 1))  # a Saturday
    assert len(calls) == 1
    assert "2026-08-01" in calls[0][2]


def test_gate_holidays_defaults_true():
    from trading.ops.runner import SCHEDULE

    gated = [name for name, slot in SCHEDULE.items() if slot.gate_holidays]
    assert "monthly_sip" not in gated
    assert len(gated) == 12  # every pre-existing slot stays gated
