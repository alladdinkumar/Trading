"""End-to-end smoke checks for the scheduled reminders + their Slack messages.

These guard the failure mode that crashed scheduled tasks in production: a
reminder firing under Windows Task Scheduler and either (a) emitting emoji to a
cp1252 console and raising `UnicodeEncodeError`, or (b) being registered from a
Task Scheduler XML whose declared encoding doesn't match its bytes (so the task
never imports). Unlike the focused unit tests in `test_ops_runner` /
`test_ops_notify`, these exercise *every* slot through the real notify → Slack
render path and cross-check the scheduler XML on disk.
"""

from __future__ import annotations

import io
import json
import re
from datetime import date
from pathlib import Path

import pytest
import requests

from trading.cli import _force_utf8_io
from trading.ops import notify as notify_mod
from trading.ops import runner as runner_mod

_SCHEDULER_DIR = Path(__file__).resolve().parents[1] / "docs" / "scheduler"
_REMIND_XMLS = sorted(_SCHEDULER_DIR.glob("trading_remind_*.xml"))
# All registered task XMLs (everything except the pre-substitution scaffold).
_TASK_XMLS = sorted(p for p in _SCHEDULER_DIR.glob("*.xml") if p.name != "_TEMPLATE.xml")


def _read_xml(path: Path) -> str:
    """Decode a Task Scheduler XML by its actual byte encoding (BOM-sniffed)."""
    raw = path.read_bytes()
    encoding = "utf-16" if raw[:2] == b"\xff\xfe" else "utf-8"
    return raw.decode(encoding)


def _declared_encoding(text: str) -> str:
    m = re.search(r'encoding="([^"]+)"', text)
    assert m is not None, "XML declaration missing an encoding attribute"
    return m.group(1).upper()


def _actual_encoding(path: Path) -> str:
    return "UTF-16" if path.read_bytes()[:2] == b"\xff\xfe" else "UTF-8"


# ---------------------------------------------------------------------------
# Every slot fires and produces an encodable Slack message
# ---------------------------------------------------------------------------


def test_every_slot_fires_through_real_slack_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire all 13 slots through the real `notify` → `post_slack` path.

    Only the network boundary (`requests.post`) and the Windows toast backend
    are stubbed; the Slack message string is built for real. Asserts no slot
    raises, each emits exactly one payload, `<date>` is substituted, and the
    payload is UTF-8 / JSON encodable (what actually goes over the wire).
    """
    payloads: list[dict[str, object]] = []

    def fake_post(url: str, json: dict[str, object], timeout: int):
        payloads.append(json)

        class _Resp:
            def raise_for_status(self) -> None:
                return None

        return _Resp()

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(notify_mod, "_plyer_notification", None)  # toast → no-op
    monkeypatch.setattr(runner_mod, "is_trading_day", lambda d: True)
    notify_mod._reset_warned()

    for slot in runner_mod.SCHEDULE:
        runner_mod.fire_reminder(slot, today=date(2026, 6, 17))

    assert len(payloads) == len(runner_mod.SCHEDULE)
    for payload in payloads:
        text = payload["text"]
        assert isinstance(text, str) and text
        assert "<date>" not in text
        text.encode("utf-8")  # must not raise
        json.dumps(payload).encode("utf-8")  # the actual POST body


def test_slot_titles_carry_emoji_that_break_cp1252() -> None:
    """Sanity: at least one slot title really does carry a non-cp1252 glyph.

    If this ever stops being true the encoding-regression test below would
    pass vacuously.
    """
    titles = "".join(slot.title for slot in runner_mod.SCHEDULE.values())
    with pytest.raises(UnicodeEncodeError):
        titles.encode("cp1252")


# ---------------------------------------------------------------------------
# The production crash: emoji to a cp1252 console
# ---------------------------------------------------------------------------


def test_force_utf8_io_protects_emoji_console_writes() -> None:
    """`_force_utf8_io` makes a cp1252-backed stream emoji-safe.

    Regression for the scheduled-task crash: the bell/₹/→ glyphs the CLI emits
    raise `UnicodeEncodeError` on a cp1252 console; the `_main` callback
    reconfigures stdout/stderr to UTF-8 so `uv run trading remind …` never
    crashes regardless of the console code page.
    """
    emoji_title = runner_mod.SCHEDULE["pre_open_kite"].title
    assert "\U0001f514" in emoji_title  # 🔔

    # Before: a cp1252 stream cannot encode the bell emoji.
    broken = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    with pytest.raises(UnicodeEncodeError):
        broken.write(emoji_title)
        broken.flush()

    # After reconfiguring to UTF-8 the same write succeeds and round-trips.
    raw = io.BytesIO()
    fixed = io.TextIOWrapper(raw, encoding="cp1252")
    _force_utf8_io(fixed)
    fixed.write(emoji_title)
    fixed.flush()
    assert "\U0001f514".encode() in raw.getvalue()


def test_force_utf8_io_skips_streams_without_reconfigure() -> None:
    """Capture buffers / plain objects without `reconfigure` are ignored, not crashed."""

    class _NoReconfigure:
        pass

    _force_utf8_io(_NoReconfigure())  # must not raise


# ---------------------------------------------------------------------------
# Scheduler XML ↔ SCHEDULE drift + encoding integrity
# ---------------------------------------------------------------------------


def test_every_remind_xml_targets_a_real_slot() -> None:
    """Each `trading remind --slot X` task points at a slot that still exists."""
    xml_slots: set[str] = set()
    for path in _REMIND_XMLS:
        text = _read_xml(path)
        m = re.search(r"--slot\s+([A-Za-z0-9_]+)", text)
        assert m is not None, f"{path.name} has no --slot argument"
        xml_slots.add(m.group(1))
    orphans = xml_slots - set(runner_mod.SCHEDULE)
    assert orphans == set(), f"scheduled tasks reference unknown slots: {orphans}"


def test_every_slot_has_a_scheduler_xml() -> None:
    """Every SCHEDULE slot is actually wired to a Task Scheduler XML."""
    xml_slots = {
        m.group(1)
        for path in _REMIND_XMLS
        if (m := re.search(r"--slot\s+([A-Za-z0-9_]+)", _read_xml(path)))
    }
    missing = set(runner_mod.SCHEDULE) - xml_slots
    assert missing == set(), f"slots with no scheduler XML: {missing}"


@pytest.mark.parametrize("path", _TASK_XMLS, ids=lambda p: p.name)
def test_scheduler_xml_declared_encoding_matches_bytes(path: Path) -> None:
    """The `encoding="…"` declaration must match the file's real bytes.

    Task Scheduler honours the declaration on import; a UTF-8 file that claims
    `encoding="UTF-16"` fails to register, silently dropping that schedule (the
    bug found in `trading_remind_monthly_sip.xml` / `trading_weekly_train.xml`).
    """
    text = _read_xml(path)
    assert _declared_encoding(text) == _actual_encoding(path), (
        f"{path.name}: declares {_declared_encoding(text)} but stored as {_actual_encoding(path)}"
    )


@pytest.mark.parametrize("path", _TASK_XMLS, ids=lambda p: p.name)
def test_scheduler_xml_runs_from_project_dir(path: Path) -> None:
    """Every task cd's into the project before `uv run trading …`.

    `cd /d "D:\\Projects\\Trading"` is what lets the scheduled `uv run` resolve
    the project; without it the task runs from system32 and `trading` isn't found.
    """
    text = _read_xml(path)
    assert "uv run trading" in text
    assert 'cd /d "D:\\Projects\\Trading"' in text
