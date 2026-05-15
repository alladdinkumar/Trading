"""Per-symbol parquet storage for OHLCV history.

Files live at `data/parquet/nifty200/<SYMBOL>.parquet`. The `.NS` yfinance
suffix is stripped at the storage boundary so the on-disk filename is the
plain NSE ticker.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from trading.config import Paths
from trading.data.yfinance import NSE_SUFFIX, REQUIRED_COLUMNS

PARQUET_SUBDIR = "nifty200"


def _strip_suffix(symbol: str) -> str:
    return symbol[: -len(NSE_SUFFIX)] if symbol.endswith(NSE_SUFFIX) else symbol


def parquet_path(symbol: str, paths: Paths) -> Path:
    """Return the parquet file path for a symbol (suffix-stripped)."""
    return paths.parquet_dir / PARQUET_SUBDIR / f"{_strip_suffix(symbol)}.parquet"


def write_ohlcv(df: pd.DataFrame, symbol: str, paths: Paths) -> Path:
    """Write a normalised OHLCV DataFrame to parquet. Returns the path written."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing or list(df.columns) != list(REQUIRED_COLUMNS):
        raise ValueError(
            f"DataFrame columns must be exactly {REQUIRED_COLUMNS}; got {list(df.columns)}"
        )
    target = parquet_path(symbol, paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(target, compression="snappy")
    return target


def read_ohlcv(
    symbol: str,
    paths: Paths,
    *,
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame:
    """Read a symbol's parquet, optionally filter by inclusive [start, end].

    Trailing rows with NaN `close` (yfinance current-day stub) are dropped
    before slicing.
    """
    target = parquet_path(symbol, paths)
    if not target.is_file():
        raise FileNotFoundError(f"No parquet for {symbol} at {target}")
    df = pd.read_parquet(target)
    df = _drop_trailing_nan_close(df)
    if start is not None:
        df = df.loc[pd.Timestamp(start) :]
    if end is not None:
        df = df.loc[: pd.Timestamp(end)]
    return df


def _drop_trailing_nan_close(df: pd.DataFrame) -> pd.DataFrame:
    """Strip trailing rows where `close` is NaN.

    yfinance returns a row for the current trading day with NaN OHLC
    before the day has closed. That stub row breaks downstream indicator
    math (RSI/SMA propagate NaN), so we drop it here at the storage
    boundary. Interior NaN rows are preserved — only trailing NaN-close
    rows are stripped.
    """
    if df.empty:
        return df
    last_valid_idx = df["close"].last_valid_index()
    if last_valid_idx is None:
        return df.iloc[0:0]
    return df.loc[:last_valid_idx]


def list_symbols(paths: Paths) -> list[str]:
    """Sorted list of symbols (filename stems) with a parquet file on disk."""
    root = paths.parquet_dir / PARQUET_SUBDIR
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.parquet"))
