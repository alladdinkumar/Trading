"""Tests for trading.data.quotes_snapshot — JSON readers + typed errors."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from freezegun import freeze_time

from trading import clock
from trading.clock import IST
from trading.config import get_paths
from trading.data.kite import Quote
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)
from trading.data.snapshot_schema import SnapshotSchemaError


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_QUOTE_ROW = {
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
    "tradingsymbol": "NTPC",
}


def _seed_quotes(paths, as_of: date, hhmm: str, rows: list) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(json.dumps(rows), encoding="utf-8")
    return target


def _mock_now_ist(monkeypatch: pytest.MonkeyPatch, iso: str) -> None:
    """Patch `trading.clock.now_ist()` to a fixed tz-aware IST instant.

    F-058: the staleness gate must read `trading.clock.now_ist()`, not the
    host clock. Patching the clock module's attribute (rather than freezing
    the host's `datetime.now()`) proves the gate actually calls through the
    canonical clock, regardless of what the host system clock/timezone says.
    """
    fixed = datetime.fromisoformat(iso)
    monkeypatch.setattr(clock, "now_ist", lambda: fixed)


def test_read_latest_quotes_happy_path(paths, monkeypatch) -> None:
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert "NTPC" in quotes
    assert isinstance(quotes["NTPC"], Quote)
    assert quotes["NTPC"].last_price == 395.25
    assert capture_ts == datetime(2026, 5, 16, 12, 32, tzinfo=IST)


def test_read_latest_quotes_picks_newest_when_multiple(paths, monkeypatch) -> None:
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    older = dict(_QUOTE_ROW)
    older["last_price"] = 390.0
    _seed_quotes(paths, date(2026, 5, 16), "1100", [older])
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert quotes["NTPC"].last_price == 395.25
    assert capture_ts == datetime(2026, 5, 16, 12, 32, tzinfo=IST)


def test_read_latest_quotes_missing_raises(paths, monkeypatch) -> None:
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    with pytest.raises(QuoteSnapshotMissingError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    assert "/kite-quotes-snapshot" in str(exc.value)


def test_read_latest_quotes_stale_raises(paths, monkeypatch) -> None:
    """Newest snapshot is from 12:32; default max_age is 30 min, now is 15:30."""
    _mock_now_ist(monkeypatch, "2026-05-16T15:30:00+05:30")
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    with pytest.raises(QuoteSnapshotStaleError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    msg = str(exc.value)
    assert "stale" in msg.lower()
    assert "12:32" in msg or "1232" in msg


def test_read_latest_quotes_empty_list_returns_empty_dict(paths, monkeypatch) -> None:
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    _seed_quotes(paths, date(2026, 5, 16), "1232", [])
    quotes, _ = read_latest_quotes(paths, date(2026, 5, 16))
    assert quotes == {}


def test_read_latest_quotes_wrong_type_raises(paths, monkeypatch) -> None:
    """F-002: a malformed quote (wrong type) is rejected at the read boundary."""
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    bad = dict(_QUOTE_ROW)
    bad["last_price"] = "395.25"  # string, not float
    _seed_quotes(paths, date(2026, 5, 16), "1232", [bad])
    with pytest.raises(SnapshotSchemaError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    assert "last_price" in str(exc.value)


def test_read_latest_quotes_missing_tradingsymbol_raises(paths, monkeypatch) -> None:
    """F-002: a quote row without its symbol key fails loudly, not with a bare
    KeyError mid-loop."""
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    bad = dict(_QUOTE_ROW)
    del bad["tradingsymbol"]
    _seed_quotes(paths, date(2026, 5, 16), "1232", [bad])
    with pytest.raises(SnapshotSchemaError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    assert "tradingsymbol" in str(exc.value)


def test_read_latest_quotes_ignores_malformed_filenames(paths, monkeypatch) -> None:
    """quotes_2400.json should be ignored (invalid hour); valid one used."""
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    base = paths.raw_dir / "2026-05-16"
    base.mkdir(parents=True, exist_ok=True)
    # Malformed file — invalid hour. Should NOT match the tightened regex.
    (base / "quotes_2400.json").write_text(json.dumps([_QUOTE_ROW]), encoding="utf-8")
    # Valid file
    (base / "quotes_1232.json").write_text(json.dumps([_QUOTE_ROW]), encoding="utf-8")
    quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert "NTPC" in quotes
    assert capture_ts == datetime(2026, 5, 16, 12, 32, tzinfo=IST)


def test_read_latest_quotes_only_malformed_files_raises_missing(paths, monkeypatch) -> None:
    """If only malformed files exist, behave as if no snapshot file is present."""
    _mock_now_ist(monkeypatch, "2026-05-16T12:33:00+05:30")
    base = paths.raw_dir / "2026-05-16"
    base.mkdir(parents=True, exist_ok=True)
    (base / "quotes_2400.json").write_text("[]", encoding="utf-8")
    (base / "quotes_9999.json").write_text("[]", encoding="utf-8")
    with pytest.raises(QuoteSnapshotMissingError):
        read_latest_quotes(paths, date(2026, 5, 16))


def test_read_latest_quotes_stale_gate_ignores_host_clock_when_fresh(paths, monkeypatch) -> None:
    """F-058 regression: a genuinely fresh IST capture must not be rejected just
    because the host system clock disagrees with IST (e.g. a UTC-defaulted
    sandbox/container). Host wall-clock is frozen to 14:05 -- 95 "minutes" past
    a 12:30 capture by naive host-clock arithmetic, which would look stale --
    but the true IST clock says only 2 minutes have passed. The gate must
    trust `trading.clock.now_ist()`, not the host clock, and accept it.
    """
    with freeze_time("2026-05-16T14:05:00"):  # host wall-clock (would-be datetime.now())
        _mock_now_ist(monkeypatch, "2026-05-16T12:32:00+05:30")  # true IST reading
        _seed_quotes(paths, date(2026, 5, 16), "1230", [_QUOTE_ROW])
        quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert "NTPC" in quotes
    assert capture_ts == datetime(2026, 5, 16, 12, 30, tzinfo=IST)


def test_read_latest_quotes_stale_gate_ignores_host_clock_when_stale(paths, monkeypatch) -> None:
    """F-058 regression: an hours-stale capture must not be waved through just
    because the host clock happens to read close to the capture time. Host
    wall-clock is frozen to 12:32 -- only 2 "minutes" past a 12:30 capture by
    naive host-clock arithmetic, which would look fresh -- but the true IST
    clock says 95 minutes have passed. The gate must trust
    `trading.clock.now_ist()`, not the host clock, and reject it.
    """
    with freeze_time("2026-05-16T12:32:00"):  # host wall-clock (would-be datetime.now())
        _mock_now_ist(monkeypatch, "2026-05-16T14:05:00+05:30")  # true IST reading
        _seed_quotes(paths, date(2026, 5, 16), "1230", [_QUOTE_ROW])
        with pytest.raises(QuoteSnapshotStaleError):
            read_latest_quotes(paths, date(2026, 5, 16))
