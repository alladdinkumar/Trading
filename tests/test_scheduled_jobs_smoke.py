"""Runtime smoke tests for the two non-reminder scheduled entry points.

`trading_weekly_train.xml` and `trading_remind_monthly_sip.xml` are the two
Task Scheduler tasks that were stored as UTF-8 while declaring UTF-16 (so they
failed to register/run); the byte/declaration match is guarded in
`test_schedule_slack_smoke.py`. These tests cover the *other* half — that the
commands those tasks invoke actually run to exit 0 under the scheduler-shaped
path (notify stubbed, fresh empty project), and that the emoji they emit are
UTF-8 encodable so they can't crash a legacy-codepage console.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trading.cli import app

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A fresh, empty project root with both notify sinks stubbed.

    Stubbing `post_slack`/`post_toast` at the source neutralises every module's
    `notify` binding (weekly_train imports it at module level), so no network or
    Windows-toast call escapes — exactly the headless scheduler context.
    """
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    posted: list[str] = []
    monkeypatch.setattr("trading.ops.notify.post_slack", lambda text: posted.append(text) or True)
    monkeypatch.setattr("trading.ops.notify.post_toast", lambda title, message: True)
    return posted


def test_remind_monthly_sip_exits_zero(project, monkeypatch) -> None:
    """`trading remind --slot monthly_sip` — the scheduled command — exits 0.

    monthly_sip is the one non-holiday-gated slot; it must fire its notify and
    return cleanly regardless of trading-day/Slack state."""
    monkeypatch.setattr("trading.ops.runner.is_trading_day", lambda d: True)
    sip_calls: list[str] = []
    monkeypatch.setattr(
        "trading.ops.runner.notify",
        lambda level, title, body="": sip_calls.append(title),
    )
    result = runner.invoke(app, ["remind", "--slot", "monthly_sip"])
    assert result.exit_code == 0, result.output
    assert len(sip_calls) == 1
    assert "SIP" in sip_calls[0]


def test_weekly_train_exits_zero_on_empty_project(project) -> None:
    """`trading weekly-train` runs to completion on a fresh DB with no parquet.

    The retrain step degrades gracefully (no symbols → skipped) and the review
    markdown is always written, so the scheduled Sunday task exits 0."""
    result = runner.invoke(app, ["weekly-train", "--date", "2026-06-21"])
    assert result.exit_code == 0, result.output
    assert "weekly_train" in result.output
    # The 📊 Slack summary fired through the (stubbed) sink and encodes cleanly.
    assert any("Weekly review" in text for text in project)
    for text in project:
        text.encode("utf-8")


def test_weekly_train_failure_is_recorded_in_failures_log(project, tmp_path, monkeypatch) -> None:
    """An unexpected failure in the scheduled run is captured durably in
    failures.log (so it can be flagged/fixed) and still exits non-zero."""
    import trading.jobs.weekly_train as wt

    def boom(*a, **kw):
        raise RuntimeError("simulated weekly failure")

    monkeypatch.setattr(wt, "run_weekly_train", boom)

    result = runner.invoke(app, ["weekly-train", "--date", "2026-06-21"])
    assert result.exit_code != 0

    failures = tmp_path / "data" / "logs" / "failures.log"
    assert failures.exists()
    text = failures.read_text(encoding="utf-8")
    assert "weekly_train" in text
    assert "simulated weekly failure" in text


def test_scheduled_notify_emoji_are_utf8_safe() -> None:
    """The emoji these jobs emit (📊 weekly, 💰 SIP, 🔔 reminders) encode to UTF-8.

    Slack receives UTF-8 JSON; a glyph that can't encode would crash the post.
    """
    from trading.jobs.weekly_train import run_weekly_train  # noqa: F401  (import smoke)
    from trading.ops.runner import SCHEDULE

    titles = [SCHEDULE["monthly_sip"].title, "📊 Weekly review 2026-06-21"]
    for title in titles:
        title.encode("utf-8")  # must not raise
    # And at least one carries a non-cp1252 glyph (else the guard is vacuous).
    assert any(_has_non_cp1252(t) for t in titles)


def _has_non_cp1252(text: str) -> bool:
    try:
        text.encode("cp1252")
        return False
    except UnicodeEncodeError:
        return True
