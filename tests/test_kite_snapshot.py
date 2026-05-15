"""Tests for trading.data.kite_snapshot — JSON readers + typed errors."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from tests.conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.data.kite import Holding
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_holdings,
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_HOLDING_ROW = {
    "tradingsymbol": "RVNL",
    "exchange": "NSE",
    "isin": "INE415G01027",
    "quantity": 32,
    "average_price": 305.0,
    "last_price": 329.6,
    "close_price": 327.1,
    "pnl": 787.2,
    "day_change": 2.5,
    "day_change_percentage": 0.76,
}


def test_read_holdings_happy_path(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[_HOLDING_ROW])
    out = read_holdings(paths, date(2026, 5, 15))
    assert len(out) == 1
    assert isinstance(out[0], Holding)
    assert out[0].tradingsymbol == "RVNL"
    assert out[0].quantity == 32
    assert out[0].pnl == 787.2


def test_read_holdings_returns_empty_list(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[])
    assert read_holdings(paths, date(2026, 5, 15)) == []


def test_read_holdings_missing_raises(paths) -> None:
    with pytest.raises(KiteSnapshotMissingError) as exc:
        read_holdings(paths, date(2026, 5, 15))
    msg = str(exc.value)
    assert "holdings.json" in msg
    assert "/kite-snapshot" in msg


def test_read_holdings_stale_raises(paths) -> None:
    yesterday = datetime(2026, 5, 14, 16, 30)
    seed_kite_snapshot(
        paths, date(2026, 5, 15),
        holdings=[_HOLDING_ROW],
        snapshot_at=yesterday,
    )
    with pytest.raises(KiteSnapshotStaleError) as exc:
        read_holdings(paths, date(2026, 5, 15))
    assert "2026-05-14" in str(exc.value)
