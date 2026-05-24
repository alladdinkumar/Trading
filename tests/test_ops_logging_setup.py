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
