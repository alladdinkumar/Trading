"""Tests for trading.store.ohlcv — parquet read/write/list."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.store.ohlcv import (
    list_symbols,
    parquet_path,
    read_ohlcv,
    write_ohlcv,
)


def _sample_df(rows: int = 10) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=rows, freq="B")
    idx.name = "date"
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(rows)],
            "high": [105.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "close": [104.0 + i for i in range(rows)],
            "volume": [1_000_000 + i for i in range(rows)],
        },
        index=idx,
    )


def test_parquet_path_strips_nse_suffix(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    assert parquet_path("RVNL.NS", paths) == paths.parquet_dir / "nifty200" / "RVNL.parquet"
    assert parquet_path("RVNL", paths) == paths.parquet_dir / "nifty200" / "RVNL.parquet"


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _sample_df()
    written = write_ohlcv(df, "RVNL", paths)
    assert written.is_file()
    back = read_ohlcv("RVNL", paths)
    # Parquet doesn't preserve pandas-specific index freq metadata, so normalise
    # both sides before comparison.
    expected = df.copy()
    expected.index = pd.DatetimeIndex(expected.index.values, name=expected.index.name)
    pd.testing.assert_frame_equal(expected, back)


def test_write_validates_schema(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    bad = pd.DataFrame({"open": [1, 2, 3], "wrong_col": [1, 2, 3]})
    with pytest.raises(ValueError, match="columns"):
        write_ohlcv(bad, "RVNL", paths)


def test_read_filters_by_date_range(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _sample_df(rows=20)
    write_ohlcv(df, "RVNL", paths)
    sliced = read_ohlcv("RVNL", paths, start="2025-01-06", end="2025-01-10")
    # B-frequency from 2025-01-01: Wed, Thu, Fri, Mon(6), Tue(7), Wed(8), Thu(9), Fri(10)
    # Filter 6..10 → 5 rows
    assert len(sliced) == 5
    assert sliced.index[0].strftime("%Y-%m-%d") == "2025-01-06"
    assert sliced.index[-1].strftime("%Y-%m-%d") == "2025-01-10"


def test_read_missing_raises(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    with pytest.raises(FileNotFoundError):
        read_ohlcv("NOPE", paths)


def test_list_symbols_returns_sorted(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _sample_df()
    write_ohlcv(df, "NTPC", paths)
    write_ohlcv(df, "RVNL", paths)
    write_ohlcv(df, "AAPL", paths)
    assert list_symbols(paths) == ["AAPL", "NTPC", "RVNL"]


def test_list_symbols_empty_when_dir_missing(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    assert list_symbols(paths) == []
