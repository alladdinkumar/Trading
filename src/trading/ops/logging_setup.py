"""Loguru configuration for daily jobs.

`configure_logging(job)` adds these sinks:
- Rotating file at `data/logs/{job}_YYYY-MM-DD.log` (daily rotation,
  60-day retention, gzip compression).
- stderr in human-readable format.
- A durable, job-tagged `data/logs/failures.log` (ERROR+, synchronous) — the
  single place to flag-and-fix any job failure without trawling per-job logs.
- (Opt-in, ERROR+) Slack/toast sink — added in `_install_slack_sink` only when
  `slack_on_error` is True. It defaults to the `TRADING_SLACK_ON_ERROR` env flag
  (off unless set), so a failing job records to `failures.log` instead of
  spamming Slack/Windows on every run.

Idempotent within a process via `_configured: set[str]`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from trading.clock import today_ist
from trading.config import get_paths

_configured: set[str] = set()

_TRUTHY = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _file_format() -> str:
    return "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"


def _stderr_format() -> str:
    return "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"


def _failure_format(job: str) -> str:
    """File format for the shared failures ledger, tagged with the job name.

    `job` is an internal constant (never user input), so embedding it directly
    in the loguru template is safe.
    """
    return (
        "{time:YYYY-MM-DD HH:mm:ss} | "
        + job
        + " | {level: <8} | {name}:{function}:{line} | {message}"
    )


def configure_logging(job: str, *, slack_on_error: bool | None = None) -> Path:
    """Add file + stderr + failures.log (+ opt-in Slack) sinks for `job`.

    `slack_on_error=None` (the default) reads the `TRADING_SLACK_ON_ERROR` env
    flag, which is off unless explicitly set — so failures are tracked in
    `failures.log` rather than pushed to Slack/Windows on every run. Pass
    `True`/`False` to force it.

    Returns the resolved log file path. Idempotent — second call for
    the same job in the same process is a no-op.
    """
    if slack_on_error is None:
        slack_on_error = _env_truthy("TRADING_SLACK_ON_ERROR")

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
    # Durable failure ledger — synchronous (enqueue=False) so a crashing job
    # still leaves a reviewable, job-tagged record even when Slack is silenced.
    logger.add(
        paths.logs_dir / "failures.log",
        format=_failure_format(job),
        level="ERROR",
        rotation="5 MB",
        retention="90 days",
    )

    if slack_on_error:
        _install_slack_sink(job, log_path)

    _configured.add(job)
    return log_path


def _current_log_path(job: str) -> Path:
    # F-058: today_ist(), never the host clock's date.today() (drift on non-IST hosts).
    return get_paths().logs_dir / f"{job}_{today_ist().isoformat()}.log"


def _install_slack_sink(job: str, log_path: Path) -> None:
    """Add a loguru sink that posts ERROR+ records to Slack + toast.

    Body = formatted exception (if present) + tail of log file. Sink
    itself never raises — failures in the notify layer must not crash
    the job.
    """
    from trading.ops import notify as notify_mod

    def _sink(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        body_parts: list[str] = []
        exc = record.get("exception")
        if exc is not None:
            import traceback

            body_parts.append(
                "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))
            )
        body_parts.append(f"Message: {record['message']}")
        tail = _tail_log_file(log_path, lines=20)
        if tail:
            body_parts.append("Recent log:\n" + tail)
        body = "\n\n".join(body_parts)
        import contextlib

        with contextlib.suppress(Exception):
            notify_mod.notify("error", f"{job} FAILED", body)

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
