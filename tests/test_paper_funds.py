"""Tests for trading.paper.funds — the capital top-ups ledger."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from trading.paper.funds import FundsDeposit, add_funds, list_funds, total_funds_added
from trading.store.migrations import run_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


def test_add_funds_returns_deposit_and_persists(conn: sqlite3.Connection) -> None:
    dep = add_funds(conn, amount=50_000.0, date="2026-06-19", note="June top-up")
    assert isinstance(dep, FundsDeposit)
    assert dep.id == 1
    assert dep.amount == 50_000.0
    assert dep.date == "2026-06-19"
    assert dep.note == "June top-up"
    assert dep.created_at  # non-empty ISO timestamp
    rows = conn.execute("SELECT amount FROM cash_ledger").fetchall()
    assert [r["amount"] for r in rows] == [50_000.0]


def test_add_funds_rejects_non_positive(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        add_funds(conn, amount=0.0, date="2026-06-19")
    with pytest.raises(ValueError):
        add_funds(conn, amount=-100.0, date="2026-06-19")


def test_list_funds_orders_by_date_then_id(conn: sqlite3.Connection) -> None:
    add_funds(conn, amount=10.0, date="2026-06-19")
    add_funds(conn, amount=20.0, date="2026-06-17")
    add_funds(conn, amount=30.0, date="2026-06-19")
    got = [(d.date, d.amount) for d in list_funds(conn)]
    assert got == [("2026-06-17", 20.0), ("2026-06-19", 10.0), ("2026-06-19", 30.0)]


def test_total_funds_added_filters_by_as_of(conn: sqlite3.Connection) -> None:
    add_funds(conn, amount=10_000.0, date="2026-06-10")
    add_funds(conn, amount=25_000.0, date="2026-06-20")
    assert total_funds_added(conn, as_of=date(2026, 6, 15)) == 10_000.0
    assert total_funds_added(conn, as_of=date(2026, 6, 30)) == 35_000.0


def test_total_funds_added_zero_when_empty(conn: sqlite3.Connection) -> None:
    assert total_funds_added(conn, as_of=date(2026, 6, 19)) == 0.0
