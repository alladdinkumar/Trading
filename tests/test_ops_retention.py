"""Tests for trading.ops.retention — data retention / prune (F-013).

Two independent pruners (raw date-dirs on disk, `news_items` rows in SQLite)
plus a `run_retention` orchestrator. Dry-run by default: nothing is deleted
unless `apply=True`, but the would-delete counts are always reported.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from trading.config import get_paths
from trading.data.news import NewsItem
from trading.ops.retention import (
    DEFAULT_NEWS_KEEP_DAYS,
    DEFAULT_RAW_KEEP_DAYS,
    prune_news,
    prune_raw_dirs,
    run_retention,
)
from trading.store.migrations import run_migrations
from trading.store.news_store import insert_news_items


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _make_news(ts: str, i: int) -> NewsItem:
    return NewsItem(
        ts=ts,
        symbol="RELIANCE",
        source="mc",
        headline=f"headline {i}",
        url=f"https://x/{i}",
        sentiment=0.0,
        category="results",
        is_critical=False,
    )


# ---------------------------------------------------------------------------
# prune_raw_dirs
# ---------------------------------------------------------------------------


def test_prune_raw_dirs_removes_only_old_date_dirs(tmp_path) -> None:
    paths = get_paths(tmp_path)
    now = date(2026, 6, 18)
    old = paths.raw_dir / "2026-04-01"  # 78 days old → past 30d
    recent = paths.raw_dir / "2026-06-10"  # 8 days old → kept
    for d in (old, recent):
        d.mkdir(parents=True)
        (d / "holdings.json").write_text("{}", encoding="utf-8")

    deleted = prune_raw_dirs(paths, keep_days=30, now=now, apply=True)

    assert deleted == ["2026-04-01"]
    assert not old.exists()
    assert recent.exists()


def test_prune_raw_dirs_ignores_non_date_entries(tmp_path) -> None:
    paths = get_paths(tmp_path)
    paths.raw_dir.mkdir(parents=True)
    (paths.raw_dir / "README.md").write_text("keep me", encoding="utf-8")
    stray = paths.raw_dir / "not-a-date"
    stray.mkdir()

    deleted = prune_raw_dirs(paths, keep_days=30, now=date(2026, 6, 18), apply=True)

    assert deleted == []
    assert (paths.raw_dir / "README.md").exists()
    assert stray.exists()


def test_prune_raw_dirs_dry_run_reports_without_deleting(tmp_path) -> None:
    paths = get_paths(tmp_path)
    old = paths.raw_dir / "2026-01-01"
    old.mkdir(parents=True)

    deleted = prune_raw_dirs(paths, keep_days=30, now=date(2026, 6, 18), apply=False)

    assert deleted == ["2026-01-01"]
    assert old.exists()  # dry run: still on disk


def test_prune_raw_dirs_missing_dir_is_noop(tmp_path) -> None:
    paths = get_paths(tmp_path)  # raw_dir does not exist
    assert prune_raw_dirs(paths, keep_days=30, now=date(2026, 6, 18), apply=True) == []


# ---------------------------------------------------------------------------
# prune_news
# ---------------------------------------------------------------------------


def test_prune_news_deletes_rows_older_than_cutoff(conn: sqlite3.Connection) -> None:
    insert_news_items(
        conn,
        [
            _make_news("2025-01-01T10:00:00+00:00", 1),  # old
            _make_news("2026-06-01T10:00:00+00:00", 2),  # recent
        ],
    )
    now = date(2026, 6, 18)

    deleted = prune_news(conn, keep_days=365, now=now, apply=True)

    assert deleted == 1
    rows = conn.execute("SELECT ts FROM news_items").fetchall()
    assert [r["ts"] for r in rows] == ["2026-06-01T10:00:00+00:00"]


def test_prune_news_dry_run_counts_without_deleting(conn: sqlite3.Connection) -> None:
    insert_news_items(conn, [_make_news("2024-01-01T10:00:00+00:00", 1)])

    deleted = prune_news(conn, keep_days=365, now=date(2026, 6, 18), apply=False)

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"] == 1


def test_prune_news_keeps_row_on_cutoff_boundary(conn: sqlite3.Connection) -> None:
    # keep_days=10, now=2026-06-18 → cutoff date 2026-06-08; that day is kept.
    insert_news_items(conn, [_make_news("2026-06-08T23:00:00+00:00", 1)])

    deleted = prune_news(conn, keep_days=10, now=date(2026, 6, 18), apply=True)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# run_retention orchestrator
# ---------------------------------------------------------------------------


def test_run_retention_prunes_both_and_reports(tmp_path, conn: sqlite3.Connection) -> None:
    paths = get_paths(tmp_path)
    (paths.raw_dir / "2025-01-01").mkdir(parents=True)
    insert_news_items(conn, [_make_news("2024-01-01T10:00:00+00:00", 1)])

    result = run_retention(paths, conn, as_of=date(2026, 6, 18), apply=True)

    assert result.applied is True
    assert result.raw_dirs_deleted == ["2025-01-01"]
    assert result.news_rows_deleted == 1
    assert result.raw_keep_days == DEFAULT_RAW_KEEP_DAYS
    assert result.news_keep_days == DEFAULT_NEWS_KEEP_DAYS
    assert not (paths.raw_dir / "2025-01-01").exists()


def test_run_retention_dry_run_default(tmp_path, conn: sqlite3.Connection) -> None:
    paths = get_paths(tmp_path)
    (paths.raw_dir / "2025-01-01").mkdir(parents=True)

    result = run_retention(paths, conn, as_of=date(2026, 6, 18))

    assert result.applied is False
    assert result.raw_dirs_deleted == ["2025-01-01"]
    assert (paths.raw_dir / "2025-01-01").exists()  # untouched
