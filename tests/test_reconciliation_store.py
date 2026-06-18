"""Tests for trading.store.reconciliation_store — macro_reconciliation rows (F-035)."""

from __future__ import annotations

from pathlib import Path

from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.reconciliation_store import (
    ReconRow,
    get_reconciliation_rows,
    upsert_reconciliation_row,
)


def _row(**over: object) -> ReconRow:
    base = dict(
        date="2026-06-19",
        field="vix",
        primary_value=19.40,
        primary_source="yfinance",
        secondary_value=19.55,
        secondary_source="kite_mcp",
        abs_delta=0.15,
        status="ok",
        checked_at="2026-06-19T08:15:00",
    )
    base.update(over)
    return ReconRow(**base)  # type: ignore[arg-type]


def test_upsert_then_get_roundtrips(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        upsert_reconciliation_row(conn, _row())
        rows = get_reconciliation_rows(conn, "2026-06-19")
    assert rows == [_row()]


def test_upsert_is_idempotent_on_date_field(tmp_path: Path) -> None:
    """A second upsert for the same (date, field) overwrites rather than duplicates."""
    db = tmp_path / "r2.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        upsert_reconciliation_row(conn, _row(status="ok", secondary_value=19.55))
        upsert_reconciliation_row(conn, _row(status="mismatch", secondary_value=22.0, abs_delta=2.6))
        rows = get_reconciliation_rows(conn, "2026-06-19")
    assert len(rows) == 1
    assert rows[0].status == "mismatch"
    assert rows[0].secondary_value == 22.0


def test_upsert_preserves_null_values(tmp_path: Path) -> None:
    db = tmp_path / "r3.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        upsert_reconciliation_row(
            conn,
            _row(
                field="usdinr",
                primary_value=None,
                secondary_value=83.2,
                abs_delta=None,
                status="missing_primary",
            ),
        )
        rows = get_reconciliation_rows(conn, "2026-06-19")
    assert rows[0].primary_value is None
    assert rows[0].abs_delta is None
    assert rows[0].status == "missing_primary"
