"""Tests for trading.store.fno_ban_store — fno_ban_list writer/reader (F-010)."""

from __future__ import annotations

from pathlib import Path

from trading.store.db import get_conn
from trading.store.fno_ban_store import get_fno_ban_symbols, replace_fno_ban_list
from trading.store.migrations import run_migrations


def test_replace_then_get_roundtrips(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "GNFC"])
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["GNFC", "IDEA"]  # ORDER BY symbol


def test_replace_overwrites_same_date(tmp_path: Path) -> None:
    db = tmp_path / "b2.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "GNFC"])
        replace_fno_ban_list(conn, "2026-06-19", ["TATASTEEL"])
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["TATASTEEL"]


def test_empty_list_clears_the_date(tmp_path: Path) -> None:
    db = tmp_path / "b3.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA"])
        replace_fno_ban_list(conn, "2026-06-19", [])
        assert get_fno_ban_symbols(conn, "2026-06-19") == []


def test_duplicate_input_does_not_violate_pk(tmp_path: Path) -> None:
    db = tmp_path / "b4.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "IDEA"])
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["IDEA"]
