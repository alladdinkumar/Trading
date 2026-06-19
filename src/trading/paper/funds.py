"""Paper-trading funds ledger — capital top-ups on top of INITIAL_CAPITAL.

The initial paper capital stays a constant seed (see
`trading.paper.reconcile.INITIAL_CAPITAL`); this ledger records *additional*
deposits the user makes over time. `compute_paper_cash` adds the running sum
to its seed, so a top-up raises available cash without disturbing the existing
trade-derived cash math (an empty ledger sums to 0.0).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class FundsDeposit:
    """One row in `cash_ledger` — a single capital top-up."""

    id: int
    date: str
    amount: float
    note: str | None
    created_at: str


def add_funds(
    conn: sqlite3.Connection,
    *,
    amount: float,
    date: str,
    note: str | None = None,
) -> FundsDeposit:
    """Record a capital top-up. Rejects `amount <= 0` (deposits only).

    `date` is the caller's responsibility (CLI passes today or `--date`);
    `created_at` is stamped here with the wall clock.
    """
    if amount <= 0:
        raise ValueError(f"funds amount must be positive, got {amount!r}")
    created_at = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO cash_ledger (date, amount, note, created_at) VALUES (?, ?, ?, ?)",
        (date, amount, note, created_at),
    )
    conn.commit()
    return FundsDeposit(
        id=int(cur.lastrowid),
        date=date,
        amount=amount,
        note=note,
        created_at=created_at,
    )


def list_funds(conn: sqlite3.Connection) -> list[FundsDeposit]:
    """All deposits ordered by date, then insertion id."""
    rows = conn.execute(
        "SELECT id, date, amount, note, created_at FROM cash_ledger ORDER BY date, id"
    ).fetchall()
    return [
        FundsDeposit(
            id=int(r["id"]),
            date=str(r["date"]),
            amount=float(r["amount"]),
            note=r["note"],
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


def total_funds_added(conn: sqlite3.Connection, *, as_of: date) -> float:
    """Sum of top-ups with `date <= as_of` (0.0 if none).

    Date-filtered so re-running an older `as_of` reproduces the balance as it
    stood that day — mirrors `compute_paper_cash`.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0.0) AS total FROM cash_ledger WHERE date <= ?",
        (as_of.isoformat(),),
    ).fetchone()
    return float(row["total"]) if row is not None else 0.0
