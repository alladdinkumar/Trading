"""Tests for trading.data.yfinance — fetch & shape normalisation."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from trading.data.yfinance import (
    NSE_SUFFIX,
    REQUIRED_COLUMNS,
    OhlcvFetchError,
    fetch_ohlcv,
    to_yf_symbol,
)

# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------


def test_to_yf_symbol_appends_suffix() -> None:
    assert to_yf_symbol("RVNL") == "RVNL.NS"


def test_to_yf_symbol_is_idempotent() -> None:
    assert to_yf_symbol("RVNL.NS") == "RVNL.NS"


def test_to_yf_symbol_preserves_dashes() -> None:
    # BAJAJ-AUTO is a real NSE ticker
    assert to_yf_symbol("BAJAJ-AUTO") == f"BAJAJ-AUTO{NSE_SUFFIX}"


# ---------------------------------------------------------------------------
# fetch_ohlcv normalisation
# ---------------------------------------------------------------------------


def _fake_yf_response(rows: int = 5) -> pd.DataFrame:
    """Build a yfinance-shaped DataFrame: title-case cols, tz-aware DatetimeIndex."""
    idx = pd.date_range("2025-01-01", periods=rows, freq="B", tz="Asia/Kolkata")
    df = pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(rows)],
            "High": [105.0 + i for i in range(rows)],
            "Low": [99.0 + i for i in range(rows)],
            "Close": [104.0 + i for i in range(rows)],
            "Volume": [1_000_000 + i for i in range(rows)],
        },
        index=idx,
    )
    df.index.name = "Date"
    return df


def test_fetch_ohlcv_lowercases_and_orders_columns() -> None:
    with patch("trading.data.yfinance.yf.download", return_value=_fake_yf_response()):
        df = fetch_ohlcv("RVNL", date(2025, 1, 1), date(2025, 1, 10))
    assert tuple(df.columns) == REQUIRED_COLUMNS


def test_fetch_ohlcv_strips_timezone() -> None:
    with patch("trading.data.yfinance.yf.download", return_value=_fake_yf_response()):
        df = fetch_ohlcv("RVNL", "2025-01-01", "2025-01-10")
    assert df.index.tz is None
    assert df.index.name == "date"


def test_fetch_ohlcv_flattens_multiindex_columns() -> None:
    base = _fake_yf_response()
    base.columns = pd.MultiIndex.from_product([base.columns, ["RVNL.NS"]])
    with patch("trading.data.yfinance.yf.download", return_value=base):
        df = fetch_ohlcv("RVNL", "2025-01-01", "2025-01-10")
    assert tuple(df.columns) == REQUIRED_COLUMNS


def test_fetch_ohlcv_raises_on_empty() -> None:
    with (
        patch("trading.data.yfinance.yf.download", return_value=pd.DataFrame()),
        pytest.raises(OhlcvFetchError, match="No data"),
    ):
        fetch_ohlcv("BOGUS", "2025-01-01", "2025-01-10")


def test_fetch_ohlcv_raises_on_none() -> None:
    with (
        patch("trading.data.yfinance.yf.download", return_value=None),
        pytest.raises(OhlcvFetchError),
    ):
        fetch_ohlcv("BOGUS", "2025-01-01", "2025-01-10")


def test_fetch_ohlcv_drops_partial_nan_close_row() -> None:
    """A forming-day row with NaN OHLC (real volume) is stripped at fetch."""
    base = _fake_yf_response(rows=3)
    partial_idx = base.index[-1] + pd.Timedelta(days=1)
    base.loc[partial_idx] = [float("nan")] * 4 + [1_234_567]
    with patch("trading.data.yfinance.yf.download", return_value=base):
        df = fetch_ohlcv("RVNL", "2025-01-01", "2025-01-10")
    assert df["close"].notna().all()
    assert len(df) == 3  # the partial row is gone


def test_fetch_ohlcv_passes_yfinance_args_correctly() -> None:
    fake = _fake_yf_response()
    with patch("trading.data.yfinance.yf.download", return_value=fake) as mock:
        fetch_ohlcv("RVNL", "2025-01-01", "2025-01-10")
    mock.assert_called_once()
    kwargs = mock.call_args.kwargs
    assert mock.call_args.args[0] == "RVNL.NS"
    assert kwargs["auto_adjust"] is True
    assert kwargs["progress"] is False
