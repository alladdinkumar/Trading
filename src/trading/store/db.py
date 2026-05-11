"""SQLite connection helper — single source of truth for how we open the DB."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with FK + WAL pragmas, auto-create parent dir.

    - `foreign_keys = ON` so CHECK and FK constraints actually enforce.
    - `journal_mode = WAL` so reads don't block writers (helps Streamlit + jobs).
    - Row factory `sqlite3.Row` so callers can index by column name.
    - Commits on clean exit, rolls back on exception, always closes.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level="DEFERRED",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
