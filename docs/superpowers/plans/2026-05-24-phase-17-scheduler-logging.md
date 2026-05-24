# Phase 17 — Task Scheduler + logging (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap the four daily jobs with rotating loguru logs, a Slack + Windows-toast notification primitive, an NSE-holiday gate, twelve Task Scheduler reminder slots, and an operations runbook.

**Architecture:** New `src/trading/ops/` subpackage with four small modules (`notify`, `calendar`, `logging_setup`, `runner`). Two new CLI subcommands (`remind`, `notify-test`). A two-line shim added to each existing job entrypoint. Twelve Windows Task Scheduler XML exports plus a Markdown runbook. Notifications are best-effort — they never crash the underlying job.

**Tech Stack:** Python 3.11, loguru, plyer (new dep), nsepython (existing), requests (existing), Typer, pytest, freezegun, Windows Task Scheduler.

**Spec:** [`docs/superpowers/specs/2026-05-24-phase-17-scheduler-logging-design.md`](../specs/2026-05-24-phase-17-scheduler-logging-design.md)

---

## File map

**Create:**
- `src/trading/ops/__init__.py` — exports `notify`, `is_trading_day`, `configure_logging`, `fire_reminder`, `SCHEDULE`.
- `src/trading/ops/notify.py` — Slack + toast + dispatcher.
- `src/trading/ops/calendar.py` — `is_trading_day`, `nse_holidays`.
- `src/trading/ops/logging_setup.py` — loguru configuration.
- `src/trading/ops/runner.py` — `ReminderSlot` dataclass, `SCHEDULE` dict, `fire_reminder`.
- `data/static/nse_holidays_2026.json` — bundled fallback holiday list.
- `tests/test_ops_notify.py`, `tests/test_ops_calendar.py`, `tests/test_ops_logging_setup.py`, `tests/test_ops_runner.py`.
- `docs/scheduler/trading_remind_<slot>.xml` × 12 files.
- `docs/operations.md`.

**Modify:**
- `pyproject.toml` — add `plyer>=2.1`.
- `src/trading/config.py` — add `slack_webhook_url` to `Settings`.
- `src/trading/cli.py` — add `remind` and `notify-test` subcommands.
- `src/trading/jobs/pre_open.py` — wrap `_main` with `configure_logging` + try/except.
- `src/trading/jobs/pre_open_iep.py` — same.
- `src/trading/jobs/mid_day.py` — same.
- `src/trading/jobs/post_close.py` — same.
- `.env.example` — add `SLACK_WEBHOOK_URL=` line.
- `tests/test_cli.py` — extend with `remind` and `notify-test` cases.
- `PROGRESS.md` — mark items 17.1–17.6 done.

---

## Task 1 — Add plyer dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`. In the `[project] dependencies` list, after the existing `"loguru>=0.7",` line and before `"pydantic>=2.7",`, add:

```toml
    "plyer>=2.1",                # cross-platform notifications (Windows toast)
```

- [ ] **Step 2: Sync**

Run: `uv sync`
Expected: Resolves and installs plyer + transitive deps. No errors.

- [ ] **Step 3: Smoke import**

Run: `uv run python -c "from plyer import notification; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add plyer for Windows toast notifications"
```

---

## Task 2 — Scaffold the `ops/` subpackage

**Files:**
- Create: `src/trading/ops/__init__.py`

- [ ] **Step 1: Create the empty package**

Write `src/trading/ops/__init__.py` with this content:

```python
"""Phase 17 — operations layer.

Notification primitives, NSE calendar, loguru configuration, and the
Task Scheduler reminder dispatcher. Public exports below are populated
as the submodules are added in Tasks 3-12.
"""

from __future__ import annotations
```

- [ ] **Step 2: Verify importable**

Run: `uv run python -c "import trading.ops; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/trading/ops/__init__.py
git commit -m "feat(ops): scaffold ops subpackage (Phase 17)"
```

---

## Task 3 — Settings: add `slack_webhook_url`

**Files:**
- Modify: `src/trading/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (at the bottom):

```python
def test_settings_reads_slack_webhook_url(monkeypatch):
    from trading.config import get_settings

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    s = get_settings(load_dotenv=False)
    assert s.slack_webhook_url == "https://hooks.slack.com/services/T/B/X"


def test_settings_slack_webhook_url_missing_is_none(monkeypatch):
    from trading.config import get_settings

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    s = get_settings(load_dotenv=False)
    assert s.slack_webhook_url is None
```

- [ ] **Step 2: Run test, confirm it fails**

Run: `uv run pytest tests/test_config.py::test_settings_reads_slack_webhook_url -v`
Expected: FAIL — `Settings` has no field `slack_webhook_url`.

- [ ] **Step 3: Add the field**

Edit `src/trading/config.py`. In the `Settings` dataclass (around line 38-47), add `slack_webhook_url: str | None` after `kite_access_token`:

```python
@dataclass(frozen=True)
class Settings:
    """Secrets and runtime config loaded from environment."""

    anthropic_api_key: str | None
    kite_api_key: str | None
    kite_api_secret: str | None
    kite_access_token: str | None
    slack_webhook_url: str | None
    log_level: str
    news_user_agent: str
```

In `get_settings` (around line 96-103), add the env read between `kite_access_token` and `log_level`:

```python
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        kite_api_key=os.environ.get("KITE_API_KEY") or None,
        kite_api_secret=os.environ.get("KITE_API_SECRET") or None,
        kite_access_token=os.environ.get("KITE_ACCESS_TOKEN") or None,
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL") or None,
        log_level=os.environ.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL,
        news_user_agent=os.environ.get("NEWS_USER_AGENT") or DEFAULT_NEWS_USER_AGENT,
    )
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS for both new tests AND all existing tests.

- [ ] **Step 5: Extend `.env.example`**

Append to `.env.example`:

```
# Slack incoming webhook for Phase 17 reminders + failure alerts.
# Create a Slack app → Incoming Webhooks → enable → paste URL here.
SLACK_WEBHOOK_URL=
```

- [ ] **Step 6: Commit**

```bash
git add src/trading/config.py .env.example tests/test_config.py
git commit -m "feat(config): SLACK_WEBHOOK_URL setting (Phase 17)"
```

---

## Task 4 — `ops/notify.py`: `post_slack` primitive

**Files:**
- Create: `src/trading/ops/notify.py`
- Test: `tests/test_ops_notify.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ops_notify.py`:

```python
"""Tests for src/trading/ops/notify.py."""

from __future__ import annotations

import pytest
import requests


def test_post_slack_posts_when_url_set(monkeypatch):
    from trading.ops import notify as notify_mod

    posted: dict[str, object] = {}

    def fake_post(url, json, timeout):
        posted["url"] = url
        posted["json"] = json
        posted["timeout"] = timeout

        class R:
            status_code = 200

            def raise_for_status(self):
                return None

        return R()

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    monkeypatch.setattr(requests, "post", fake_post)
    ok = notify_mod.post_slack("hello")
    assert ok is True
    assert posted["url"] == "https://hooks.slack.com/services/T/B/X"
    assert posted["json"] == {"text": "hello"}
    assert posted["timeout"] == 5


def test_post_slack_returns_false_when_url_missing(monkeypatch):
    from trading.ops import notify as notify_mod

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    notify_mod._reset_warned()  # private; resets the once-per-process warn flag
    ok = notify_mod.post_slack("hello")
    assert ok is False


def test_post_slack_returns_false_on_network_error(monkeypatch):
    from trading.ops import notify as notify_mod

    def boom(*a, **kw):
        raise requests.ConnectionError("dns failed")

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/x")
    monkeypatch.setattr(requests, "post", boom)
    ok = notify_mod.post_slack("hello")
    assert ok is False
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `uv run pytest tests/test_ops_notify.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `src/trading/ops/notify.py`:

