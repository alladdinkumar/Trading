"""Tests for trading.data.ohlcv_refresh — incremental refresh + close cross-check."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from trading.config import get_paths
from trading.data.kite import Holding
from trading.data.ohlcv_refresh import (
    DEFAULT_HISTORY_START,
    OVERLAP_CALENDAR_DAYS,
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


def _yf_like(full: pd.DataFrame, starts: list[object] | None = None):
    """Fetch stub that slices one canonical (re-adjusted) frame the way
    yfinance does: the whole series from DEFAULT_HISTORY_START, else the rows
    from `start` on. Optionally records each requested start."""

    def fake_fetch(symbol: str, start, end, **kw):
        if starts is not None:
            starts.append(start)
        if start == DEFAULT_HISTORY_START:
            return full
        return full[full.index >= pd.Timestamp(start)]

    return fake_fetch


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


def test_refresh_incremental_window_overlaps_last_bar(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    existing = _frame("2026-06-01", 5)  # last bar 2026-06-05 (Fri)
    write_ohlcv(existing, "FOO", paths)
    last_bar = existing.index[-1].date()

    starts: list[object] = []
    full = _frame("2026-06-01", 7)  # same scale, 2 genuinely new bars (Mon, Tue)

    def fake_fetch(symbol: str, start, end, **kw):
        starts.append(start)
        assert end == date(2026, 6, 10)
        return full[full.index >= pd.Timestamp(start)]

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=fake_fetch):
        result = refresh_ohlcv(paths, date(2026, 6, 10), symbols=["FOO"])

    # F-054: the window starts BEFORE the last stored bar so re-adjusted
    # history (split/bonus) is detectable, not blindly appended past.
    assert starts == [last_bar - timedelta(days=OVERLAP_CALENDAR_DAYS)]
    assert result.bars_added == 2
    assert result.symbols_refreshed == 1
    assert result.symbols_failed == 0
    # New bars are appended to the existing frame.
    merged = read_ohlcv("FOO", paths)
    assert len(merged) == 7


def test_refresh_no_split_appends_without_duplicates(tmp_path: Path) -> None:
    """Matching overlap → normal append; overlap rows dedupe cleanly."""
    paths = get_paths(root=tmp_path)
    existing = _frame("2026-05-04", 20, close=100.0)
    write_ohlcv(existing, "FOO", paths)

    starts: list[object] = []
    full = _frame("2026-05-04", 22, close=100.0)  # 2 new bars, same scale
    as_of = full.index[-1].date() + timedelta(days=1)

    with patch(
        "trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=_yf_like(full, starts)
    ):
        result = refresh_ohlcv(paths, as_of, symbols=["FOO"])

    assert DEFAULT_HISTORY_START not in starts  # no full re-fetch happened
    merged = read_ohlcv("FOO", paths)
    assert not merged.index.duplicated().any()
    assert len(merged) == len(existing) + 2
    assert result.bars_added == 2
    assert result.warnings == []


def test_refresh_split_mismatch_rebackfills_full_history(tmp_path: Path) -> None:
    """A 1:2 split re-scales yfinance history; the overlap mismatch must
    trigger a full re-backfill instead of leaving a phantom −50% seam."""
    paths = get_paths(root=tmp_path)
    # Stored history at the pre-split scale (₹2000).
    write_ohlcv(_frame("2026-05-04", 20, close=2000.0), "SPLIT", paths)

    starts: list[object] = []
    # yfinance now serves the whole series re-adjusted to the post-split
    # scale (₹1000), including 2 new post-split bars.
    full = _frame("2026-05-04", 22, close=1000.0)
    as_of = full.index[-1].date() + timedelta(days=1)

    with patch(
        "trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=_yf_like(full, starts)
    ):
        result = refresh_ohlcv(paths, as_of, symbols=["SPLIT"])

    assert DEFAULT_HISTORY_START in starts  # full re-fetch was triggered
    merged = read_ohlcv("SPLIT", paths)
    # The whole series is on one scale: no >20% single-bar seam anywhere.
    seams = merged["close"].pct_change().dropna().abs()
    assert (seams < 0.20).all()
    assert not merged.index.duplicated().any()
    assert float(merged["close"].iloc[0]) == 1000.0  # history was overwritten
    assert any("SPLIT" in w for w in result.warnings)  # note names the symbol


def test_refresh_overlap_without_shared_dates_rebackfills(tmp_path: Path) -> None:
    """Overlap fetch returning no stored dates → no anchor to compare, so the
    safest path is a full re-fetch."""
    paths = get_paths(root=tmp_path)
    write_ohlcv(_frame("2026-05-04", 5, close=100.0), "GAP", paths)  # ends 05-08

    starts: list[object] = []
    full = _frame("2026-06-01", 5, close=100.0)  # no overlap with stored dates

    with patch(
        "trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=_yf_like(full, starts)
    ):
        result = refresh_ohlcv(paths, date(2026, 6, 10), symbols=["GAP"])

    assert DEFAULT_HISTORY_START in starts
    assert read_ohlcv("GAP", paths).index.min() == full.index.min()
    assert any("GAP" in w for w in result.warnings)


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

    # yfinance hands back a forming as_of bar despite exclusive end:
    # 06-01..06-08 at the same scale; as_of = 06-08.
    full = _frame("2026-06-01", 6)

    with patch("trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=_yf_like(full)):
        refresh_ohlcv(paths, date(2026, 6, 8), symbols=["FOO"])

    merged = read_ohlcv("FOO", paths)
    assert merged.index.max().date() < date(2026, 6, 8)


def test_refresh_dedupe_prefers_new_bars_within_tolerance(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    write_ohlcv(_frame("2026-06-01", 3, close=100.0), "FOO", paths)

    starts: list[object] = []
    # Overlap closes drift 0.3% (< 0.5% tolerance) — not a split, so this
    # stays an append and the fresher bar wins the date collision.
    full = _frame("2026-06-01", 5, close=100.3)

    with patch(
        "trading.data.ohlcv_refresh.fetch_ohlcv", side_effect=_yf_like(full, starts)
    ):
        refresh_ohlcv(paths, date(2026, 6, 10), symbols=["FOO"])

    assert DEFAULT_HISTORY_START not in starts  # no re-backfill for tiny drift
    merged = read_ohlcv("FOO", paths)
    assert merged.loc[pd.Timestamp("2026-06-03"), "close"] == 100.3  # new wins


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
