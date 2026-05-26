"""Tests for trading.data.sector — RS computation + regime labels + map loader."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.data.sector import (
    LAGGING_THRESHOLD,
    LEADING_THRESHOLD,
    _regime_for,
    compute_rs,
    load_sector_map,
)


def _series(values: list[float]) -> pd.Series:
    """Build a date-indexed close series, oldest first."""
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, name="close")


def test_compute_rs_simple_difference() -> None:
    # Sector +5% over the window, benchmark +2% → RS = +0.03
    sector = _series([100.0] * 5 + [105.0])
    bench = _series([100.0] * 5 + [102.0])
    rs = compute_rs(sector, bench, window=5)
    assert rs is not None
    assert abs(rs - 0.03) < 1e-9


def test_compute_rs_returns_none_when_history_too_short() -> None:
    sector = _series([100.0, 101.0, 102.0])  # 3 bars, asking for window=5
    bench = _series([100.0, 100.0, 100.0])
    assert compute_rs(sector, bench, window=5) is None


def test_compute_rs_returns_none_when_lookback_close_is_zero() -> None:
    sector = _series([0.0, 100.0, 100.0, 100.0, 100.0, 105.0])
    bench = _series([100.0, 100.0, 100.0, 100.0, 100.0, 102.0])
    assert compute_rs(sector, bench, window=5) is None


def test_regime_for_leading_when_above_threshold() -> None:
    assert _regime_for(LEADING_THRESHOLD + 0.001) == "LEADING"


def test_regime_for_lagging_when_below_threshold() -> None:
    assert _regime_for(LAGGING_THRESHOLD - 0.001) == "LAGGING"


def test_regime_for_neutral_on_boundary() -> None:
    # Strictly inside the (lagging, leading) band → NEUTRAL.
    assert _regime_for(LEADING_THRESHOLD) == "NEUTRAL"
    assert _regime_for(LAGGING_THRESHOLD) == "NEUTRAL"
    assert _regime_for(0.0) == "NEUTRAL"


def test_regime_for_none_when_rs_none() -> None:
    assert _regime_for(None) is None


def test_load_sector_map_reads_csv_with_comments_and_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    static_dir = tmp_path / "data" / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "sector_map.csv").write_text(
        "# Comment\n"
        "\n"
        "symbol,sector\n"
        "INFY,IT\n"
        "HDFCBANK,NIFTYBANK\n"
        "# another comment\n",
        encoding="utf-8",
    )
    paths = get_paths()
    assert load_sector_map(paths) == {"INFY": "IT", "HDFCBANK": "NIFTYBANK"}


def test_load_sector_map_returns_empty_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    assert load_sector_map(paths) == {}
