"""Tests for src/trading/ops/notify.py."""

from __future__ import annotations

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
