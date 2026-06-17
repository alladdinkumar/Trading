"""Tests for trading.store.migrations — schema v1 bootstrap."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading.store.db import get_conn
from trading.store.migrations import (
    CURRENT_VERSION,
    SCHEMA_V1,
    SCHEMA_V2,
    run_migrations,
)

EXPECTED_TABLES = {
    "schema_version",
    "signals",
    "paper_trades",
    "predictions",
    "portfolio_snapshots",
    "news_items",
    "sentiment_daily",
    "sector_daily",
    "macro_snapshot",
    "oi_daily",
    "fno_ban_list",
    "bulk_block_deals",
    "corp_actions",
    "account_events",
    "preopen_snapshot",
    "live_quotes",
    "event_calendar",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_run_migrations_creates_all_tables(tmp_path: Path) -> None:
    with get_conn(tmp_path / "m.db") as conn:
        version = run_migrations(conn)
    assert version == CURRENT_VERSION
    with get_conn(tmp_path / "m.db") as conn:
        assert _table_names(conn) == EXPECTED_TABLES


def test_schema_version_row_recorded(tmp_path: Path) -> None:
    with get_conn(tmp_path / "v.db") as conn:
        run_migrations(conn)
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_version ORDER BY version"
        ).fetchall()
    assert len(rows) == CURRENT_VERSION
    assert [r["version"] for r in rows] == list(range(1, CURRENT_VERSION + 1))
    assert all(r["applied_at"] for r in rows)


def test_run_migrations_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "i.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        run_migrations(conn)
        run_migrations(conn)
        rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
    # Each version applies exactly once across re-runs.
    assert rows["n"] == CURRENT_VERSION


def test_signals_side_check_constraint(tmp_path: Path) -> None:
    with get_conn(tmp_path / "c.db") as conn:
        run_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days) "
                "VALUES ('2026-05-11T09:10:00', 'RVNL', 'HOLD', 300, 285, 360, 15)"
            )


def test_paper_trades_exit_reason_check(tmp_path: Path) -> None:
    with get_conn(tmp_path / "e.db") as conn:
        run_migrations(conn)
        sig_id = conn.execute(
            "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days) "
            "VALUES ('2026-05-11T09:10:00', 'RVNL', 'LONG', 300, 285, 360, 15)"
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, exit_reason) "
                "VALUES (?, '2026-05-12T09:15:00', 305, 10, 'FAKE')",
                (sig_id,),
            )


def test_oi_daily_opt_type_check(tmp_path: Path) -> None:
    with get_conn(tmp_path / "o.db") as conn:
        run_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO oi_daily (date, symbol, expiry, strike, opt_type, oi) "
                "VALUES ('2026-05-11', 'NIFTY', '2026-05-29', 24000, 'XX', 1000)"
            )


def test_macro_regime_check(tmp_path: Path) -> None:
    with get_conn(tmp_path / "r.db") as conn:
        run_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO macro_snapshot (date, regime) VALUES ('2026-05-11', 'BAD')")


def test_news_items_unique_index_rejects_duplicate(tmp_path: Path) -> None:
    """F-016: a UNIQUE index on (source, headline, COALESCE(url,'')) blocks a raw
    duplicate insert (the store uses INSERT OR IGNORE to lean on it)."""
    with get_conn(tmp_path / "n.db") as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO news_items (ts, source, headline, url) "
            "VALUES ('t', 'mc', 'Reliance Q4 beat', 'https://x/1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO news_items (ts, source, headline, url) "
                "VALUES ('t', 'mc', 'Reliance Q4 beat', 'https://x/1')"
            )


def test_v3_collapses_preexisting_duplicate_news_rows(tmp_path: Path) -> None:
    """Upgrading a v2 DB that already holds duplicate news rows collapses them to
    one before installing the unique index (F-016)."""
    db = tmp_path / "dup.db"
    with get_conn(db) as conn:
        # Stand the DB up at v2 (no dedup index yet) and seed duplicate rows.
        conn.executescript(SCHEMA_V1)
        conn.executescript(SCHEMA_V2)
        conn.executemany(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, 'x')",
            [(1,), (2,)],
        )
        dup = ("2026-05-13T10:00:00+00:00", "nse_events", "RVNL: Board Meeting", "https://nse?s=RVNL")
        conn.executemany(
            "INSERT INTO news_items (ts, source, headline, url) VALUES (?, ?, ?, ?)",
            [dup, dup, dup],
        )
        run_migrations(conn)  # applies v3 → dedup + unique index
        count = conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"]
    assert count == 1


def test_pk_composite_sentiment_daily(tmp_path: Path) -> None:
    with get_conn(tmp_path / "s.db") as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO sentiment_daily (date, symbol, score_7d) "
            "VALUES ('2026-05-11', 'RVNL', 0.5)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sentiment_daily (date, symbol, score_7d) "
                "VALUES ('2026-05-11', 'RVNL', 0.6)"
            )
