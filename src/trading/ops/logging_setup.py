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
    return (
        "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
        "{name}:{function}:{line} | {message}"
    )


def _stderr_format() -> str:
    return (
        "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<level>{message}</level>"
    )


def configure_logging(job: str, *, slack_on_error: bool = True) -> Path:
    """Add file + stderr (+ optional Slack) sinks for `job`.

    Returns the resolved log file path. Idempotent — second call for
    the same job in the same process is a no-op.
    """
    if job in _configured:
        return _current_log_path(job)

    if not _configured:
        # First-ever configure call in this process — drop loguru's default sink
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
