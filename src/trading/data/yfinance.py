"""yfinance wrapper that returns a normalised OHLCV DataFrame.

yfinance is convenient but irregular: column names vary by version, multi-ticker
calls produce MultiIndex columns, the DatetimeIndex is tz-aware on some symbols
and tz-naive on others. This module hides all of that.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import yfinance as yf

NSE_SUFFIX = ".NS"
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


class OhlcvFetchError(Exception):
    """yfinance returned empty/missing data for the requested symbol."""


def to_yf_symbol(symbol: str) -> str:
    """Append the NSE suffix `.NS` if not already present. Idempotent."""
    if symbol.endswith(NSE_SUFFIX):
        return symbol
    return f"{symbol}{NSE_SUFFIX}"


def fetch_ohlcv(
    symbol: str,
    start: date | str,
    end: date | str,
    *,
    auto_adjust: bool = True,
) -> pd.DataFrame:
    """Fetch daily OHLCV bars for an NSE symbol.

    Returns a DataFrame indexed by tz-naive Timestamp (`name='date'`) with
    columns `open, high, low, close, volume` in that order. Raises
    `OhlcvFetchError` when yfinance returns no rows.
    """
    yf_symbol = to_yf_symbol(symbol)
    raw: Any = yf.download(
        yf_symbol,
        start=start,
        end=end,
        auto_adjust=auto_adjust,
        progress=False,
        actions=False,
    )
    if raw is None or (hasattr(raw, "empty") and raw.empty):
        raise OhlcvFetchError(f"No data for {symbol} ({yf_symbol}) {start}..{end}")
    df: pd.DataFrame = raw
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise OhlcvFetchError(f"yfinance response missing columns {missing} for {symbol}")
    df = df.loc[:, list(REQUIRED_COLUMNS)]
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    idx.name = "date"
    df.index = idx
    # Drop partial-bar artifacts: yfinance returns a row for the still-forming
    # current day with NaN OHLC (sometimes a real volume). Those rows poison
    # downstream indicator math, so strip them at the fetch boundary — both the
    # ingest_history backfill and the daily refresh benefit.
    df = df[df["close"].notna()]
    return df
