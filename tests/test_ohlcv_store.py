"""Tests for trading.store.ohlcv — parquet read/write/list."""

from __future__ import annotations

import math
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


def _df_with_trailing_nan(valid_rows: int = 3, nan_rows: int = 1) -> pd.DataFrame:
    total = valid_rows + nan_rows
    idx = pd.date_range("2025-01-01", periods=total, freq="B")
    idx.name = "date"
    closes = [100.0 + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    opens = [99.0 + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    highs = [101.0 + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    lows = [98.0 + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    vols = [1_000_000 + i for i in range(valid_rows)] + [5_000_000] * nan_rows
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def test_read_drops_trailing_nan_close_rows(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=3, nan_rows=1)
    write_ohlcv(df, "RECLTD", paths)
    back = read_ohlcv("RECLTD", paths)
    assert len(back) == 3
    assert not math.isnan(back["close"].iloc[-1])


def test_read_drops_multiple_trailing_nan_rows(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=2, nan_rows=3)
    write_ohlcv(df, "X", paths)
    back = read_ohlcv("X", paths)
    assert len(back) == 2


def test_read_keeps_interior_nan_close(tmp_path: Path) -> None:
    """Only TRAILING NaN-close rows are stripped — a NaN in the middle stays."""
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=3, nan_rows=0)
    df.iloc[1, df.columns.get_loc("close")] = float("nan")  # interior NaN
    write_ohlcv(df, "Y", paths)
    back = read_ohlcv("Y", paths)
    assert len(back) == 3
    assert math.isnan(back["close"].iloc[1])


def test_read_all_nan_close_returns_empty(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=0, nan_rows=3)
    write_ohlcv(df, "Z", paths)
    back = read_ohlcv("Z", paths)
    assert len(back) == 0
