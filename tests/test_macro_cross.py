"""Tests for trading.data.macro_cross — validated reader for the Kite MCP
cross-source macro file (F-035)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from trading.data.macro_cross import (
    MacroCrossSource,
    MacroCrossStaleError,
    read_macro_cross,
)
from trading.data.snapshot_schema import SnapshotSchemaError


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_read_macro_cross_parses_full_file(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "macro_cross_0815.json",
        {
            "source": "kite_mcp",
            "captured_at": "2026-06-19T08:15:00",
            "vix": 19.55,
            "usdinr": 83.20,
        },
    )
    cross = read_macro_cross(f)
    assert cross == MacroCrossSource(
        source="kite_mcp",
        captured_at="2026-06-19T08:15:00",
        vix=19.55,
        usdinr=83.20,
    )


def test_read_macro_cross_allows_missing_optional_field(tmp_path: Path) -> None:
    """The skill may write only the fields Kite returned — absent figures → None."""
    f = _write(
        tmp_path / "macro_cross_0815.json",
        {"source": "kite_mcp", "captured_at": "2026-06-19T08:15:00", "vix": 19.55},
    )
    cross = read_macro_cross(f)
    assert cross.vix == 19.55
    assert cross.usdinr is None


def test_read_macro_cross_rejects_non_numeric_figure(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "macro_cross_0815.json",
        {"source": "kite_mcp", "captured_at": "2026-06-19T08:15:00", "vix": "high"},
    )
    with pytest.raises(SnapshotSchemaError):
        read_macro_cross(f)


def test_read_macro_cross_rejects_missing_required_field(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "macro_cross_0815.json",
        {"captured_at": "2026-06-19T08:15:00", "vix": 19.55},
    )
    with pytest.raises(SnapshotSchemaError):
        read_macro_cross(f)


def test_read_macro_cross_rejects_non_object_json(tmp_path: Path) -> None:
    f = _write(tmp_path / "macro_cross_0815.json", [1, 2, 3])
    with pytest.raises(SnapshotSchemaError):
        read_macro_cross(f)


def test_read_macro_cross_rejects_off_date_cross(tmp_path: Path) -> None:
    """F-063: a cross-file captured on a different day than the refresh date is
    stale — reusing it would gap-fill today's snapshot with a days-old figure."""
    f = _write(
        tmp_path / "macro_cross_1010.json",
        {"source": "kite_mcp", "captured_at": "2026-06-28T10:10:00", "vix": 19.55},
    )
    with pytest.raises(MacroCrossStaleError):
        read_macro_cross(f, as_of=date(2026, 7, 1))


def test_read_macro_cross_accepts_same_date_cross(tmp_path: Path) -> None:
    """F-063: a cross-file captured on the refresh date passes the freshness check."""
    f = _write(
        tmp_path / "macro_cross_1010.json",
        {"source": "kite_mcp", "captured_at": "2026-07-01T10:10:00", "vix": 19.55},
    )
    cross = read_macro_cross(f, as_of=date(2026, 7, 1))
    assert cross.vix == 19.55
