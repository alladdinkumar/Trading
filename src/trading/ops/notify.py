"""Notification primitives — Slack incoming webhook + Windows toast.

Both channels are best-effort. Failures are caught and surfaced as
WARN-level loguru logs but never propagated; the underlying job is the
source of truth, and notification is informational only.
"""

from __future__ import annotations

import os

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
