"""Persistence helpers for `sector_daily` (one row per (date, sector))."""

from __future__ import annotations

import sqlite3
from datetime import date

from trading.data.sector import SectorRow


def upsert_sector_daily(conn: sqlite3.Connection, rows: list[SectorRow]) -> int:
    """INSERT ON CONFLICT(date, sector) DO UPDATE per row. Returns count written."""
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO sector_daily (date, sector, close, rs_5d, rs_20d, rs_60d, regime)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, sector) DO UPDATE SET
          close  = excluded.close,
          rs_5d  = excluded.rs_5d,
          rs_20d = excluded.rs_20d,
          rs_60d = excluded.rs_60d,
          regime = excluded.regime
        """,
        [
            (r.date, r.sector, r.close, r.rs_5d, r.rs_20d, r.rs_60d, r.regime)
            for r in rows
        ],
    )
    return len(rows)


def get_sector_daily(conn: sqlite3.Connection, as_of: date) -> list[SectorRow]:
    """All sector_daily rows for one date, ordered by sector. [] if none."""
    cursor = conn.execute(
        "SELECT date, sector, close, rs_5d, rs_20d, rs_60d, regime "
        "FROM sector_daily WHERE date = ? ORDER BY sector",
        (as_of.isoformat(),),
    )
    return [
        SectorRow(
            date=row["date"],
            sector=row["sector"],
            close=row["close"],
            rs_5d=row["rs_5d"],
            rs_20d=row["rs_20d"],
            rs_60d=row["rs_60d"],
            regime=row["regime"],
        )
        for row in cursor.fetchall()
    ]
