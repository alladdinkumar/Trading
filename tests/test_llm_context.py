"""Tests for trading.llm.context — input bundle assembly."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.llm.context import ContextInputs, assemble_context
from trading.store.migrations import run_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def test_assemble_context_writes_file_with_header(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn,
        paths=paths,
        as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    assert "# Trading context bundle — 2026-05-15" in body
    assert "(mode: pre_open)" in body
    assert out == paths.research_dir / "2026-05-15" / "_context.md"


def _seed_macro(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO macro_snapshot
          (date, sgx_nifty, dow_fut, nasdaq_fut, sp500, usdinr, crude,
           vix, us_10y, fii_flow_cr, dii_flow_cr, regime)
        VALUES (?, NULL, NULL, NULL, NULL, ?, NULL, ?, NULL, ?, NULL, ?)
        """,
        ("2026-05-15", 95.76, 19.4, 187.0, "NEUTRAL"),
    )
    conn.commit()


def test_assemble_context_includes_macro_section(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_macro(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "VIX" in body and "19.4" in body
    assert "USDINR" in body and "95.76" in body
    assert "FII flow" in body and "187" in body
    assert "NEUTRAL" in body


def test_assemble_context_macro_no_data_when_missing(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "_(no data)_" in body