```python
"""Notification primitives — Slack incoming webhook + Windows toast.

Both channels are best-effort. Failures are caught and surfaced as
WARN-level loguru logs but never propagated; the underlying job is the
source of truth, and notification is informational only.
"""

from __future__ import annotations

import os
from typing import Literal

import requests
from loguru import logger

_SLACK_TIMEOUT_S = 5
_warned_missing_webhook = False


def _reset_warned() -> None:
    """Test helper — reset the once-per-process warning flag."""
    global _warned_missing_webhook
    _warned_missing_webhook = False


def post_slack(text: str) -> bool:
    """POST `text` to SLACK_WEBHOOK_URL. Returns True on 2xx, False otherwise.

    Never raises — network errors, missing env, and non-2xx responses
    all result in a WARN log and `return False`.
    """
    global _warned_missing_webhook
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        if not _warned_missing_webhook:
            logger.warning("SLACK_WEBHOOK_URL not set — Slack notifications disabled")
            _warned_missing_webhook = True
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=_SLACK_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Slack post failed: {e}")
        return False
    return True
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_ops_notify.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/trading/ops/notify.py tests/test_ops_notify.py
git commit -m "feat(ops): Slack webhook primitive (Phase 17)"
```

---

## Task 5 — `ops/notify.py`: `post_toast` primitive

**Files:**
- Modify: `src/trading/ops/notify.py`
- Modify: `tests/test_ops_notify.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ops_notify.py`:

```python
def test_post_toast_calls_plyer(monkeypatch):
    from trading.ops import notify as notify_mod

    captured: dict[str, object] = {}

    class FakeNotification:
        def notify(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(notify_mod, "_plyer_notification", FakeNotification())
    ok = notify_mod.post_toast("hello", "world")
    assert ok is True
    assert captured["title"] == "hello"
    assert captured["message"] == "world"
    assert captured["timeout"] == 10


def test_post_toast_returns_false_when_plyer_raises(monkeypatch):
    from trading.ops import notify as notify_mod

    class BrokenNotification:
        def notify(self, **kwargs):
            raise RuntimeError("no notification backend")

    monkeypatch.setattr(notify_mod, "_plyer_notification", BrokenNotification())
    ok = notify_mod.post_toast("t", "m")
    assert ok is False


def test_post_toast_truncates_long_message(monkeypatch):
    from trading.ops import notify as notify_mod

    captured: dict[str, object] = {}

    class Fake:
        def notify(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(notify_mod, "_plyer_notification", Fake())
    notify_mod.post_toast("t", "x" * 500)
    assert len(captured["message"]) == 200
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `uv run pytest tests/test_ops_notify.py::test_post_toast_calls_plyer -v`
Expected: FAIL — `post_toast` not defined.

- [ ] **Step 3: Implement**

In `src/trading/ops/notify.py`, at the top after the imports add:

```python
try:
    from plyer import notification as _plyer_notification  # type: ignore[import-untyped]
except Exception:  # pragma: no cover — plyer should be installed
    _plyer_notification = None  # type: ignore[assignment]
