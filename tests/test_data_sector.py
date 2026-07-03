"""Tests for trading.data.sector — RS computation + regime labels + map loader."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

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
from trading.data.universe import load_candidate_universe


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


def test_every_candidate_universe_symbol_resolves_to_a_sector() -> None:
    """F-057 regression: every data/static/nifty50.txt symbol must have a row in
    sector_map.csv, else daily_budget's max-2-lots-per-sector cap silently skips
    it (a `sector=None` candidate is never gated — see daily_budget.py:130)."""
    universe = set(load_candidate_universe())
    mapped = set(load_sector_map())
    missing = universe - mapped
    assert not missing, f"nifty50.txt symbols missing from sector_map.csv: {sorted(missing)}"


def test_load_sector_map_returns_empty_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    assert load_sector_map(paths) == {}


def test_fetch_sector_history_returns_none_on_yfinance_error() -> None:
    from trading.data.sector import fetch_sector_history

    with patch("trading.data.sector.yf.download", side_effect=RuntimeError("boom")):
        result = fetch_sector_history("^NSEBANK", lookback_days=90)
    assert result is None


def test_fetch_sector_history_returns_none_on_empty_frame() -> None:
    from trading.data.sector import fetch_sector_history

    with patch("trading.data.sector.yf.download", return_value=pd.DataFrame()):
        result = fetch_sector_history("^NSEBANK", lookback_days=90)
    assert result is None


def test_fetch_all_sectors_skips_failed_tickers_and_returns_rows() -> None:
    """One sector + benchmark succeed; another sector fails. Result has 1 row."""
    from trading.data.sector import fetch_all_sectors

    # 22 bars total: indexes 0..21. Sector and benchmark are flat at 100
    # for the first 21 bars; final bar jumps. Window math:
    #   rs_5d: last bar vs bar 16 (5 back) — flat-100→105 sector vs flat-100→102 bench → +0.03
    #   rs_20d: last bar vs bar 1 — same → +0.03
    #   rs_60d: not enough history (22 bars < 61) → None
    bench_df = pd.DataFrame(
        {"Close": [100.0] * 21 + [102.0]},
        index=pd.date_range("2026-01-01", periods=22, freq="B"),
    )
    sector_df = pd.DataFrame(
        {"Close": [100.0] * 21 + [105.0]},
        index=pd.date_range("2026-01-01", periods=22, freq="B"),
    )

    def fake_download(ticker: str, **kwargs: object) -> pd.DataFrame:
        if ticker == "^NSEI":
            return bench_df.copy()
        if ticker == "^NSEBANK":
            return sector_df.copy()
        raise RuntimeError("ticker failed")

    with patch("trading.data.sector.yf.download", side_effect=fake_download):
        rows = fetch_all_sectors(date(2026, 2, 1))

    # Only NIFTYBANK succeeded among the 11 sectors.
    assert len(rows) == 1
    r = rows[0]
    assert r.sector == "NIFTYBANK"
    assert r.date == "2026-02-01"
    assert r.close == 105.0
    assert r.rs_5d is not None and abs(r.rs_5d - 0.03) < 1e-9
    assert r.rs_20d is not None and r.rs_20d > 0.02
    assert r.regime == "LEADING"
    assert r.rs_60d is None
