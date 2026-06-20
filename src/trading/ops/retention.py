"""Data retention / prune (F-013).

`data/raw/<YYYY-MM-DD>/` snapshot dirs and the append-only `news_items` table
both grow without bound. This module prunes the stale tail of each:

- `prune_raw_dirs` — removes only `raw_dir/` children whose name parses as an
  ISO date older than the cutoff. Non-date entries (a stray README, a `static/`
  helper) are never touched.
- `prune_news` — deletes `news_items` rows older than the cutoff. The derived
  `sentiment_daily` rollups are *kept* (tiny, and the live 30-day health scorer
  reads them), so dropping old raw news is lossless for the live path.

Both default to **dry-run**: they report what *would* go but only delete when
`apply=True`, mirroring the apply discipline of the daily jobs. `run_retention`
runs both and is wired into the Sunday `weekly_train` housekeeping slot.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from trading.clock import today_ist
from trading.config import Paths

DEFAULT_RAW_KEEP_DAYS = 30
DEFAULT_NEWS_KEEP_DAYS = 365


@dataclass(frozen=True)
class RetentionResult:
    as_of: date
    raw_keep_days: int
    news_keep_days: int
    raw_dirs_deleted: list[str]
    news_rows_deleted: int
    applied: bool


def _parse_date_dirname(name: str) -> date | None:
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def prune_raw_dirs(paths: Paths, *, keep_days: int, now: date, apply: bool) -> list[str]:
    """Remove `raw_dir/` date-dirs older than `now - keep_days`.

    Returns the sorted names that were (or would be) deleted. Only directories
    whose name is a valid ISO date are eligible; everything else is left alone.
    """
    cutoff = now - timedelta(days=keep_days)
    raw = paths.raw_dir
    if not raw.is_dir():
        return []
    deleted: list[str] = []
    for child in sorted(raw.iterdir()):
        if not child.is_dir():
            continue
        d = _parse_date_dirname(child.name)
        if d is None or d >= cutoff:
            continue
        deleted.append(child.name)
        if apply:
            shutil.rmtree(child)
    return deleted


def prune_news(conn: sqlite3.Connection, *, keep_days: int, now: date, apply: bool) -> int:
    """Delete `news_items` whose date is older than `now - keep_days`.

    `ts` is full ISO 8601; comparing its `YYYY-MM-DD` prefix lexically against
    the cutoff date is exact for date ordering. Returns the row count removed
    (or, in dry-run, that would be removed).
    """
    cutoff_iso = (now - timedelta(days=keep_days)).isoformat()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM news_items WHERE substr(ts, 1, 10) < ?",
        (cutoff_iso,),
    ).fetchone()["c"]
    if apply and n:
        conn.execute("DELETE FROM news_items WHERE substr(ts, 1, 10) < ?", (cutoff_iso,))
    return int(n)


def run_retention(
    paths: Paths,
    conn: sqlite3.Connection,
    *,
    as_of: date | None = None,
    raw_keep_days: int = DEFAULT_RAW_KEEP_DAYS,
    news_keep_days: int = DEFAULT_NEWS_KEEP_DAYS,
    apply: bool = False,
) -> RetentionResult:
    """Prune raw date-dirs and old news rows. Dry-run unless `apply=True`."""
    d = as_of or today_ist()
    raw_deleted = prune_raw_dirs(paths, keep_days=raw_keep_days, now=d, apply=apply)
    news_deleted = prune_news(conn, keep_days=news_keep_days, now=d, apply=apply)
    return RetentionResult(
        as_of=d,
        raw_keep_days=raw_keep_days,
        news_keep_days=news_keep_days,
        raw_dirs_deleted=raw_deleted,
        news_rows_deleted=news_deleted,
        applied=apply,
    )