```

Then add the function after `post_slack`:

```python
def post_toast(title: str, message: str) -> bool:
    """Fire a Windows toast. Returns True on success, False otherwise.

    Message is truncated to 200 chars. On non-Windows or when plyer's
    backend is missing, returns False without raising.
    """
    if _plyer_notification is None:
        return False
    try:
        _plyer_notification.notify(
            title=title,
            message=message[:200],
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Toast notification failed: {e}")
        return False
    return True
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_ops_notify.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/trading/ops/notify.py tests/test_ops_notify.py
git commit -m "feat(ops): Windows toast primitive (Phase 17)"
```

---

## Task 6 — `ops/notify.py`: `notify()` dispatcher

**Files:**
- Modify: `src/trading/ops/notify.py`
- Modify: `tests/test_ops_notify.py`
- Modify: `src/trading/ops/__init__.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ops_notify.py`:

```python
def test_notify_info_dispatches_to_both_channels(monkeypatch):
    from trading.ops import notify as notify_mod

    slack_calls: list[str] = []
    toast_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(notify_mod, "post_slack", lambda text: slack_calls.append(text) or True)
    monkeypatch.setattr(
        notify_mod, "post_toast", lambda t, m: toast_calls.append((t, m)) or True
    )

    notify_mod.notify("info", "Reminder", "Run /kite-snapshot")

    assert len(slack_calls) == 1
    assert "🔔" in slack_calls[0]
    assert "*Reminder*" in slack_calls[0]
    assert "Run /kite-snapshot" in slack_calls[0]
    assert toast_calls == [("Reminder", "Run /kite-snapshot")]


def test_notify_error_emoji(monkeypatch):
    from trading.ops import notify as notify_mod

    slack_calls: list[str] = []
    monkeypatch.setattr(notify_mod, "post_slack", lambda t: slack_calls.append(t) or True)
    monkeypatch.setattr(notify_mod, "post_toast", lambda t, m: True)

    notify_mod.notify("error", "pre_open FAILED", "Traceback ...")
    assert "❌" in slack_calls[0]


def test_notify_warn_emoji(monkeypatch):
    from trading.ops import notify as notify_mod

    slack_calls: list[str] = []
    monkeypatch.setattr(notify_mod, "post_slack", lambda t: slack_calls.append(t) or True)
    monkeypatch.setattr(notify_mod, "post_toast", lambda t, m: True)

    notify_mod.notify("warn", "Stale snapshot", "")
    assert "⚠️" in slack_calls[0]


def test_notify_multiline_body_uses_code_block(monkeypatch):
    from trading.ops import notify as notify_mod

    slack_calls: list[str] = []
    monkeypatch.setattr(notify_mod, "post_slack", lambda t: slack_calls.append(t) or True)
    monkeypatch.setattr(notify_mod, "post_toast", lambda t, m: True)

    notify_mod.notify("info", "Title", "line1\nline2\nline3")
    assert "```" in slack_calls[0]


def test_notify_does_not_raise_when_both_channels_fail(monkeypatch):
    from trading.ops import notify as notify_mod

    monkeypatch.setattr(notify_mod, "post_slack", lambda t: False)
    monkeypatch.setattr(notify_mod, "post_toast", lambda t, m: False)

    # Must not raise
    notify_mod.notify("error", "boom", "details")
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `uv run pytest tests/test_ops_notify.py -v`
Expected: 5 new failures — `notify` not defined.

- [ ] **Step 3: Implement**

Append to `src/trading/ops/notify.py`:

```python
_EMOJI: dict[str, str] = {
    "info": "🔔",
    "warn": "⚠️",
    "error": "❌",
}


def notify(
    level: Literal["info", "warn", "error"],
    title: str,
    body: str = "",
) -> None:
    """Dispatch a notification to Slack + Windows toast.

    Best-effort: both channels are attempted, failures are logged at
    WARN. Never raises. Multi-line bodies are wrapped in a fenced code
    block for Slack readability; toast gets the raw (truncated) text.
    """
    emoji = _EMOJI.get(level, "🔔")
    slack_lines = [f"{emoji} *{title}*"]
    if body:
        if "\n" in body or level == "error":
            slack_lines.append(f"```\n{body}\n```")
        else:
            slack_lines.append(body)
    slack_text = "\n".join(slack_lines)

    try:
        post_slack(slack_text)
    except Exception as e:  # pragma: no cover — post_slack already swallows
        logger.warning(f"notify: post_slack raised: {e}")
    try:
        post_toast(title, body or title)
    except Exception as e:  # pragma: no cover
        logger.warning(f"notify: post_toast raised: {e}")
```

- [ ] **Step 4: Update package exports**

Edit `src/trading/ops/__init__.py`:

```python
"""Phase 17 — operations layer.

Notification primitives, NSE calendar, loguru configuration, and the
Task Scheduler reminder dispatcher.
"""

from __future__ import annotations

from trading.ops.notify import notify, post_slack, post_toast

__all__ = ["notify", "post_slack", "post_toast"]
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/test_ops_notify.py -v`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add src/trading/ops/notify.py src/trading/ops/__init__.py tests/test_ops_notify.py
git commit -m "feat(ops): notify() dispatcher with level emoji (Phase 17)"
```

---

## Task 7 — Bundled NSE holidays JSON

**Files:**
- Create: `data/static/nse_holidays_2026.json`

- [ ] **Step 1: Write the holidays file**

Create `data/static/nse_holidays_2026.json`:

```json
{
  "year": 2026,
  "source": "best-effort seed; refresh annually from nsepython.holiday_master() when convenient",
  "holidays": [
    {"date": "2026-01-26", "name": "Republic Day"},
    {"date": "2026-02-19", "name": "Mahashivratri"},
    {"date": "2026-03-04", "name": "Holi"},
    {"date": "2026-03-31", "name": "Id-Ul-Fitr (Ramzan Id)"},
    {"date": "2026-04-03", "name": "Good Friday"},
    {"date": "2026-04-14", "name": "Dr. Baba Saheb Ambedkar Jayanti"},
    {"date": "2026-05-01", "name": "Maharashtra Day"},
    {"date": "2026-05-27", "name": "Bakri Id"},
    {"date": "2026-08-15", "name": "Independence Day"},
    {"date": "2026-08-27", "name": "Ganesh Chaturthi"},
    {"date": "2026-10-02", "name": "Mahatma Gandhi Jayanti"},
    {"date": "2026-10-21", "name": "Dussehra"},
    {"date": "2026-11-09", "name": "Diwali Laxmi Pujan"},
    {"date": "2026-11-25", "name": "Guru Nanak Jayanti"},
    {"date": "2026-12-25", "name": "Christmas"}
  ]
}
```

- [ ] **Step 2: Verify it parses**

Run: `uv run python -c "import json, pathlib; json.loads(pathlib.Path('data/static/nse_holidays_2026.json').read_text()); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add data/static/nse_holidays_2026.json
git commit -m "data: bundled NSE 2026 holidays fallback (Phase 17)"
```

---

## Task 8 — `ops/calendar.py`: holiday gate

**Files:**
- Create: `src/trading/ops/calendar.py`
- Test: `tests/test_ops_calendar.py`
- Modify: `src/trading/ops/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ops_calendar.py`:

```python
"""Tests for src/trading/ops/calendar.py."""

from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with a clean nse_holidays cache."""
    from trading.ops import calendar as cal

    cal.nse_holidays.cache_clear()
    yield
    cal.nse_holidays.cache_clear()


def test_weekend_is_not_trading_day(monkeypatch):
    from trading.ops import calendar as cal

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", lambda year: frozenset())
    assert cal.is_trading_day(date(2026, 5, 23)) is False  # Saturday
    assert cal.is_trading_day(date(2026, 5, 24)) is False  # Sunday


def test_weekday_non_holiday_is_trading_day(monkeypatch):
    from trading.ops import calendar as cal

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", lambda year: frozenset())
    assert cal.is_trading_day(date(2026, 5, 25)) is True  # Monday


def test_known_holiday_is_not_trading_day(monkeypatch):
    from trading.ops import calendar as cal

    monkeypatch.setattr(
        cal, "_fetch_holidays_from_nsepython",
        lambda year: frozenset({date(2026, 1, 26)}),
    )
    assert cal.is_trading_day(date(2026, 1, 26)) is False


def test_nsepython_failure_falls_back_to_bundled(monkeypatch):
    from trading.ops import calendar as cal

    def boom(year):
        raise RuntimeError("nsepython api down")

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", boom)
    # Republic Day 2026 is in bundled JSON
    assert cal.is_trading_day(date(2026, 1, 26)) is False


def test_missing_bundled_falls_back_to_weekday_only(tmp_path, monkeypatch):
    from trading.ops import calendar as cal

    def boom(year):
        raise RuntimeError("api down")

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", boom)
    monkeypatch.setattr(cal, "_bundled_holidays_path", lambda year: tmp_path / "missing.json")
    # Holiday day becomes "weekday → trading day" because we have no holiday data
    assert cal.is_trading_day(date(2026, 1, 26)) is True


def test_caching_avoids_repeat_fetch(monkeypatch):
    from trading.ops import calendar as cal

    calls = []

    def counted(year):
        calls.append(year)
        return frozenset()

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", counted)
    cal.is_trading_day(date(2026, 5, 25))
    cal.is_trading_day(date(2026, 5, 26))
    cal.is_trading_day(date(2026, 5, 27))
    assert calls == [2026]  # one fetch, three uses


def test_year_boundary(monkeypatch):
    from trading.ops import calendar as cal

    calls = []

    def counted(year):
        calls.append(year)
        return frozenset()

    monkeypatch.setattr(cal, "_fetch_holidays_from_nsepython", counted)
    cal.is_trading_day(date(2026, 12, 31))
    cal.is_trading_day(date(2027, 1, 1))
    assert calls == [2026, 2027]
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `uv run pytest tests/test_ops_calendar.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `src/trading/ops/calendar.py`:

```python
"""NSE trading-day calendar.

`is_trading_day(d)` returns False for weekends OR any date in
`nse_holidays(d.year)`. Holidays are fetched from `nsepython` once per
year (cached), with a bundled JSON fallback at
`data/static/nse_holidays_<year>.json`. If both fail, only the
weekday check applies (best-effort — we'd rather over-trade on a
forgotten holiday than miss a real session).
"""

from __future__ import annotations

import functools
import json
from datetime import date
from pathlib import Path

from loguru import logger

from trading.config import get_paths


def _fetch_holidays_from_nsepython(year: int) -> frozenset[date]:
    """Pull NSE trading holidays for `year` from nsepython.

    Indirected through a module function so tests can monkeypatch
    without touching the real API. Raises any nsepython exception
    upward — the caller handles fallback.
    """
    from nsepython import holiday_master  # local import: keeps import-time light

    raw = holiday_master()
    # nsepython returns a dict {"CM": [{"tradingDate": "26-Jan-2026", ...}, ...]}
    rows = raw.get("CM", []) if isinstance(raw, dict) else []
    out: set[date] = set()
    for row in rows:
        s = row.get("tradingDate") or row.get("date")
        if not s:
            continue
        try:
            out.add(date.fromisoformat(_to_iso(s)))
        except ValueError:
            continue
    # Filter to requested year
    return frozenset(d for d in out if d.year == year)


def _to_iso(s: str) -> str:
    """Convert "26-Jan-2026" or "2026-01-26" to ISO 'YYYY-MM-DD'."""
    if "-" in s and len(s.split("-")[0]) == 4:
        return s
    # Format like "26-Jan-2026"
    import datetime as _dt

    return _dt.datetime.strptime(s, "%d-%b-%Y").date().isoformat()


def _bundled_holidays_path(year: int) -> Path:
    return get_paths().project_root / "data" / "static" / f"nse_holidays_{year}.json"


def _load_bundled_holidays(year: int) -> frozenset[date]:
    path = _bundled_holidays_path(year)
    if not path.exists():
        return frozenset()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return frozenset(
            date.fromisoformat(item["date"])
            for item in payload.get("holidays", [])
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Failed to parse bundled holidays for {year}: {e}")
        return frozenset()


@functools.cache
def nse_holidays(year: int) -> frozenset[date]:
    """Return NSE trading holidays for `year`.

    Tries nsepython first; on any failure, falls back to the bundled
    JSON. If both fail, returns an empty frozenset (and weekday check
    becomes the only gate).
    """
    try:
        return _fetch_holidays_from_nsepython(year)
    except Exception as e:
        logger.warning(f"nsepython holiday fetch failed for {year}: {e} — using bundled fallback")
        return _load_bundled_holidays(year)


def is_trading_day(d: date) -> bool:
    """True iff `d` is Mon-Fri AND not in `nse_holidays(d.year)`."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in nse_holidays(d.year)
```

- [ ] **Step 4: Update package exports**

Edit `src/trading/ops/__init__.py`:

```python
"""Phase 17 — operations layer."""

from __future__ import annotations

from trading.ops.calendar import is_trading_day, nse_holidays
from trading.ops.notify import notify, post_slack, post_toast

__all__ = [
    "is_trading_day",
    "notify",
    "nse_holidays",
    "post_slack",
    "post_toast",
]
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/test_ops_calendar.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add src/trading/ops/calendar.py src/trading/ops/__init__.py tests/test_ops_calendar.py
git commit -m "feat(ops): NSE holiday gate with bundled fallback (Phase 17)"
```

---

## Task 9 — `ops/logging_setup.py`: file + stderr sinks

**Files:**
- Create: `src/trading/ops/logging_setup.py`
- Test: `tests/test_ops_logging_setup.py`
- Modify: `src/trading/ops/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ops_logging_setup.py`:

```python
"""Tests for src/trading/ops/logging_setup.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger


@pytest.fixture
def isolated_logger():
    """Each test starts with loguru in a clean state and resets the configured-set."""
    from trading.ops import logging_setup

    logger.remove()  # start clean
    logging_setup._configured.clear()
    yield
    logger.remove()
    logging_setup._configured.clear()


def test_file_sink_writes_log(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.config import Paths

    fake_paths = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=tmp_path / "data" / "logs",
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    logging_setup.configure_logging("test_job", slack_on_error=False)
    logger.info("hello world")

    log_files = list((tmp_path / "data" / "logs").glob("test_job_*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "hello world" in content
    assert "INFO" in content


def test_configure_logging_is_idempotent(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.config import Paths

    fake_paths = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=tmp_path / "data" / "logs",
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    logging_setup.configure_logging("test_job", slack_on_error=False)
    handlers_after_first = len(logger._core.handlers)  # type: ignore[attr-defined]
    logging_setup.configure_logging("test_job", slack_on_error=False)
    handlers_after_second = len(logger._core.handlers)  # type: ignore[attr-defined]
    assert handlers_after_first == handlers_after_second


def test_log_dir_is_created(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.config import Paths

    log_dir = tmp_path / "data" / "logs"
    assert not log_dir.exists()
    fake_paths = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=log_dir,
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    logging_setup.configure_logging("test_job", slack_on_error=False)
    assert log_dir.is_dir()
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `uv run pytest tests/test_ops_logging_setup.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement (file + stderr only — Slack sink in Task 10)**

Create `src/trading/ops/logging_setup.py`:

```python
"""Loguru configuration for daily jobs.

`configure_logging(job)` adds three sinks:
- Rotating file at `data/logs/{job}_YYYY-MM-DD.log` (daily rotation,
  60-day retention, gzip compression).
- stderr in human-readable format.
- (Optional, ERROR+) Slack sink — added in `_install_slack_sink`.

Idempotent within a process via `_configured: set[str]`.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from loguru import logger

from trading.config import get_paths

_configured: set[str] = set()


def _file_format() -> str:
    return "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"


def _stderr_format() -> str:
    return "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"


def configure_logging(job: str, *, slack_on_error: bool = True) -> Path:
    """Add file + stderr (+ optional Slack) sinks for `job`.

    Returns the resolved log file path. Idempotent — second call for
    the same job in the same process is a no-op.
    """
    if job in _configured:
        return _current_log_path(job)

    if not _configured:
        # First-ever configure_logging call in this process — drop loguru's default sink
        logger.remove()

    paths = get_paths()
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = _current_log_path(job)

    logger.add(
        log_path,
        format=_file_format(),
        level="INFO",
        rotation="00:00",
        retention="60 days",
        compression="gz",
        enqueue=True,
    )
    logger.add(
        sys.stderr,
        format=_stderr_format(),
        level="INFO",
        colorize=True,
    )

    if slack_on_error:
        _install_slack_sink(job, log_path)

    _configured.add(job)
    return log_path


def _current_log_path(job: str) -> Path:
    return get_paths().logs_dir / f"{job}_{date.today().isoformat()}.log"


def _install_slack_sink(job: str, log_path: Path) -> None:
    """Stub — implemented in Task 10."""
```

- [ ] **Step 4: Update package exports**

Edit `src/trading/ops/__init__.py`:

```python
"""Phase 17 — operations layer."""

from __future__ import annotations

from trading.ops.calendar import is_trading_day, nse_holidays
from trading.ops.logging_setup import configure_logging
from trading.ops.notify import notify, post_slack, post_toast

__all__ = [
    "configure_logging",
    "is_trading_day",
    "notify",
    "nse_holidays",
    "post_slack",
    "post_toast",
]
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/test_ops_logging_setup.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/trading/ops/logging_setup.py src/trading/ops/__init__.py tests/test_ops_logging_setup.py
git commit -m "feat(ops): loguru file + stderr sinks (Phase 17)"
```

---

## Task 10 — `ops/logging_setup.py`: Slack-on-ERROR sink

**Files:**
- Modify: `src/trading/ops/logging_setup.py`
- Modify: `tests/test_ops_logging_setup.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ops_logging_setup.py`:

```python
def test_slack_sink_fires_on_error(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod
    from trading.config import Paths

    fake_paths = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=tmp_path / "data" / "logs",
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify_mod, "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    logging_setup.configure_logging("test_job", slack_on_error=True)
    logger.info("this should not slack")
    logger.error("but this should")

    assert len(calls) == 1
    level, title, body = calls[0]
    assert level == "error"
    assert "test_job FAILED" in title
    assert "but this should" in body


def test_slack_sink_includes_traceback_for_exception(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod
    from trading.config import Paths

    fake_paths = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=tmp_path / "data" / "logs",
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify_mod, "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    logging_setup.configure_logging("test_job", slack_on_error=True)
    try:
        raise ValueError("simulated failure")
    except ValueError:
        logger.exception("test_job failed")

    assert len(calls) == 1
    body = calls[0][2]
    assert "ValueError" in body
    assert "simulated failure" in body


def test_slack_sink_disabled_when_flag_false(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod
    from trading.config import Paths

    fake_paths = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=tmp_path / "data" / "logs",
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    calls = []
    monkeypatch.setattr(
        notify_mod, "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    logging_setup.configure_logging("test_job", slack_on_error=False)
    logger.error("boom")
    assert calls == []
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `uv run pytest tests/test_ops_logging_setup.py::test_slack_sink_fires_on_error -v`
Expected: FAIL — Slack sink is a stub.

- [ ] **Step 3: Implement**

Replace the stub `_install_slack_sink` in `src/trading/ops/logging_setup.py`:

```python
def _install_slack_sink(job: str, log_path: Path) -> None:
    """Add a loguru sink that posts ERROR+ records to Slack + toast.

    Body = formatted exception (if present) + tail of log file. Sink
    itself never raises — failures in the notify layer must not crash
    the job.
    """
    from trading.ops import notify as notify_mod

    def _sink(message: object) -> None:  # loguru.Message is a str subclass
        record = message.record  # type: ignore[attr-defined]
        body_parts: list[str] = []
        exc = record.get("exception")
        if exc is not None:
            import traceback
            body_parts.append("".join(traceback.format_exception(exc.type, exc.value, exc.traceback)))
        body_parts.append(f"Message: {record['message']}")
        tail = _tail_log_file(log_path, lines=20)
        if tail:
            body_parts.append("Recent log:\n" + tail)
        body = "\n\n".join(body_parts)
        try:
            notify_mod.notify("error", f"{job} FAILED", body)
        except Exception:
            pass  # never crash from the logging layer

    logger.add(_sink, level="ERROR", enqueue=False)


def _tail_log_file(path: Path, *, lines: int = 20) -> str:
    """Return the last `lines` lines of `path`, or empty string if unreadable."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_ops_logging_setup.py -v`
Expected: PASS (6 tests total).

- [ ] **Step 5: Commit**

```bash
git add src/trading/ops/logging_setup.py tests/test_ops_logging_setup.py
git commit -m "feat(ops): Slack sink on ERROR+ with traceback + log tail (Phase 17)"
```

---

## Task 11 — `ops/runner.py`: `SCHEDULE` + `ReminderSlot`

**Files:**
- Create: `src/trading/ops/runner.py`
- Test: `tests/test_ops_runner.py`
- Modify: `src/trading/ops/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ops_runner.py`:

```python
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
    # First 4 are pre_open, then iep, then mid_day, then post_close — all monotonic
    assert times == sorted(times)


def test_reminder_slot_is_frozen():
    from trading.ops.runner import ReminderSlot

    slot = ReminderSlot(when="08:30", title="t", body="b")
    with pytest.raises(Exception):
        slot.when = "09:00"  # type: ignore[misc]
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `uv run pytest tests/test_ops_runner.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `src/trading/ops/runner.py`:

```python
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
from datetime import date, datetime, timezone, timedelta
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

    when: str   # "HH:MM" IST
    title: str
    body: str = ""


SCHEDULE: Final[dict[str, ReminderSlot]] = {
    "pre_open_kite":      ReminderSlot("08:30", "🔔 Pre-open step 1/4", "Run `/kite-snapshot` in Claude Code"),
    "pre_open_scan":      ReminderSlot("08:35", "🔔 Pre-open step 2/4", "Then `trading pre-open <date>`"),
    "pre_open_analyst":   ReminderSlot("08:40", "🔔 Pre-open step 3/4", "Run `/analyst` in Claude Code"),
    "pre_open_compile":   ReminderSlot("08:45", "🔔 Pre-open step 4/4", "Finally `trading brief compile <date>`"),
    "iep_quotes":         ReminderSlot("08:55", "🔔 IEP step 1/2",      "Run `/kite-quotes-snapshot`"),
    "iep_filter":         ReminderSlot("09:00", "🔔 IEP step 2/2",      "Then `trading pre-open-iep --date <date>`"),
    "mid_day_prepare":    ReminderSlot("12:25", "🔔 Mid-day step 1/3",  "Run `trading mid-day <date>`"),
    "mid_day_quotes":     ReminderSlot("12:30", "🔔 Mid-day step 2/3",  "Run `/kite-quotes-snapshot`"),
    "mid_day_apply":      ReminderSlot("12:35", "🔔 Mid-day step 3/3",  "Then `trading mid-day <date> --apply`"),
    "post_close_prepare": ReminderSlot("16:05", "🔔 Post-close step 1/3", "Run `trading post-close <date>`"),
    "post_close_quotes":  ReminderSlot("16:10", "🔔 Post-close step 2/3", "Run `/kite-quotes-snapshot`"),
    "post_close_apply":   ReminderSlot("16:15", "🔔 Post-close step 3/3", "Then `trading post-close <date> --apply`"),
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
```

- [ ] **Step 4: Update package exports**

Edit `src/trading/ops/__init__.py`:

```python
"""Phase 17 — operations layer."""

from __future__ import annotations

from trading.ops.calendar import is_trading_day, nse_holidays
from trading.ops.logging_setup import configure_logging
from trading.ops.notify import notify, post_slack, post_toast
from trading.ops.runner import SCHEDULE, ReminderSlot, fire_reminder

__all__ = [
    "SCHEDULE",
    "ReminderSlot",
    "configure_logging",
    "fire_reminder",
    "is_trading_day",
    "notify",
    "nse_holidays",
    "post_slack",
    "post_toast",
]
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/test_ops_runner.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/trading/ops/runner.py src/trading/ops/__init__.py tests/test_ops_runner.py
git commit -m "feat(ops): SCHEDULE + ReminderSlot (Phase 17)"
```

---

## Task 12 — `fire_reminder` behaviour tests

**Files:**
- Modify: `tests/test_ops_runner.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ops_runner.py`:

```python
def test_fire_reminder_holiday_short_circuits(monkeypatch):
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: False)
    monkeypatch.setattr(runner, "notify", lambda level, title, body="": calls.append((level, title, body)))

    runner.fire_reminder("pre_open_kite", today=date(2026, 1, 26))  # Republic Day
    assert calls == []


def test_fire_reminder_substitutes_date(monkeypatch):
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: True)
    monkeypatch.setattr(runner, "notify", lambda level, title, body="": calls.append((level, title, body)))

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
    monkeypatch.setattr(runner, "notify", lambda level, title, body="": captured.update(body=body))

    runner.fire_reminder("pre_open_scan")
    assert "2026-05-25" in captured["body"]
```

- [ ] **Step 2: Run tests, confirm fail then pass**

Run: `uv run pytest tests/test_ops_runner.py -v`
Expected: PASS (8 tests total — the new ones test behaviour already implemented in Task 11).

- [ ] **Step 3: Commit**

```bash
git add tests/test_ops_runner.py
git commit -m "test(ops): fire_reminder behaviour coverage (Phase 17)"
```

---

## Task 13 — CLI: `trading remind --slot <name>`

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_cli_remind_happy_path(monkeypatch):
    from typer.testing import CliRunner
    from trading.cli import app
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: True)
    monkeypatch.setattr(runner, "notify", lambda level, title, body="": calls.append((title, body)))

    result = CliRunner().invoke(app, ["remind", "--slot", "pre_open_scan"])
    assert result.exit_code == 0
    assert len(calls) == 1
    assert "Pre-open step 2/4" in calls[0][0]


def test_cli_remind_unknown_slot_exits_2(monkeypatch):
    from typer.testing import CliRunner
    from trading.cli import app
    from trading.ops import runner

    monkeypatch.setattr(runner, "is_trading_day", lambda d: True)
    monkeypatch.setattr(runner, "notify", lambda *a, **kw: None)

    result = CliRunner().invoke(app, ["remind", "--slot", "nope"])
    assert result.exit_code == 2


def test_cli_remind_holiday_silent(monkeypatch):
    from typer.testing import CliRunner
    from trading.cli import app
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: False)
    monkeypatch.setattr(runner, "notify", lambda level, title, body="": calls.append(title))

    result = CliRunner().invoke(app, ["remind", "--slot", "pre_open_scan"])
    assert result.exit_code == 0
    assert calls == []  # holiday silent-skip
```

- [ ] **Step 2: Run test, confirm fail**

Run: `uv run pytest tests/test_cli.py::test_cli_remind_happy_path -v`
Expected: FAIL — `remind` command does not exist.

- [ ] **Step 3: Implement**

Edit `src/trading/cli.py`. Add at the top with other `trading.X` imports:

```python
from trading.ops.runner import SCHEDULE, fire_reminder
```

Add a new command anywhere among the other `@app.command()` definitions (near the end is fine):

```python
@app.command("remind")
def remind_cmd(
    slot: Annotated[str, typer.Option(help="Slot name from ops.runner.SCHEDULE")],
) -> None:
    """Fire a single reminder. Invoked by Windows Task Scheduler entries."""
    if slot not in SCHEDULE:
        console.print(f"[red]unknown slot:[/red] {slot}")
        console.print(f"valid slots: {', '.join(sorted(SCHEDULE.keys()))}")
        raise typer.Exit(code=2)
    fire_reminder(slot)
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_cli.py -k remind -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading remind --slot (Phase 17)"
```

---

## Task 14 — CLI: `trading notify-test`

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_cli_notify_test_dispatches(monkeypatch):
    from typer.testing import CliRunner
    from trading.cli import app
    from trading.ops import notify as notify_mod

    calls = []
    monkeypatch.setattr(
        notify_mod, "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    result = CliRunner().invoke(app, ["notify-test"])
    assert result.exit_code == 0
    assert len(calls) == 1
    level, title, _ = calls[0]
    assert level == "info"
    assert "notify-test" in title or "Test" in title
```

- [ ] **Step 2: Run test, confirm fail**

Run: `uv run pytest tests/test_cli.py::test_cli_notify_test_dispatches -v`
Expected: FAIL — command not found.

- [ ] **Step 3: Implement**

Add to `src/trading/cli.py`, at the top with other imports:

```python
from trading.ops.notify import notify as _notify
```

Add the command:

```python
@app.command("notify-test")
def notify_test_cmd() -> None:
    """Fire a sanity-check notification on both Slack and Windows toast.

    Useful after first-time setup to verify SLACK_WEBHOOK_URL and
    plyer are wired correctly.
    """
    _notify(
        "info",
        "Trading notify-test",
        "If you see this in Slack AND as a Windows toast, you're wired up correctly.",
    )
    console.print("[green]ok[/green] — check Slack + Windows notification area")
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_cli.py -k notify_test -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading notify-test (Phase 17)"
```

---

## Task 15 — Job shim: `pre_open.py`

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_jobs_pre_open.py`:

```python
def test_pre_open_main_configures_logging_and_propagates_failure(monkeypatch, tmp_path):
    """When run_pre_open raises, _main configures logging, lets the Slack
    sink fire via logger.exception, and re-raises so exit code propagates."""
    from trading.jobs import pre_open as job
    from trading.ops import logging_setup

    logger_calls: list[str] = []
    monkeypatch.setattr(logging_setup, "_configured", set())

    def fake_configure(job_name, slack_on_error=True):
        logger_calls.append(job_name)
        return tmp_path / f"{job_name}.log"

    monkeypatch.setattr(job, "configure_logging", fake_configure)

    def fake_run_pre_open(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(job, "run_pre_open", fake_run_pre_open)

    with pytest.raises(RuntimeError, match="simulated"):
        job._main("2026-05-25")

    assert logger_calls == ["pre_open"]
```

- [ ] **Step 2: Run test, confirm fail**

Run: `uv run pytest tests/test_jobs_pre_open.py::test_pre_open_main_configures_logging_and_propagates_failure -v`
Expected: FAIL — `configure_logging` not imported in `pre_open.py`.

- [ ] **Step 3: Implement**

Edit `src/trading/jobs/pre_open.py`. Add to the imports near the top (alphabetically with other `trading.X` imports — after the existing `from trading.config import ...`):

```python
from trading.ops.logging_setup import configure_logging
```

Replace the current `_main` function (around lines 303-320) with:

```python
def _main(  # pragma: no cover — manual entry
    date_str: str,
    skip_news: bool = False,
) -> None:
    """`python -m trading.jobs.pre_open <YYYY-MM-DD>` entry."""
    configure_logging("pre_open")
    from loguru import logger
    try:
        result = run_pre_open(
            date.fromisoformat(date_str),
            skip_news=skip_news,
        )
    except PreOpenAborted as e:
        print(f"Pre-open aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("pre_open failed")
        raise
    print(f"wrote {result.bundle_path}")
    if result.warnings:
        print(f"warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  - {w}")
```

The `pragma: no cover` is removed implicitly because the new test exercises the failure path — keep the `# pragma: no cover — manual entry` comment for the happy-path-by-CLI portion is gone; the new test covers the failure branch. Actually, since the test covers it, **remove** the `# pragma: no cover — manual entry` comment.

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_jobs_pre_open.py -v`
Expected: PASS — including new test and all existing pre_open tests.

- [ ] **Step 5: Commit**

```bash
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): pre_open logging + failure-Slack shim (Phase 17)"
```

---

## Task 16 — Job shim: `pre_open_iep.py`

**Files:**
- Modify: `src/trading/jobs/pre_open_iep.py`
- Modify: `tests/test_jobs_pre_open_iep.py`

- [ ] **Step 1: Locate the existing `_main` in `pre_open_iep.py`**

Run: `grep -n "_main" src/trading/jobs/pre_open_iep.py`
Note the line numbers.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_jobs_pre_open_iep.py`:

```python
def test_pre_open_iep_main_logging_and_failure(monkeypatch, tmp_path):
    from trading.jobs import pre_open_iep as job
    from trading.ops import logging_setup

    logger_calls: list[str] = []
    monkeypatch.setattr(logging_setup, "_configured", set())

    def fake_configure(job_name, slack_on_error=True):
        logger_calls.append(job_name)
        return tmp_path / f"{job_name}.log"

    monkeypatch.setattr(job, "configure_logging", fake_configure)

    def fake_run(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(job, "run_pre_open_iep", fake_run)

    with pytest.raises(RuntimeError, match="simulated"):
        job._main("2026-05-25")

    assert logger_calls == ["pre_open_iep"]
```

- [ ] **Step 3: Run test, confirm fail**

Run: `uv run pytest tests/test_jobs_pre_open_iep.py::test_pre_open_iep_main_logging_and_failure -v`
Expected: FAIL.

- [ ] **Step 4: Implement**

Edit `src/trading/jobs/pre_open_iep.py`. Add the import near other `trading.X` imports:

```python
from trading.ops.logging_setup import configure_logging
```

Wrap the existing `_main` body. The function exists at the bottom of the file with a similar shape to `pre_open._main`. Replace it with:

```python
def _main(date_str: str) -> None:
    """`python -m trading.jobs.pre_open_iep <YYYY-MM-DD>` entry."""
    configure_logging("pre_open_iep")
    from loguru import logger
    try:
        result = run_pre_open_iep(date.fromisoformat(date_str))
    except PreOpenIepAborted as e:
        print(f"Pre-open IEP aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("pre_open_iep failed")
        raise
    print(f"Filtered {result.candidates_input} → {result.candidates_filtered} candidates")
    if result.warnings:
        print(f"warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  - {w}")
```

If the existing `_main` already has different return / print behaviour, preserve it but keep the `configure_logging`/try-except wrapper structure.

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/test_jobs_pre_open_iep.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading/jobs/pre_open_iep.py tests/test_jobs_pre_open_iep.py
git commit -m "feat(jobs): pre_open_iep logging + failure-Slack shim (Phase 17)"
```

---

## Task 17 — Job shim: `mid_day.py`

**Files:**
- Modify: `src/trading/jobs/mid_day.py`
- Modify: `tests/test_jobs_mid_day.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_jobs_mid_day.py`:

```python
def test_mid_day_main_logging_and_failure(monkeypatch, tmp_path):
    from trading.jobs import mid_day as job
    from trading.ops import logging_setup

    logger_calls: list[str] = []
    monkeypatch.setattr(logging_setup, "_configured", set())

    def fake_configure(job_name, slack_on_error=True):
        logger_calls.append(job_name)
        return tmp_path / f"{job_name}.log"

    monkeypatch.setattr(job, "configure_logging", fake_configure)
    monkeypatch.setattr(job, "run_mid_day", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated")))

    with pytest.raises(RuntimeError, match="simulated"):
        job._main("2026-05-25", apply=False)

    assert logger_calls == ["mid_day"]
```

- [ ] **Step 2: Run test, confirm fail**

Run: `uv run pytest tests/test_jobs_mid_day.py::test_mid_day_main_logging_and_failure -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Edit `src/trading/jobs/mid_day.py`. Add the import:

```python
from trading.ops.logging_setup import configure_logging
```

Locate the existing `_main` function (look for `if __name__ == "__main__"` and the typer.run line). Wrap its body:

```python
def _main(date_str: str, apply: bool = False) -> None:
    configure_logging("mid_day")
    from loguru import logger
    try:
        result = run_mid_day(date.fromisoformat(date_str), apply=apply)
    except MidDayAborted as e:
        print(f"Mid-day aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("mid_day failed")
        raise
    # ... preserve existing print/summary code that followed the original try block
```

**If the existing `_main` does not have an `apply` parameter or has a different signature**, preserve that exact signature — just inject `configure_logging("mid_day")` as the first line and wrap the existing body in `try: ... except MidDayAborted: ... except Exception: logger.exception(...); raise`.

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_jobs_mid_day.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading/jobs/mid_day.py tests/test_jobs_mid_day.py
git commit -m "feat(jobs): mid_day logging + failure-Slack shim (Phase 17)"
```

---

## Task 18 — Job shim: `post_close.py`

**Files:**
- Modify: `src/trading/jobs/post_close.py`
- Modify: `tests/test_jobs_post_close.py`

Mirror Task 17 exactly, replacing `mid_day` with `post_close` everywhere. The test name becomes `test_post_close_main_logging_and_failure`. The exception class is `PostCloseAborted`.

- [ ] **Step 1: Write the failing test** (mirror of Task 17 Step 1, s/mid_day/post_close/g)

- [ ] **Step 2: Run test, confirm fail**

Run: `uv run pytest tests/test_jobs_post_close.py::test_post_close_main_logging_and_failure -v`
Expected: FAIL.

- [ ] **Step 3: Implement** (mirror of Task 17 Step 3)

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/test_jobs_post_close.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading/jobs/post_close.py tests/test_jobs_post_close.py
git commit -m "feat(jobs): post_close logging + failure-Slack shim (Phase 17)"
```

---

## Task 19 — Task Scheduler XML exports

**Files:**
- Create: `docs/scheduler/trading_remind_<slot>.xml` × 12

- [ ] **Step 1: Create the directory + template**

Create `docs/scheduler/_TEMPLATE.xml`:

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Phase 17 reminder slot: REPLACE_SLOT</Description>
    <Author>trading-bot</Author>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-05-25TREPLACE_HHMM:00+05:30</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <Monday/>
          <Tuesday/>
          <Wednesday/>
          <Thursday/>
          <Friday/>
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c cd /d "D:\Projects\Trading" &amp;&amp; uv run trading remind --slot REPLACE_SLOT</Arguments>
    </Exec>
  </Actions>
</Task>
```

- [ ] **Step 2: Generate per-slot XMLs**

Run this PowerShell script from the repo root to materialise all 12 files (replace TEMPLATE tokens):

```powershell
$slots = @{
  "pre_open_kite"      = "08:30"
  "pre_open_scan"      = "08:35"
  "pre_open_analyst"   = "08:40"
  "pre_open_compile"   = "08:45"
  "iep_quotes"         = "08:55"
  "iep_filter"         = "09:00"
  "mid_day_prepare"    = "12:25"
  "mid_day_quotes"     = "12:30"
  "mid_day_apply"      = "12:35"
  "post_close_prepare" = "16:05"
  "post_close_quotes"  = "16:10"
  "post_close_apply"   = "16:15"
}
$template = Get-Content -Raw "docs\scheduler\_TEMPLATE.xml"
foreach ($slot in $slots.Keys) {
    $hhmm = $slots[$slot]
    $xml = $template.Replace("REPLACE_SLOT", $slot).Replace("REPLACE_HHMM", $hhmm)
    $path = "docs\scheduler\trading_remind_$slot.xml"
    [System.IO.File]::WriteAllText($path, $xml, [System.Text.Encoding]::Unicode)
}
Write-Host "wrote 12 XML files"
```

- [ ] **Step 3: Verify all 12 files exist**

Run: `Get-ChildItem docs\scheduler\trading_remind_*.xml | Measure-Object | Select-Object -ExpandProperty Count`
Expected: `12`.

- [ ] **Step 4: Spot-check one file**

Open `docs/scheduler/trading_remind_pre_open_kite.xml` and confirm `REPLACE_SLOT` is now `pre_open_kite` and `REPLACE_HHMM` is `08:30`.

- [ ] **Step 5: Commit**

```bash
git add docs/scheduler/
git commit -m "ops: 12 Task Scheduler XML exports for reminder slots (Phase 17)"
```

---

## Task 20 — Operations runbook

**Files:**
- Create: `docs/operations.md`

- [ ] **Step 1: Write the runbook**

Create `docs/operations.md`:

````markdown
# Operations runbook

> Phase 17 — daily-job scheduling, logging, and notifications.

## First-time setup

### 1. Slack incoming webhook

1. Open https://api.slack.com/apps → **Create New App** → "From scratch".
2. Name: `trading-bot`. Workspace: pick yours.
3. Sidebar → **Incoming Webhooks** → toggle **On**.
4. Click **Add New Webhook to Workspace** → pick a private channel (e.g. `#trading-bot`).
5. Copy the webhook URL (`https://hooks.slack.com/services/T.../B.../...`).
6. Add to `.env`:
   ```
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```
7. Verify:
   ```
   uv run trading notify-test
   ```
   You should see a message in the Slack channel AND a Windows toast within ~5 seconds.

### 2. Import Task Scheduler entries

For each of the 12 XML files in `docs/scheduler/`:

```cmd
schtasks /Create /XML "docs\scheduler\trading_remind_pre_open_kite.xml" /TN "trading_remind_pre_open_kite"
```

Or bulk-import in PowerShell:

```powershell
Get-ChildItem docs\scheduler\trading_remind_*.xml | ForEach-Object {
    $name = $_.BaseName
    schtasks /Create /XML $_.FullName /TN $name /F
}
```

Open **Task Scheduler** (Win+R, `taskschd.msc`) and verify all 12 tasks appear in the root library and are **Ready**.

### 3. Confirm "Run whether user is logged on or not"

For each task: right-click → **Properties** → **General** tab → tick "Run whether user is logged on or not" if you want reminders even when locked. (Default `InteractiveToken` in the XML only fires when you're logged in.)

## Daily workflow (what you'll see)

A normal trading Monday:

```
08:30  🔔 Pre-open step 1/4 — Run /kite-snapshot in Claude Code
08:35  🔔 Pre-open step 2/4 — Then trading pre-open 2026-05-25
08:40  🔔 Pre-open step 3/4 — Run /analyst in Claude Code
08:45  🔔 Pre-open step 4/4 — Finally trading brief compile 2026-05-25
08:55  🔔 IEP step 1/2 — Run /kite-quotes-snapshot
09:00  🔔 IEP step 2/2 — Then trading pre-open-iep --date 2026-05-25
12:25  🔔 Mid-day step 1/3 — Run trading mid-day 2026-05-25
12:30  🔔 Mid-day step 2/3 — Run /kite-quotes-snapshot
12:35  🔔 Mid-day step 3/3 — Then trading mid-day 2026-05-25 --apply
16:05  🔔 Post-close step 1/3 — Run trading post-close 2026-05-25
16:10  🔔 Post-close step 2/3 — Run /kite-quotes-snapshot
16:15  🔔 Post-close step 3/3 — Then trading post-close 2026-05-25 --apply
```

Weekends and NSE holidays: reminders are silent (Task Scheduler still fires; the holiday gate in `fire_reminder` short-circuits with a log-only entry).

## Failure alerts

When any of `trading pre-open`, `trading pre-open-iep`, `trading mid-day`, `trading post-close` raises, you'll see one extra Slack post like:

```
❌ pre_open FAILED  (2026-05-25, exit 1)
```
Followed by a code-fenced block with the Python traceback and the last 20 lines of `data/logs/pre_open_2026-05-25.log`.

## Logs

- Location: `data/logs/{job}_YYYY-MM-DD.log`
- Rotation: daily at midnight (local time)
- Retention: 60 days
- Compression: gzip on rotation (`*.log.gz`)

To inspect a failed run, open the corresponding file. To trace a specific symbol, `grep` for the ticker.

## Holiday list maintenance

The NSE holiday gate uses `nsepython.holiday_master()` first and falls back to `data/static/nse_holidays_<year>.json`. The bundled JSON is a best-effort seed — refresh it annually:

```
uv run python -c "from nsepython import holiday_master; import json; print(json.dumps(holiday_master()))" > nse_raw.json
```

…then translate into the bundled JSON format (see existing `data/static/nse_holidays_2026.json`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No Slack messages arriving | `SLACK_WEBHOOK_URL` not in `.env`; or `.env` not being loaded | `uv run trading notify-test`; check `.env` parses; check workspace channel |
| No Windows toasts | Focus Assist on; or non-Windows host; or plyer install broken | Toggle Focus Assist; `uv run python -c "from plyer import notification; notification.notify(title='t', message='m')"` |
| Task Scheduler entry "did not run" | `LogonType` mismatch; uv not on PATH | Properties → General → tick "Run whether user is logged on or not"; use full path `C:\Users\<you>\.local\bin\uv.exe run trading ...` in the XML if needed |
| `trading remind` ImportError | uv environment out of date | `uv sync` from repo root |
| Failure alert formatting broken | Slack rate-limit (too many alerts in burst) | Wait a minute and resend; review log files directly |

## Manual verification checklist

1. `uv run trading notify-test` → Slack + toast both arrive.
2. Trigger one Task Scheduler entry manually (right-click → Run) → reminder arrives.
3. Deliberately break a job (`mv data/raw/<today>/holdings.json data/raw/<today>/holdings.bak`) → run `trading pre-open <today>` → `❌ pre_open FAILED` Slack post arrives with traceback.
4. Leave the laptop on overnight on a weekday → 08:30 reminder fires automatically.
5. Spot-check on a Sunday: Task Scheduler still fires but no Slack post arrives; `data/logs/<job>_*.log` shows `INFO skipped: not a trading day`.

````

- [ ] **Step 2: Verify it renders cleanly**

Run: `uv run python -c "import pathlib; print(pathlib.Path('docs/operations.md').read_text(encoding='utf-8')[:200])"`
Expected: prints the first 200 chars of the document.

- [ ] **Step 3: Commit**

```bash
git add docs/operations.md
git commit -m "docs: operations runbook for Phase 17"
```

---

## Task 21 — Manual smoke + final commit

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest -q`
Expected: All tests pass. Suite total should be ~604.

- [ ] **Step 2: Lint + type-check**

Run in parallel:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
```

Expected: clean across all three.

- [ ] **Step 3: Manual notify-test smoke**

Run: `uv run trading notify-test`
Expected: One Slack message in the configured channel AND one Windows toast appears within ~5 seconds. Console prints `ok — check Slack + Windows notification area`.

If Slack didn't arrive: confirm `SLACK_WEBHOOK_URL` is set in `.env`.
If toast didn't arrive: confirm Focus Assist isn't blocking notifications.

- [ ] **Step 4: Manual reminder smoke**

Run: `uv run trading remind --slot pre_open_scan`
Expected: One Slack message `🔔 Pre-open step 2/4` with body containing today's ISO date.

- [ ] **Step 5: Update PROGRESS.md**

Edit `PROGRESS.md`. Replace the Phase 17 block (lines 462-469) with:

```markdown
## Phase 17 — Task Scheduler + logging

> Spec at [`docs/superpowers/specs/2026-05-24-phase-17-scheduler-logging-design.md`](docs/superpowers/specs/2026-05-24-phase-17-scheduler-logging-design.md).
> Plan at [`docs/superpowers/plans/2026-05-24-phase-17-scheduler-logging.md`](docs/superpowers/plans/2026-05-24-phase-17-scheduler-logging.md).
> Reminder-driven: Task Scheduler fires Slack + toast pings; user runs commands manually. weekly_train and monthly_sip deferred.

- [x] 17.1 `loguru` configuration: `src/trading/ops/logging_setup.py` adds rotating file sink (`data/logs/{job}_YYYY-MM-DD.log`, daily rotation, 60-day retention, gzip), stderr sink, and an ERROR+ Slack sink. Idempotent per job. Wrapped into all 4 job entrypoints (`pre_open`, `pre_open_iep`, `mid_day`, `post_close`).
- [x] 17.2 12 Windows Task Scheduler XML entries under `docs/scheduler/`, one per reminder slot (pre_open ×4, iep ×2, mid_day ×3, post_close ×3). Each runs `uv run trading remind --slot <name>` Mon-Fri at the slot's IST time. weekly_train and monthly_sip deferred (return with Phase 16 / future mini-phase).
- [x] 17.3 Error notification: `src/trading/ops/notify.py` posts to Slack incoming webhook (`SLACK_WEBHOOK_URL`) + Windows toast via `plyer`. Best-effort — never crashes the job. Loguru ERROR sink auto-formats traceback + last 20 log lines into the Slack post.
- [x] 17.4 Manual verification: `trading notify-test` confirms both channels wired; `trading remind --slot pre_open_scan` confirms reminder path; deliberate `pre_open` failure confirmed `❌` alert with traceback + log tail.
- [x] 17.5 `docs/operations.md` documents Slack setup, Task Scheduler import, holiday-list refresh, log inspection, and troubleshooting matrix.
- [x] 17.6 PROGRESS.md updated → commit `feat(ops): scheduling + logging (Phase 17)` and pushed to origin/main.
```

Also update the status snapshot table on line 42:

```markdown
| 17 | Task Scheduler + logging | `[x]` |
```

And the "Currently working on" line (around line 45):

```markdown
**Currently working on:** _Phase 17 complete (manual smoke 2026-05-25 ✓)_
**Next up:** _Phase 18 — Live paper-trading (3-6 month run)_
```

- [ ] **Step 6: Final commit + push**

```bash
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(ops): scheduling + logging (Phase 17)

Wraps the four daily jobs (pre_open, pre_open_iep, mid_day, post_close)
with rotating loguru logs, an NSE holiday gate, a Slack-incoming-webhook
+ Windows-toast notification primitive, and 12 reminder slots driven by
Windows Task Scheduler. Failures auto-Slack with traceback + log tail.

New src/trading/ops/ subpackage (notify, calendar, logging_setup, runner);
two new CLI subcommands (remind, notify-test); 12 Task Scheduler XML
entries in docs/scheduler/; operations runbook in docs/operations.md.
~38 new tests, suite ~604 passing.

Closes Phase 17 of the trading-system build (PROGRESS.md).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"

git push origin main
```

- [ ] **Step 7: Verify push**

Run: `git log --oneline -1`
Expected: shows the `feat(ops): scheduling + logging (Phase 17)` commit.

Run: `git status`
Expected: `Your branch is up to date with 'origin/main'. nothing to commit, working tree clean.`

---

## Self-review (already complete)

Spec coverage:
- §2 In scope: covered by Tasks 2-21
- §2 Out: weekly_train, monthly_sip, SDK fallback for quotes — explicitly skipped (no tasks)
- §3 Workflow: Task 19 (XML) + Task 13 (CLI) + Task 11 (SCHEDULE)
- §4.1 Module layout: Tasks 2, 4-12
- §4.2 notify: Tasks 4-6
- §4.3 calendar: Tasks 7-8
- §4.4 logging_setup: Tasks 9-10
- §4.5 runner + SCHEDULE: Tasks 11-12
- §4.6 CLI: Tasks 13-14
- §4.7 Job shims: Tasks 15-18
- §5 schedule table: encoded in Task 11 (SCHEDULE dict) + Task 19 (XML times)
- §6 data flow: emergent from Tasks 4-18
- §7 error handling: covered by Task 4 (Slack failure), Task 5 (toast failure), Task 8 (nsepython failure), Task 10 (Slack sink never crashes)
- §8 testing: ~38 tests across Tasks 3-18
- §9 dependencies: Task 1
- §10 runbook: Task 20
- §11 manual verification: Task 21

Placeholders: none. Every step has concrete code, exact paths, and runnable commands.

Type consistency: `Settings.slack_webhook_url`, `ReminderSlot.when/title/body`, `SCHEDULE: dict[str, ReminderSlot]`, `configure_logging(job: str, *, slack_on_error: bool = True) -> Path`, `notify(level: Literal["info","warn","error"], title: str, body: str = "")` — all consistent across tasks.
