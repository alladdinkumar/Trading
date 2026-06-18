"""Shared pytest fixtures for the trading project."""

import json as _kite_json
from datetime import datetime as _kite_dt
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_notifications(monkeypatch):
    """No test may emit a real Slack post or Windows toast, and no loguru sink
    may leak into the next test.

    Background: `configure_logging(..., slack_on_error=True)` installs an ERROR
    loguru sink into the *global* logger. Loguru is a process-wide singleton, so
    a sink installed by one test (e.g. the real `trading weekly-train` smoke run)
    survived into later tests; any subsequent `logger.exception(...)` then fired
    that stale sink through the real notify boundary, spamming Slack/Windows with
    mismatched "monthly_sip/weekly_train FAILED" messages carrying an unrelated
    test's traceback. This fixture closes both holes:

    - Neutralise the notify boundary (drop SLACK_WEBHOOK_URL so `post_slack`
      no-ops; null the toast backend) so even an in-test error sink can't escape.
      Tests that exercise the real notify path re-set these themselves.
    - Reset loguru handlers + the configured-set after every test so no sink
      leaks forward.
    """
    from loguru import logger

    from trading.ops import logging_setup
    from trading.ops import notify as notify_mod

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TRADING_SLACK_ON_ERROR", raising=False)
    monkeypatch.setattr(notify_mod, "_plyer_notification", None, raising=False)
    yield
    logger.remove()
    logging_setup._configured.clear()


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the tests/fixtures/ directory."""
    return Path(__file__).parent / "fixtures"


def seed_kite_snapshot(
    paths,
    as_of,
    *,
    holdings=None,
    gtts=None,
    positions=None,
    snapshot_at=None,
    source="mcp",
):
    """Test helper: write data/raw/<as_of>/{resource}.json + _meta.json.

    Each `*_lists` parameter is a list of dicts (or None to skip the
    file). Used by kite_snapshot tests + pre_open / portfolio CLI tests.
    """
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    if holdings is not None:
        (base / "holdings.json").write_text(
            _kite_json.dumps(holdings), encoding="utf-8"
        )
    if gtts is not None:
        (base / "gtts.json").write_text(
            _kite_json.dumps(gtts), encoding="utf-8"
        )
    if positions is not None:
        (base / "positions.json").write_text(
            _kite_json.dumps(positions), encoding="utf-8"
        )
    meta_ts = (
        snapshot_at.isoformat()
        if snapshot_at is not None
        else _kite_dt.combine(as_of, _kite_dt.min.time()).isoformat()
    )
    (base / "_meta.json").write_text(
        _kite_json.dumps({
            "snapshot_at": meta_ts, "source": source, "skill_version": "1",
        }),
        encoding="utf-8",
    )
    return base
