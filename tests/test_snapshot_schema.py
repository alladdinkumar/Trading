"""Tests for trading.data.snapshot_schema — read-boundary validation (F-002)."""

from __future__ import annotations

import pytest

from trading.data.kite import GttOrder, Holding, Quote
from trading.data.snapshot_schema import (
    SnapshotSchemaError,
    validate_row,
    validate_rows,
)

_HOLDING = {
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


def test_validate_row_happy_returns_dataclass() -> None:
    out = validate_row(Holding, dict(_HOLDING), source="holdings.json")
    assert isinstance(out, Holding)
    assert out.tradingsymbol == "RVNL"
    assert out.quantity == 32


def test_missing_required_field_raises_with_field_and_source() -> None:
    bad = dict(_HOLDING)
    del bad["quantity"]
    with pytest.raises(SnapshotSchemaError) as exc:
        validate_row(Holding, bad, source="holdings.json")
    msg = str(exc.value)
    assert "quantity" in msg
    assert "holdings.json" in msg


def test_unexpected_field_raises() -> None:
    bad = dict(_HOLDING)
    bad["surprise"] = 1
    with pytest.raises(SnapshotSchemaError) as exc:
        validate_row(Holding, bad, source="holdings.json")
    assert "surprise" in str(exc.value)


def test_wrong_type_raises() -> None:
    bad = dict(_HOLDING)
    bad["quantity"] = "32"  # string, not int
    with pytest.raises(SnapshotSchemaError) as exc:
        validate_row(Holding, bad, source="holdings.json")
    assert "quantity" in str(exc.value)


def test_invalid_exchange_raises() -> None:
    bad = dict(_HOLDING)
    bad["exchange"] = "NYSE"  # not a Kite exchange
    with pytest.raises(SnapshotSchemaError) as exc:
        validate_row(Holding, bad, source="holdings.json")
    assert "exchange" in str(exc.value)
    assert "NYSE" in str(exc.value)


def test_null_in_non_optional_raises() -> None:
    bad = dict(_HOLDING)
    bad["last_price"] = None
    with pytest.raises(SnapshotSchemaError):
        validate_row(Holding, bad, source="holdings.json")


def test_null_allowed_in_optional_field() -> None:
    ok = dict(_HOLDING)
    ok["isin"] = None  # isin: str | None
    out = validate_row(Holding, ok, source="holdings.json")
    assert out.isin is None


def test_float_field_accepts_int() -> None:
    ok = dict(_HOLDING)
    ok["average_price"] = 305  # JSON int for a float field
    out = validate_row(Holding, ok, source="holdings.json")
    assert isinstance(out.average_price, float)
    assert out.average_price == 305.0


def test_int_field_rejects_bool() -> None:
    bad = dict(_HOLDING)
    bad["quantity"] = True  # bool sneaking in as int
    with pytest.raises(SnapshotSchemaError):
        validate_row(Holding, bad, source="holdings.json")


_GTT = {
    "id": 12345,
    "type": "single",
    "status": "active",
    "tradingsymbol": "RVNL",
    "exchange": "NSE",
    "trigger_values": [350.0],
    "last_price": 329.6,
    "created_at": "2026-05-10T10:00:00",
    "orders": [{"transaction_type": "SELL", "quantity": 32, "price": 350.0}],
}


def test_list_field_validates_element_types() -> None:
    bad = dict(_GTT)
    bad["trigger_values"] = [350.0, "oops"]
    with pytest.raises(SnapshotSchemaError) as exc:
        validate_row(GttOrder, bad, source="gtts.json")
    assert "trigger_values" in str(exc.value)


def test_gtt_happy_path_with_nested_orders() -> None:
    out = validate_row(GttOrder, dict(_GTT), source="gtts.json")
    assert isinstance(out, GttOrder)
    assert out.orders[0]["transaction_type"] == "SELL"
    assert out.trigger_values == [350.0]


_QUOTE = {
    "instrument_token": 2977281,
    "last_price": 395.25,
    "volume": 8123456,
    "open": 396.30,
    "high": 397.10,
    "low": 393.80,
    "close": 396.30,
    "bid": 395.20,
    "ask": 395.30,
    "oi": None,
    "upper_circuit_limit": 435.93,
    "lower_circuit_limit": 356.67,
}


def test_quote_optional_nulls_allowed() -> None:
    out = validate_row(Quote, dict(_QUOTE), source="quotes_1232.json")
    assert isinstance(out, Quote)
    assert out.oi is None


def test_validate_rows_rejects_non_list() -> None:
    with pytest.raises(SnapshotSchemaError) as exc:
        validate_rows(Holding, {"not": "a list"}, source="holdings.json")
    assert "holdings.json" in str(exc.value)


def test_validate_rows_reports_offending_index() -> None:
    bad = dict(_HOLDING)
    del bad["pnl"]
    with pytest.raises(SnapshotSchemaError) as exc:
        validate_rows(Holding, [dict(_HOLDING), bad], source="holdings.json")
    # second row (index 1) is the bad one
    assert "1" in str(exc.value)
    assert "pnl" in str(exc.value)
