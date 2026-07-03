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

try:
    from plyer import notification as _plyer_notification
except Exception:  # pragma: no cover — plyer should be installed
    _plyer_notification = None

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
        # F-060: for a Slack incoming webhook the URL *is* the credential.
        # str(e) for HTTPError/ConnectionError embeds the full posted URL
        # (e.g. "... for url: https://hooks.slack.com/services/T/B/<token>"),
        # so never log str(e)/e here — only the exception type and, if
        # present, the HTTP status code.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status is not None:
            logger.warning(f"Slack post failed: {type(e).__name__} (status={status})")
        else:
            logger.warning(f"Slack post failed: {type(e).__name__}")
        return False
    return True


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


_EMOJI: dict[str, str] = {
    "info": "\U0001f514",  # 🔔
    "warn": "⚠️",  # ⚠️
    "error": "❌",  # ❌
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
    emoji = _EMOJI.get(level, _EMOJI["info"])
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
