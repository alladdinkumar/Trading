"""Persistence helpers for `macro_reconciliation` (one row per (date, field)).

The side table records the cross-source audit trail for the macro snapshot
(F-035): which feed supplied a figure, what the Kite MCP second source said,
their delta, and a status flag. `trading macro refresh` writes provenance rows
when it gap-fills from the cross source; `trading macro verify` (F-036) writes
reconciliation outcomes; `context._render_macro` reads them back to annotate the
bundle.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# The closed set of reconciliation outcomes, mirrored by the CHECK constraint in
# migration v4. Kept here so producers can reference the names without restating
# the literals.
RECON_STATUSES = (
    "ok",
    "mismatch",
    "missing_primary",
    "missing_secondary",
    "unreconciled",
)


@dataclass(frozen=True)
class ReconRow:
    """One `macro_reconciliation` row. Value/delta fields are nullable."""

    date: str  # YYYY-MM-DD
    field: str  # macro field name, e.g. 'vix', 'usdinr'
    primary_value: float | None
    primary_source: str | None
    secondary_value: float | None
    secondary_source: str | None
    abs_delta: float | None
    status: str  # one of RECON_STATUSES
    checked_at: str  # ISO timestamp


def upsert_reconciliation_row(conn: sqlite3.Connection, row: ReconRow) -> None:
    """Insert-or-replace one reconciliation row keyed on (date, field)."""
    conn.execute(
        """
        INSERT INTO macro_reconciliation (
          date, field, primary_value, primary_source,
          secondary_value, secondary_source, abs_delta, status, checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, field) DO UPDATE SET
          primary_value    = excluded.primary_value,
          primary_source   = excluded.primary_source,
          secondary_value  = excluded.secondary_value,
          secondary_source = excluded.secondary_source,
          abs_delta        = excluded.abs_delta,
          status           = excluded.status,
          checked_at       = excluded.checked_at
        """,
        (
            row.date,
            row.field,
            row.primary_value,
            row.primary_source,
            row.secondary_value,
            row.secondary_source,
            row.abs_delta,
            row.status,
            row.checked_at,
        ),
    )


def get_reconciliation_rows(conn: sqlite3.Connection, date_iso: str) -> list[ReconRow]:
    """Return all reconciliation rows for `date_iso`, ordered by field."""
    rows = conn.execute(
        "SELECT * FROM macro_reconciliation WHERE date = ? ORDER BY field",
        (date_iso,),
    ).fetchall()
    return [
        ReconRow(
            date=r["date"],
            field=r["field"],
            primary_value=r["primary_value"],
            primary_source=r["primary_source"],
            secondary_value=r["secondary_value"],
            secondary_source=r["secondary_source"],
            abs_delta=r["abs_delta"],
            status=r["status"],
            checked_at=r["checked_at"],
        )
        for r in rows
    ]
