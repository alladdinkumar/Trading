"""Tests for trading.paper.pending — the pre-open → open-fills handoff file."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.paper.pending import (
    PendingEntriesMissingError,
    PendingEntry,
    read_pending_entries,
    write_pending_entries,
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def test_write_then_read_roundtrips(paths) -> None:
    entries = [
        PendingEntry(symbol="TATASTEEL", atr_14=4.30, ml_score=0.61, ref_close=198.97),
        PendingEntry(symbol="COALINDIA", atr_14=11.2, ml_score=None, ref_close=449.0),
    ]
    out = write_pending_entries(paths, date(2026, 6, 23), regime="NEUTRAL", entries=entries)
    assert out.name == "_pending_entries.json"

    regime, got = read_pending_entries(paths, date(2026, 6, 23))
    assert regime == "NEUTRAL"
    assert got == entries


def test_write_empty_entries_roundtrips(paths) -> None:
    write_pending_entries(paths, date(2026, 6, 23), regime="BULL", entries=[])
    regime, got = read_pending_entries(paths, date(2026, 6, 23))
    assert regime == "BULL"
    assert got == []


def test_read_missing_raises(paths) -> None:
    with pytest.raises(PendingEntriesMissingError):
        read_pending_entries(paths, date(2026, 6, 23))
