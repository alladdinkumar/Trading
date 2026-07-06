"""Tests for `ops.run_status` — artifact-inference half-run detection (F-003).

`trading status --date` reports which of the day's checkpoints have run by
probing the durable artifacts each step leaves on disk (or in the DB), and is
*time-aware*: a not-yet-run step is `missing` only once its IST slot time has
passed, else `pending`. Non-trading days are `n/a`.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.ops import run_status
from trading.ops.run_status import compute_status, has_due_failure

_IST = timezone(timedelta(hours=5, minutes=30))

# Anchored to the real current date so the CLI tests (which exercise the
# command with the live wall clock, no `now=` override) stay deterministic
# across day rollovers. Every test forces `is_trading_day → True` (autouse
# `_trading_day` fixture), so the actual weekday never matters.
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


def _ist(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST)


@pytest.fixture
def paths(tmp_path: Path):
    return get_paths(root=tmp_path)


@pytest.fixture(autouse=True)
def _trading_day(monkeypatch):
    """Default every test to a trading day (no network holiday lookup)."""
    monkeypatch.setattr(run_status, "is_trading_day", lambda d: True)


def _raw(paths, d: date) -> Path:
    out = paths.raw_dir / d.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _research(paths, d: date) -> Path:
    out = paths.research_dir / d.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _state(statuses, key: str) -> str:
    return next(s.state for s in statuses if s.key == key)


def test_checkpoint_set_is_eight_across_four_blocks(paths):
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 7, 0))
    assert len(statuses) == 8
    assert {s.block for s in statuses} == {"pre_open", "iep", "mid_day", "post_close"}


def test_all_pending_on_fresh_project_before_open(paths):
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 7, 0))
    assert all(s.state == "pending" for s in statuses)
    assert not has_due_failure(statuses)


def test_snapshot_done_when_holdings_present(paths):
    raw = _raw(paths, TODAY)
    (raw / "holdings.json").write_text("[]", encoding="utf-8")
    (raw / "_meta.json").write_text(
        json.dumps({"snapshot_at": "2026-06-18T08:31:00+05:30"}), encoding="utf-8"
    )
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 0))
    snap = next(s for s in statuses if s.key == "pre_open_kite")
    assert snap.ran is True
    assert snap.state == "done"
    assert "2026-06-18T08:31:00+05:30" in snap.detail


def test_due_step_missing_after_its_time(paths):
    # At 09:00 the four pre-open steps + iep_quotes (08:55) + iep_filter (09:00)
    # are due; mid-day (12:35) and post-close (16:15) are still pending.
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 0))
    assert _state(statuses, "pre_open_kite") == "missing"
    assert _state(statuses, "pre_open_compile") == "missing"
    assert _state(statuses, "iep_quotes") == "missing"
    assert _state(statuses, "iep_filter") == "missing"  # exactly due at 09:00
    assert _state(statuses, "mid_day") == "pending"
    assert _state(statuses, "post_close") == "pending"
    assert has_due_failure(statuses)


def test_scan_analyst_compile_detected(paths):
    research = _research(paths, TODAY)
    (research / "_context.md").write_text("ctx", encoding="utf-8")
    (research / "macro_brief.md").write_text("macro", encoding="utf-8")
    (research / "brief.md").write_text("brief", encoding="utf-8")
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 0))
    assert _state(statuses, "pre_open_scan") == "done"
    assert _state(statuses, "pre_open_analyst") == "done"
    assert _state(statuses, "pre_open_compile") == "done"


def test_iep_quotes_detected_by_time_band(paths):
    raw = _raw(paths, TODAY)
    (raw / "quotes_0856.json").write_text("{}", encoding="utf-8")
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 5))
    assert _state(statuses, "iep_quotes") == "done"


def test_midday_quotes_do_not_satisfy_iep_band(paths):
    raw = _raw(paths, TODAY)
    (raw / "quotes_1300.json").write_text("{}", encoding="utf-8")  # afternoon only
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 13, 30))
    assert _state(statuses, "iep_quotes") == "missing"


def test_iep_filter_detected_by_marker(paths):
    # pre_open_iep (F-062) stamps _context.md with its own "ran" marker,
    # which is what iep_filter now keys on (not quotes-file mtime).
    research = _research(paths, TODAY)
    ctx = research / "_context.md"
    ctx.write_text(
        "filtered\n\n<!-- pre-open-iep: ran_at="
        + _ist(TODAY, 8, 56).isoformat()
        + " quotes_available=true -->\n",
        encoding="utf-8",
    )
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 30))
    assert _state(statuses, "iep_filter") == "done"


def test_iep_filter_not_done_when_no_marker(paths):
    research = _research(paths, TODAY)
    ctx = research / "_context.md"
    ctx.write_text("pre-iep, never re-filtered", encoding="utf-8")
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 30))
    assert _state(statuses, "iep_filter") == "missing"


def test_iep_checks_done_when_marker_shows_quotes_unavailable(paths):
    """F-062: nothing writes `_quote_symbols.txt` for the IEP block, so
    `pre_open_iep` degrades gracefully and runs with no overnight quotes —
    that must read as `done`, not a false half-run, on every trading day."""
    research = _research(paths, TODAY)
    ctx = research / "_context.md"
    ctx.write_text(
        "filtered\n\n<!-- pre-open-iep: ran_at="
        + _ist(TODAY, 8, 56).isoformat()
        + " quotes_available=false -->\n",
        encoding="utf-8",
    )
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 30))
    assert _state(statuses, "iep_quotes") == "done"
    assert _state(statuses, "iep_filter") == "done"
    iep_statuses = [s for s in statuses if s.block == "iep"]
    assert not has_due_failure(iep_statuses)


def test_iep_quotes_real_file_still_preferred_over_marker(paths):
    """If the quotes-file gap is ever closed, a real IEP-band capture still
    reports via its own filename, independent of the marker."""
    raw = _raw(paths, TODAY)
    (raw / "quotes_0856.json").write_text("{}", encoding="utf-8")
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 9, 5))
    assert _state(statuses, "iep_quotes") == "done"


def test_mid_day_detected_by_update_file(paths):
    research = _research(paths, TODAY)
    (research / "mid_day_update.md").write_text("mtm", encoding="utf-8")
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 13, 0))
    assert _state(statuses, "mid_day") == "done"


def test_post_close_detected_by_summary_file(paths):
    research = _research(paths, TODAY)
    (research / "post_close_summary.md").write_text("summary", encoding="utf-8")
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 16, 30))
    assert _state(statuses, "post_close") == "done"


def test_post_close_detected_by_db_snapshot_row(paths):
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO portfolio_snapshots(date, cash, holdings_json, equity) "
            "VALUES (?, ?, ?, ?)",
            (TODAY.isoformat(), 100000.0, "[]", 100000.0),
        )
        conn.commit()
        statuses = compute_status(paths, TODAY, now=_ist(TODAY, 16, 30), conn=conn)
    assert _state(statuses, "post_close") == "done"


def test_past_date_unrun_steps_are_missing(paths):
    # Checking yesterday from today: the day is over, so every un-run step is missing.
    statuses = compute_status(paths, YESTERDAY, now=_ist(TODAY, 9, 0))
    assert all(s.state == "missing" for s in statuses)
    assert has_due_failure(statuses)


def test_future_date_all_pending(paths):
    statuses = compute_status(paths, TOMORROW, now=_ist(TODAY, 9, 0))
    assert all(s.state == "pending" for s in statuses)
    assert not has_due_failure(statuses)


def test_non_trading_day_is_all_na(paths, monkeypatch):
    monkeypatch.setattr(run_status, "is_trading_day", lambda d: False)
    statuses = compute_status(paths, TODAY, now=_ist(TODAY, 16, 30))
    assert all(s.state == "n/a" for s in statuses)
    assert not has_due_failure(statuses)


def test_cli_status_exits_one_on_half_run(tmp_path, monkeypatch):
    """`trading status --date <past>` on an empty project exits 1 (all missing)."""
    from typer.testing import CliRunner

    from trading.cli import app

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("trading.ops.run_status.is_trading_day", lambda d: True)
    result = CliRunner().invoke(app, ["status", "--date", YESTERDAY.isoformat()])
    assert result.exit_code == 1, result.output
    assert "Half-run detected" in result.output
    assert "pre_open" in result.output


def test_cli_status_exits_zero_when_nothing_due(tmp_path, monkeypatch):
    """A future date has no due steps, so the command exits 0."""
    from typer.testing import CliRunner

    from trading.cli import app

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("trading.ops.run_status.is_trading_day", lambda d: True)
    result = CliRunner().invoke(app, ["status", "--date", TOMORROW.isoformat()])
    assert result.exit_code == 0, result.output
