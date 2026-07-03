"""Tests for src/trading/ops/notify.py."""

from __future__ import annotations

import requests
from loguru import logger


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
    notify_mod._reset_warned()
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


def test_post_slack_does_not_log_webhook_url_on_http_error(monkeypatch):
    """F-060: for a Slack incoming webhook, the URL *is* the credential.
    `HTTPError.__str__` embeds the full posted URL (`... for url: <url>`), so
    logging `str(e)` writes the live token to the on-disk rotating log. The
    except block must log only the exception type + status code."""
    from trading.ops import notify as notify_mod

    class FakeResponse:
        status_code = 404

    secret_url = "https://hooks.slack.com/services/T/B/secret"

    def boom(*a, **kw):
        raise requests.exceptions.HTTPError(
            f"404 Client Error: Not Found for url: {secret_url}",
            response=FakeResponse(),
        )

    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_url)
    monkeypatch.setattr(requests, "post", boom)

    records: list[str] = []
    handler_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        ok = notify_mod.post_slack("hello")
    finally:
        logger.remove(handler_id)

    assert ok is False
    combined = "\n".join(records)
    assert "hooks.slack.com" not in combined
    assert "secret" not in combined
    assert "HTTPError" in combined
    assert "404" in combined  # status code retained for diagnostics


def test_post_slack_does_not_log_webhook_url_on_connection_error(monkeypatch):
    """Same leak via ConnectionError/timeout — str(e) also embeds the URL."""
    from trading.ops import notify as notify_mod

    secret_url = "https://hooks.slack.com/services/T/B/secret"

    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='hooks.slack.com', port=443): "
            "Max retries exceeded with url: /services/T/B/secret"
        )

    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_url)
    monkeypatch.setattr(requests, "post", boom)

    records: list[str] = []
    handler_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        ok = notify_mod.post_slack("hello")
    finally:
        logger.remove(handler_id)

    assert ok is False
    combined = "\n".join(records)
    assert "hooks.slack.com" not in combined
    assert "secret" not in combined
    assert "ConnectionError" in combined


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


def test_notify_info_dispatches_to_both_channels(monkeypatch):
    from trading.ops import notify as notify_mod

    slack_calls: list[str] = []
    toast_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(notify_mod, "post_slack", lambda text: slack_calls.append(text) or True)
    monkeypatch.setattr(notify_mod, "post_toast", lambda t, m: toast_calls.append((t, m)) or True)

    notify_mod.notify("info", "Reminder", "Run /kite-snapshot")

    assert len(slack_calls) == 1
    assert "\U0001f514" in slack_calls[0]  # bell emoji
    assert "*Reminder*" in slack_calls[0]
    assert "Run /kite-snapshot" in slack_calls[0]
    assert toast_calls == [("Reminder", "Run /kite-snapshot")]


def test_notify_error_emoji(monkeypatch):
    from trading.ops import notify as notify_mod

    slack_calls: list[str] = []
    monkeypatch.setattr(notify_mod, "post_slack", lambda t: slack_calls.append(t) or True)
    monkeypatch.setattr(notify_mod, "post_toast", lambda t, m: True)

    notify_mod.notify("error", "pre_open FAILED", "Traceback ...")
    assert "❌" in slack_calls[0]  # cross mark


def test_notify_warn_emoji(monkeypatch):
    from trading.ops import notify as notify_mod

    slack_calls: list[str] = []
    monkeypatch.setattr(notify_mod, "post_slack", lambda t: slack_calls.append(t) or True)
    monkeypatch.setattr(notify_mod, "post_toast", lambda t, m: True)

    notify_mod.notify("warn", "Stale snapshot", "")
    assert "⚠" in slack_calls[0]  # warning sign


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

    notify_mod.notify("error", "boom", "details")
