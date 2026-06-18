"""Tests for src/trading/ops/logging_setup.py."""

from __future__ import annotations

import pytest
from loguru import logger


@pytest.fixture
def isolated_logger():
    """Each test starts with loguru in a clean state and resets the configured-set."""
    from trading.ops import logging_setup

    logger.remove()
    logging_setup._configured.clear()
    yield
    logger.remove()
    logging_setup._configured.clear()


def _make_fake_paths(tmp_path):
    from trading.config import Paths

    return Paths(
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


def test_file_sink_writes_log(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup

    fake_paths = _make_fake_paths(tmp_path)
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    logging_setup.configure_logging("test_job", slack_on_error=False)
    logger.info("hello world")
    logger.complete()

    log_files = list((tmp_path / "data" / "logs").glob("test_job_*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "hello world" in content
    assert "INFO" in content


def test_configure_logging_is_idempotent(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup

    fake_paths = _make_fake_paths(tmp_path)
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    logging_setup.configure_logging("test_job", slack_on_error=False)
    handlers_after_first = len(logger._core.handlers)  # type: ignore[attr-defined]
    logging_setup.configure_logging("test_job", slack_on_error=False)
    handlers_after_second = len(logger._core.handlers)  # type: ignore[attr-defined]
    assert handlers_after_first == handlers_after_second


def test_log_dir_is_created(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup

    log_dir = tmp_path / "data" / "logs"
    assert not log_dir.exists()
    fake_paths = _make_fake_paths(tmp_path)
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    logging_setup.configure_logging("test_job", slack_on_error=False)
    assert log_dir.is_dir()


def test_slack_sink_fires_on_error(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod

    fake_paths = _make_fake_paths(tmp_path)
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify_mod,
        "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    logging_setup.configure_logging("test_job", slack_on_error=True)
    logger.info("this should not slack")
    logger.error("but this should")
    logger.complete()

    assert len(calls) == 1
    level, title, body = calls[0]
    assert level == "error"
    assert "test_job FAILED" in title
    assert "but this should" in body


def test_slack_sink_includes_traceback_for_exception(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod

    fake_paths = _make_fake_paths(tmp_path)
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify_mod,
        "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    logging_setup.configure_logging("test_job", slack_on_error=True)
    try:
        raise ValueError("simulated failure")
    except ValueError:
        logger.exception("test_job failed")
    logger.complete()

    assert len(calls) == 1
    body = calls[0][2]
    assert "ValueError" in body
    assert "simulated failure" in body


def test_slack_sink_disabled_when_flag_false(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod

    fake_paths = _make_fake_paths(tmp_path)
    monkeypatch.setattr(logging_setup, "get_paths", lambda: fake_paths)

    calls = []
    monkeypatch.setattr(
        notify_mod,
        "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )

    logging_setup.configure_logging("test_job", slack_on_error=False)
    logger.error("boom")
    logger.complete()
    assert calls == []


def test_slack_on_error_off_by_default(tmp_path, monkeypatch, isolated_logger):
    """No `slack_on_error` arg + no env → errors are NOT pushed to Slack/toast.

    The error-notification path is now opt-in so failures don't spam Slack/Windows;
    they're tracked in the log files instead (see failures.log tests)."""
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod

    monkeypatch.setattr(logging_setup, "get_paths", lambda: _make_fake_paths(tmp_path))
    monkeypatch.delenv("TRADING_SLACK_ON_ERROR", raising=False)

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda level, title, body="": calls.append((level, title, body))
    )

    logging_setup.configure_logging("test_job")  # default — no slack arg
    logger.error("boom")
    logger.complete()
    assert calls == []


def test_slack_on_error_enabled_by_env(tmp_path, monkeypatch, isolated_logger):
    """`TRADING_SLACK_ON_ERROR=1` re-enables the opt-in error notification."""
    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod

    monkeypatch.setattr(logging_setup, "get_paths", lambda: _make_fake_paths(tmp_path))
    monkeypatch.setenv("TRADING_SLACK_ON_ERROR", "1")

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda level, title, body="": calls.append((level, title, body))
    )

    logging_setup.configure_logging("test_job")  # default reads the env
    logger.error("boom")
    logger.complete()
    assert len(calls) == 1
    assert calls[0][0] == "error"


def test_error_is_recorded_in_failures_log(tmp_path, monkeypatch, isolated_logger):
    """Every ERROR is appended to a durable, job-tagged `failures.log` so a
    failure can be flagged and fixed from a file rather than a transient toast."""
    from trading.ops import logging_setup

    monkeypatch.setattr(logging_setup, "get_paths", lambda: _make_fake_paths(tmp_path))

    logging_setup.configure_logging("monthly_sip", slack_on_error=False)
    logger.error("kaboom from sip")
    logger.complete()

    failures = tmp_path / "data" / "logs" / "failures.log"
    assert failures.exists()
    text = failures.read_text(encoding="utf-8")
    assert "kaboom from sip" in text
    assert "monthly_sip" in text  # job-tagged
    assert "ERROR" in text


def test_info_is_not_recorded_in_failures_log(tmp_path, monkeypatch, isolated_logger):
    from trading.ops import logging_setup

    monkeypatch.setattr(logging_setup, "get_paths", lambda: _make_fake_paths(tmp_path))

    logging_setup.configure_logging("monthly_sip", slack_on_error=False)
    logger.info("routine progress")
    logger.complete()

    failures = tmp_path / "data" / "logs" / "failures.log"
    if failures.exists():
        assert "routine progress" not in failures.read_text(encoding="utf-8")
