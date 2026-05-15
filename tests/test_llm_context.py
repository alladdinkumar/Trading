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
