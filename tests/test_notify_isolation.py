"""Guards that the test suite can never emit a real Slack/Windows notification.

A leaked loguru ERROR sink once fired real "monthly_sip/weekly_train FAILED"
messages from unrelated tests. The autouse `_isolate_notifications` fixture
(conftest.py) neutralises both the notify boundary and loguru sink leakage; these
tests assert that guard holds, and that a leaked error sink stays inert.
"""

from __future__ import annotations

import os

from loguru import logger


def test_slack_webhook_not_set_in_test_env() -> None:
    """post_slack relies on SLACK_WEBHOOK_URL; the fixture drops it so no POST
    can reach the network even if a sink fires."""
    assert os.environ.get("SLACK_WEBHOOK_URL") is None


def test_toast_backend_disabled_in_tests() -> None:
    from trading.ops import notify as notify_mod

    assert notify_mod._plyer_notification is None


def test_post_slack_is_inert_without_webhook() -> None:
    from trading.ops import notify as notify_mod

    notify_mod._reset_warned()
    assert notify_mod.post_slack("should not send") is False


def test_leaked_error_sink_does_not_escape(tmp_path, monkeypatch) -> None:
    """Even with a real Slack error sink installed, an ERROR log makes no network
    call (no webhook) and is captured durably in failures.log — the exact
    cross-test leak that caused the spam, now inert."""
    import requests

    from trading.config import Paths
    from trading.ops import logging_setup

    fake = Paths(
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
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake)

    posts: list[object] = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: posts.append((a, k)))

    logging_setup.configure_logging("weekly_train", slack_on_error=True)
    logger.error("simulated failure")
    logger.complete()

    assert posts == []  # no webhook → no network escape
    failures = tmp_path / "data" / "logs" / "failures.log"
    assert failures.exists()
    assert "simulated failure" in failures.read_text(encoding="utf-8")
