"""Tests for trading.data.kite_snapshot — JSON readers + typed errors."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from tests.conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.data.kite import GttOrder, Holding, Position
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_gtts,
    read_holdings,
    read_positions,
)
from trading.data.snapshot_schema import SnapshotSchemaError


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


_GTT_ROW = {
    "id": 12345, "type": "single", "status": "active",
    "tradingsymbol": "RVNL", "exchange": "NSE",
    "trigger_values": [350.0], "last_price": 329.6,
    "created_at": "2026-05-10T10:00:00",
    "orders": [{"transaction_type": "SELL", "quantity": 32, "price": 350.0}],
}

_POSITION_ROW = {
    "tradingsymbol": "NTPC", "exchange": "NSE", "product": "CNC",
    "quantity": 10, "average_price": 303.0, "last_price": 305.5, "pnl": 25.0,
}


def test_read_gtts_happy_path(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), gtts=[_GTT_ROW])
    out = read_gtts(paths, date(2026, 5, 15))
    assert len(out) == 1
    assert isinstance(out[0], GttOrder)
    assert out[0].id == 12345
    assert out[0].trigger_values == [350.0]
    assert out[0].orders[0]["transaction_type"] == "SELL"


def test_read_gtts_missing_raises(paths) -> None:
    with pytest.raises(KiteSnapshotMissingError):
        read_gtts(paths, date(2026, 5, 15))


def test_read_holdings_invalid_exchange_raises(paths) -> None:
    """F-002: a wrong-exchange row is rejected at the read boundary, not splatted
    into a Holding that could drive a bad MTM/exit."""
    bad = dict(_HOLDING_ROW)
    bad["exchange"] = "NYSE"
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[bad])
    with pytest.raises(SnapshotSchemaError) as exc:
        read_holdings(paths, date(2026, 5, 15))
    assert "exchange" in str(exc.value)


def test_read_holdings_missing_field_raises(paths) -> None:
    """F-002: a malformed payload (missing required field) fails loudly with the
    field name + the offending file, instead of a cryptic TypeError."""
    bad = dict(_HOLDING_ROW)
    del bad["quantity"]
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[bad])
    with pytest.raises(SnapshotSchemaError) as exc:
        read_holdings(paths, date(2026, 5, 15))
    msg = str(exc.value)
    assert "quantity" in msg
    assert "holdings.json" in msg


def test_read_positions_happy_path(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), positions=[_POSITION_ROW])
    out = read_positions(paths, date(2026, 5, 15))
    assert len(out) == 1
    assert isinstance(out[0], Position)
    assert out[0].tradingsymbol == "NTPC"
    assert out[0].pnl == 25.0
