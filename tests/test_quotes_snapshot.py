"""Tests for trading.data.quotes_snapshot — JSON readers + typed errors."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from freezegun import freeze_time

from trading.config import get_paths
from trading.data.kite import Quote
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_QUOTE_ROW = {
    "instrument_token": 2977281,
    "last_price": 395.25,
    "volume": 8123456,
    "open": 396.30, "high": 397.10, "low": 393.80, "close": 396.30,
    "bid": 395.20, "ask": 395.30,
    "oi": None,
    "upper_circuit_limit": 435.93, "lower_circuit_limit": 356.67,
    "tradingsymbol": "NTPC",
}


def _seed_quotes(paths, as_of: date, hhmm: str, rows: list) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(json.dumps(rows), encoding="utf-8")
    return target


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_happy_path(paths) -> None:
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert "NTPC" in quotes
    assert isinstance(quotes["NTPC"], Quote)
    assert quotes["NTPC"].last_price == 395.25
    assert capture_ts == datetime(2026, 5, 16, 12, 32)


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_picks_newest_when_multiple(paths) -> None:
    older = dict(_QUOTE_ROW)
    older["last_price"] = 390.0
    _seed_quotes(paths, date(2026, 5, 16), "1100", [older])
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert quotes["NTPC"].last_price == 395.25
    assert capture_ts == datetime(2026, 5, 16, 12, 32)


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_missing_raises(paths) -> None:
    with pytest.raises(QuoteSnapshotMissingError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    assert "/kite-quotes-snapshot" in str(exc.value)


@freeze_time("2026-05-16T15:30:00")
def test_read_latest_quotes_stale_raises(paths) -> None:
    """Newest snapshot is from 12:32; default max_age is 30 min, now is 15:30."""
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    with pytest.raises(QuoteSnapshotStaleError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    msg = str(exc.value)
    assert "stale" in msg.lower()
    assert "12:32" in msg or "1232" in msg


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_empty_list_returns_empty_dict(paths) -> None:
    _seed_quotes(paths, date(2026, 5, 16), "1232", [])
    quotes, _ = read_latest_quotes(paths, date(2026, 5, 16))
    assert quotes == {}
