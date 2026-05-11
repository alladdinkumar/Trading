"""Tests for trading.store.db — get_conn context manager."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading.store.db import get_conn


def test_get_conn_creates_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    assert not db_path.exists()
    with get_conn(db_path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    assert db_path.is_file()


def test_get_conn_auto_creates_parent_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dirs" / "test.db"
    with get_conn(db_path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    assert db_path.is_file()


def test_get_conn_enables_foreign_keys(tmp_path: Path) -> None:
    with get_conn(tmp_path / "fk.db") as conn:
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1


def test_get_conn_uses_wal(tmp_path: Path) -> None:
    with get_conn(tmp_path / "wal.db") as conn:
        cur = conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0].lower() == "wal"


def test_get_conn_row_factory_is_row(tmp_path: Path) -> None:
    with get_conn(tmp_path / "rf.db") as conn:
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hi')")
        row = conn.execute("SELECT * FROM t").fetchone()
        assert row["a"] == 1
        assert row["b"] == "hi"


def test_get_conn_commits_on_clean_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "commit.db"
    with get_conn(db_path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
    # Re-open and verify the row persisted
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT x FROM t").fetchone()
        assert row["x"] == 42


def test_get_conn_rolls_back_on_exception(tmp_path: Path) -> None:
    db_path = tmp_path / "rollback.db"
    # First, create the table in a separate transaction
    with get_conn(db_path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    # Now insert and raise — should not persist
    with pytest.raises(RuntimeError, match="boom"), get_conn(db_path) as conn:
        conn.execute("INSERT INTO t VALUES (99)")
        raise RuntimeError("boom")
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM t").fetchone()
        assert row["n"] == 0


def test_get_conn_fk_enforcement(tmp_path: Path) -> None:
    """Confirm FK constraint actually blocks bad inserts."""
    with get_conn(tmp_path / "enforce.db") as conn:
        conn.executescript(
            """
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child  (id INTEGER PRIMARY KEY,
                                 parent_id INTEGER NOT NULL,
                                 FOREIGN KEY (parent_id) REFERENCES parent(id));
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child (parent_id) VALUES (999)")
