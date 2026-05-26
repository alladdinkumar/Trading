"""Tests for trading.store.sector_store."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from trading.data.sector import SectorRow
from trading.store.migrations import run_migrations
from trading.store.sector_store import get_sector_daily, upsert_sector_daily


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _row(
    sector: str, *, rs_20d: float | None = 0.01, regime: str | None = "NEUTRAL"
) -> SectorRow:
    return SectorRow(
        date="2026-05-26",
        sector=sector,
        close=42000.0,
        rs_5d=0.005,
        rs_20d=rs_20d,
        rs_60d=-0.01,
        regime=regime,
    )


def test_upsert_then_get_round_trip(conn: sqlite3.Connection) -> None:
    rows = [_row("IT"), _row("NIFTYBANK", rs_20d=0.035, regime="LEADING")]
    n = upsert_sector_daily(conn, rows)
    assert n == 2
    fetched = get_sector_daily(conn, date(2026, 5, 26))
    assert len(fetched) == 2
    by_sector = {r.sector: r for r in fetched}
    assert by_sector["IT"].rs_5d == 0.005
    assert by_sector["NIFTYBANK"].regime == "LEADING"


def test_upsert_overwrites_on_conflict(conn: sqlite3.Connection) -> None:
    upsert_sector_daily(conn, [_row("IT", rs_20d=0.0, regime="NEUTRAL")])
    upsert_sector_daily(conn, [_row("IT", rs_20d=0.05, regime="LEADING")])
    rows = get_sector_daily(conn, date(2026, 5, 26))
    assert len(rows) == 1
    assert rows[0].rs_20d == 0.05
    assert rows[0].regime == "LEADING"


def test_get_sector_daily_returns_empty_when_no_rows(conn: sqlite3.Connection) -> None:
    assert get_sector_daily(conn, date(2026, 5, 26)) == []
