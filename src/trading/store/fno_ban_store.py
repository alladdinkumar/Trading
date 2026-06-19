"""Persistence helpers for `fno_ban_list` (one row per (date, symbol)).

Populated daily by `pre_open._step_fno_ban` from the NSE F&O ban CSV and read
back by `build_scan_context` into `ScanContext.fno_ban_symbols`, which Layer A's
`passes_not_fno_banned` gate vetoes against (F-010).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def replace_fno_ban_list(conn: sqlite3.Connection, date_iso: str, symbols: Iterable[str]) -> None:
    """Replace the ban list for `date_iso` (delete-then-insert; idempotent).

    An empty `symbols` clears the date. Duplicate inputs are collapsed so the
    (date, symbol) primary key is never violated.
    """
    conn.execute("DELETE FROM fno_ban_list WHERE date = ?", (date_iso,))
    deduped = list(dict.fromkeys(symbols))
    conn.executemany(
        "INSERT INTO fno_ban_list (date, symbol) VALUES (?, ?)",
        [(date_iso, s) for s in deduped],
    )


def get_fno_ban_symbols(conn: sqlite3.Connection, date_iso: str) -> list[str]:
    """Return the banned symbols for `date_iso`, ordered by symbol."""
    rows = conn.execute(
        "SELECT symbol FROM fno_ban_list WHERE date = ? ORDER BY symbol",
        (date_iso,),
    ).fetchall()
    return [r["symbol"] for r in rows]
