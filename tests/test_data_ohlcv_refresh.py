"""Tests for trading.data.ohlcv_refresh — incremental refresh + close cross-check."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from trading.config import get_paths
from trading.data.kite import Holding
from trading.data.ohlcv_refresh import (
    DEFAULT_HISTORY_START,
    RefreshResult,
    cross_check_closes,
    refresh_ohlcv,
)
from trading.store.ohlcv import read_ohlcv, write_ohlcv


def _frame(start: str, periods: int, *, close: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq="B", name="date")
    return pd.DataFrame(
        {
            "open": [close] * periods,
            "high": [close + 1] * periods,
            "low": [close - 1] * periods,
            "close": [close] * periods,
            "volume": [1_000_000] * periods,
        },
        index=idx,
    )


def _holding(symbol: str, close_price: float) -> Holding:
    return Holding(
        tradingsymbol=symbol,
        exchange="NSE",
        isin="INE000000000",
        quantity=1,
        average_price=close_price,
        last_price=close_price,
        close_price=close_price,
        pnl=0.0,
        day_change=0.0,
        day_change_percentage=0.0,
    )


# ---------------------------------------------------------------------------
# refresh_ohlcv
# ---------------------------------------------------------------------------


def test_refresh_incremental_window_starts_after_last_bar(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    existing = _frame("2026-06-01", 5)  # last bar 2026-06-05 (Fri)
    write_ohlcv(existing, "FOO", paths)
    last_bar = existing.index[-1].date()

    captured: dict[str, object] = {}

    def fake_fetch(symbol: str, start, end, **kw):
        captured["start"] = start
        captured["end"] = end
        return _frame("2026-06-08", 2, close=110.0)  # Mon, Tue

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=fake_fetch):
        result = refresh_ohlcv(paths, date(2026, 6, 10), symbols=["FOO"])

    assert captured["start"] == last_bar + pd.Timedelta(days=1).to_pytimedelta()
    assert captured["end"] == date(2026, 6, 10)
    assert result.bars_added == 2
    assert result.symbols_refreshed == 1
    assert result.symbols_failed == 0
    # New bars are appended to the existing frame.
    merged = read_ohlcv("FOO", paths)
    assert len(merged) == 7


def test_refresh_full_fetch_when_no_parquet(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    captured: dict[str, object] = {}

    def fake_fetch(symbol: str, start, end, **kw):
        captured["start"] = start
        return _frame("2023-01-02", 10)

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=fake_fetch):
        result = refresh_ohlcv(paths, date(2026, 6, 10), symbols=["NEW"])

    assert captured["start"] == DEFAULT_HISTORY_START
    assert result.bars_added == 10
    assert read_ohlcv("NEW", paths).shape[0] == 10


def test_refresh_drops_rows_on_or_after_as_of(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    write_ohlcv(_frame("2026-06-01", 3), "FOO", paths)  # last 2026-06-03

    def fake_fetch(symbol: str, start, end, **kw):
        # yfinance hands back a forming as_of bar despite exclusive end.
        return _frame("2026-06-04", 4)  # 06-04..06-09; as_of = 06-08

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=fake_fetch):
        refresh_ohlcv(paths, date(2026, 6, 8), symbols=["FOO"])

    merged = read_ohlcv("FOO", paths)
    assert merged.index.max().date() < date(2026, 6, 8)


def test_refresh_dedupe_prefers_new_bars(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    write_ohlcv(_frame("2026-06-01", 3, close=100.0), "FOO", paths)

    def fake_fetch(symbol: str, start, end, **kw):
        # Overlaps the last existing bar (06-03) with a different close.
        return _frame("2026-06-03", 2, close=200.0)

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=fake_fetch):
        refresh_ohlcv(paths, date(2026, 6, 10), symbols=["FOO"])

    merged = read_ohlcv("FOO", paths)
    assert merged.loc[pd.Timestamp("2026-06-03"), "close"] == 200.0  # new wins


def test_refresh_noop_when_current(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    existing = _frame("2026-06-01", 5)  # last bar 2026-06-05
    write_ohlcv(existing, "FOO", paths)

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv") as mock_fetch:
        # as_of is the day after the last bar → no window to fetch.
        result = refresh_ohlcv(paths, date(2026, 6, 6), symbols=["FOO"])

    mock_fetch.assert_not_called()
    assert result.bars_added == 0
    assert result.symbols_refreshed == 0


def test_refresh_isolates_per_symbol_errors(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)

    def fake_fetch(symbol: str, start, end, **kw):
        if symbol == "BAD":
            raise RuntimeError("yfinance 404")
        return _frame("2023-01-02", 4)

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=fake_fetch):
        result = refresh_ohlcv(paths, date(2026, 6, 10), symbols=["BAD", "GOOD"])

    assert result.symbols_failed == 1
    assert result.symbols_refreshed == 1
    assert any("BAD" in w for w in result.warnings)
    assert read_ohlcv("GOOD", paths).shape[0] == 4


def test_refresh_result_type(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    with patch(
        "trading.data.ohlcv_refresh.fetch_ohlcv",
        side_effect=lambda *a, **k: _frame("2023-01-02", 2),
    ):
        result = refresh_ohlcv(paths, date(2026, 6, 10), symbols=["FOO"])
    assert isinstance(result, RefreshResult)


# ---------------------------------------------------------------------------
# cross_check_closes
# ---------------------------------------------------------------------------


def test_cross_check_silent_within_tolerance(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    write_ohlcv(_frame("2026-06-01", 3, close=100.0), "FOO", paths)
    warnings = cross_check_closes(
        paths,
        date(2026, 6, 10),
        [_holding("FOO", 100.3)],  # 0.3% < 0.5%
    )
    assert warnings == []


def test_cross_check_warns_above_tolerance(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    write_ohlcv(_frame("2026-06-01", 3, close=100.0), "FOO", paths)
    warnings = cross_check_closes(
        paths,
        date(2026, 6, 10),
        [_holding("FOO", 90.0)],  # ~+11%
    )
    assert len(warnings) == 1
    assert "FOO" in warnings[0]


def test_cross_check_skips_missing_parquet(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    warnings = cross_check_closes(paths, date(2026, 6, 10), [_holding("NOPE", 100.0)])
    assert warnings == []
