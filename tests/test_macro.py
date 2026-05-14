"""Tests for trading.data.macro — fetcher shape, FII/DII parsing, orchestrator."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date
from typing import Any

import pandas as pd
import pytest

from trading.data.macro import (
    YF_TICKERS,
    MacroSnapshot,
    YfQuote,
    build_snapshot,
    fetch_fii_dii,
    fetch_yf_quote,
)
from trading.store.macro_store import get_macro_snapshot, upsert_macro_snapshot
from trading.store.migrations import run_migrations

# ---------------------------------------------------------------------------
# fetch_yf_quote — mocked yfinance.download
# ---------------------------------------------------------------------------


def _yf_frame(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes}, index=idx)


def test_fetch_yf_quote_returns_latest_and_pct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trading.data.macro.yf.download",
        lambda *_a, **_kw: _yf_frame([100.0, 105.0]),
    )
    q = fetch_yf_quote("^GSPC")
    assert q.ticker == "^GSPC"
    assert q.close == pytest.approx(105.0)
    assert q.pct_change_1d == pytest.approx(5.0)


def test_fetch_yf_quote_one_bar_returns_close_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trading.data.macro.yf.download",
        lambda *_a, **_kw: _yf_frame([100.0]),
    )
    q = fetch_yf_quote("X")
    assert q.close == pytest.approx(100.0)
    assert q.pct_change_1d is None


def test_fetch_yf_quote_empty_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trading.data.macro.yf.download",
        lambda *_a, **_kw: pd.DataFrame(),
    )
    q = fetch_yf_quote("X")
    assert q.close is None
    assert q.pct_change_1d is None


def test_fetch_yf_quote_swallows_yfinance_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("yfinance rate-limited")

    monkeypatch.setattr("trading.data.macro.yf.download", boom)
    q = fetch_yf_quote("X")
    assert q.close is None
    assert q.pct_change_1d is None


def test_fetch_yf_quote_flattens_multiindex_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """yfinance sometimes returns MultiIndex columns even for single-ticker calls."""
    idx = pd.date_range("2026-05-01", periods=2, freq="B")
    df = pd.DataFrame({("Close", "X"): [10.0, 11.0]}, index=idx)
    monkeypatch.setattr("trading.data.macro.yf.download", lambda *_a, **_kw: df)
    q = fetch_yf_quote("X")
    assert q.close == pytest.approx(11.0)
    assert q.pct_change_1d == pytest.approx(10.0)


def test_yf_tickers_constants_exist() -> None:
    """Schema columns we promise to populate must each have a yf ticker mapping."""
    expected = {"sp500", "nasdaq", "dow", "usdinr", "crude", "vix", "us_10y"}
    assert expected.issubset(YF_TICKERS.keys())


# ---------------------------------------------------------------------------
# fetch_fii_dii — modern + legacy + failure paths
# ---------------------------------------------------------------------------


def _install_fake_nsepython(monkeypatch: pytest.MonkeyPatch, df: pd.DataFrame | None) -> None:
    import sys
    import types

    fake = types.ModuleType("nsepython")

    def nse_fiidii(mode: str = "pandas") -> pd.DataFrame | None:
        return df

    fake.nse_fiidii = nse_fiidii  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nsepython", fake)


def test_fetch_fii_dii_parses_modern_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    df = pd.DataFrame(
        {
            "category": ["FII/FPI", "DII"],
            "netValue": [1234.56, -789.0],
        }
    )
    _install_fake_nsepython(monkeypatch, df)
    fii, dii = fetch_fii_dii()
    assert fii == pytest.approx(1234.56)
    assert dii == pytest.approx(-789.0)


def test_fetch_fii_dii_parses_legacy_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    df = pd.DataFrame(
        {
            "type": ["FII", "DII"],
            "netVal": [500.0, 200.0],
        }
    )
    _install_fake_nsepython(monkeypatch, df)
    fii, dii = fetch_fii_dii()
    assert fii == pytest.approx(500.0)
    assert dii == pytest.approx(200.0)


def test_fetch_fii_dii_returns_none_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_nsepython(monkeypatch, pd.DataFrame())
    assert fetch_fii_dii() == (None, None)


def test_fetch_fii_dii_returns_none_on_unknown_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_nsepython(monkeypatch, pd.DataFrame({"foo": [1], "bar": [2]}))
    assert fetch_fii_dii() == (None, None)


def test_fetch_fii_dii_returns_none_on_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "nsepython", None)
    assert fetch_fii_dii() == (None, None)


# ---------------------------------------------------------------------------
# build_snapshot — pulls together quotes + FII into a row
# ---------------------------------------------------------------------------


def _fake_quotes() -> dict[str, YfQuote]:
    return {
        "sp500": YfQuote(ticker="^GSPC", close=5000.0, pct_change_1d=0.5),
        "nasdaq": YfQuote(ticker="^IXIC", close=18000.0, pct_change_1d=0.8),
        "dow": YfQuote(ticker="^DJI", close=40000.0, pct_change_1d=0.2),
        "usdinr": YfQuote(ticker="INR=X", close=83.5, pct_change_1d=-0.1),
        "crude": YfQuote(ticker="BZ=F", close=78.5, pct_change_1d=1.0),
        "vix": YfQuote(ticker="^INDIAVIX", close=13.0, pct_change_1d=-2.0),
        "us_10y": YfQuote(ticker="^TNX", close=4.2, pct_change_1d=0.1),
    }


def test_build_snapshot_maps_quotes_to_columns() -> None:
    snap = build_snapshot(
        date(2026, 5, 15),
        quotes=_fake_quotes(),
        fii_dii=(1500.0, -800.0),
    )
    assert snap.date == "2026-05-15"
    assert snap.sp500 == pytest.approx(5000.0)
    assert snap.nasdaq_fut == pytest.approx(18000.0)
    assert snap.dow_fut == pytest.approx(40000.0)
    assert snap.usdinr == pytest.approx(83.5)
    assert snap.crude == pytest.approx(78.5)
    assert snap.vix == pytest.approx(13.0)
    assert snap.us_10y == pytest.approx(4.2)
    assert snap.fii_flow_cr == pytest.approx(1500.0)
    assert snap.dii_flow_cr == pytest.approx(-800.0)
    assert snap.sgx_nifty is None  # no yf ticker
    assert snap.regime is None  # filled by orchestrator, not here


def test_build_snapshot_with_missing_data_keeps_nulls() -> None:
    empty = {k: YfQuote(ticker=k, close=None, pct_change_1d=None) for k in
             ("sp500", "nasdaq", "dow", "usdinr", "crude", "vix", "us_10y")}
    snap = build_snapshot(date(2026, 5, 15), quotes=empty, fii_dii=(None, None))
    for k, v in asdict(snap).items():
        if k in ("date",):
            continue
        assert v is None, f"{k} should be None"


# ---------------------------------------------------------------------------
# macro_store round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def test_macro_snapshot_roundtrip(conn: sqlite3.Connection) -> None:
    snap = build_snapshot(
        date(2026, 5, 15),
        quotes=_fake_quotes(),
        fii_dii=(1500.0, -800.0),
    )
    classified = MacroSnapshot(**{**snap.__dict__, "regime": "RISK_ON"})
    upsert_macro_snapshot(conn, classified)

    got = get_macro_snapshot(conn, "2026-05-15")
    assert got is not None
    assert got.regime == "RISK_ON"
    assert got.sp500 == pytest.approx(5000.0)
    assert got.fii_flow_cr == pytest.approx(1500.0)


def test_macro_snapshot_upsert_replaces_existing(conn: sqlite3.Connection) -> None:
    snap1 = build_snapshot(
        date(2026, 5, 15),
        quotes=_fake_quotes(),
        fii_dii=(1500.0, -800.0),
    )
    upsert_macro_snapshot(conn, snap1)

    snap2 = MacroSnapshot(**{**snap1.__dict__, "vix": 30.0, "regime": "RISK_OFF"})
    upsert_macro_snapshot(conn, snap2)

    rows = conn.execute(
        "SELECT * FROM macro_snapshot WHERE date = ?", ("2026-05-15",)
    ).fetchall()
    assert len(rows) == 1
    got = get_macro_snapshot(conn, "2026-05-15")
    assert got is not None
    assert got.vix == pytest.approx(30.0)
    assert got.regime == "RISK_OFF"


def test_get_macro_snapshot_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert get_macro_snapshot(conn, "1999-01-01") is None


def test_regime_check_constraint(conn: sqlite3.Connection) -> None:
    """The schema's CHECK clause must reject unknown regime strings."""
    snap = build_snapshot(date(2026, 5, 15), quotes=_fake_quotes(), fii_dii=(0.0, 0.0))
    bad = MacroSnapshot(**{**snap.__dict__, "regime": "GREEDY"})
    with pytest.raises(sqlite3.IntegrityError):
        upsert_macro_snapshot(conn, bad)
