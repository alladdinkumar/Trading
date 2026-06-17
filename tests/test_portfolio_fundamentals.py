"""Tests for trading.portfolio.fundamentals — static CSV → FundamentalsSnapshot (F-022)."""

from __future__ import annotations

import pytest

from trading.config import get_paths
from trading.portfolio.fundamentals import load_fundamentals_map


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def _write_csv(paths, body: str) -> None:
    static = paths.project_root / "data" / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "fundamentals.csv").write_text(body, encoding="utf-8")


def test_missing_file_returns_empty_map(paths) -> None:
    assert load_fundamentals_map(paths) == {}


def test_parses_rows_into_snapshots(paths) -> None:
    _write_csv(
        paths,
        "symbol,profit_growth_yoy,profit_cagr_3y,debt_to_equity,roe,pe_percentile_5y\n"
        "RELIANCE,0.11,0.09,0.42,0.18,0.55\n",
    )
    m = load_fundamentals_map(paths)
    snap = m["RELIANCE"]
    assert snap.profit_growth_yoy == pytest.approx(0.11)
    assert snap.profit_cagr_3y == pytest.approx(0.09)
    assert snap.debt_to_equity == pytest.approx(0.42)
    assert snap.roe == pytest.approx(0.18)
    assert snap.pe_percentile_5y == pytest.approx(0.55)


def test_blank_cells_become_none(paths) -> None:
    _write_csv(
        paths,
        "symbol,profit_growth_yoy,profit_cagr_3y,debt_to_equity,roe,pe_percentile_5y\n"
        "TCS,0.08,,,0.40,\n",
    )
    snap = load_fundamentals_map(paths)["TCS"]
    assert snap.profit_growth_yoy == pytest.approx(0.08)
    assert snap.profit_cagr_3y is None
    assert snap.debt_to_equity is None
    assert snap.roe == pytest.approx(0.40)
    assert snap.pe_percentile_5y is None


def test_comments_and_blank_lines_skipped(paths) -> None:
    _write_csv(
        paths,
        "# header comment\n"
        "\n"
        "symbol,profit_growth_yoy,profit_cagr_3y,debt_to_equity,roe,pe_percentile_5y\n"
        "# inline comment row\n"
        "INFY,0.05,0.06,0.10,0.25,0.30\n",
    )
    m = load_fundamentals_map(paths)
    assert set(m) == {"INFY"}


def test_unparseable_value_becomes_none(paths) -> None:
    _write_csv(
        paths,
        "symbol,profit_growth_yoy,profit_cagr_3y,debt_to_equity,roe,pe_percentile_5y\n"
        "WONK,n/a,0.06,0.10,0.25,0.30\n",
    )
    assert load_fundamentals_map(paths)["WONK"].profit_growth_yoy is None


def test_shipped_csv_is_loadable() -> None:
    """The repo's data/static/fundamentals.csv parses without error (may be empty)."""
    m = load_fundamentals_map()  # real project paths
    assert isinstance(m, dict)
