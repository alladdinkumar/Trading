"""Tests for trading.data.universe — load tickers from text file."""

from __future__ import annotations

from pathlib import Path

from trading.config import get_paths
from trading.data.universe import load_candidate_universe, load_universe


def test_load_universe_default_file_has_symbols() -> None:
    """The bootstrap universe.txt should load and have >=50 symbols."""
    symbols = load_universe()
    assert len(symbols) >= 50
    assert "RELIANCE" in symbols
    assert "RVNL" in symbols  # user holding
    assert "NTPC" in symbols


def test_load_universe_skips_comments_and_blanks(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_text(
        "# header comment\n"
        "\n"
        "RVNL\n"
        "  NTPC  \n"  # whitespace to strip
        "# inline comment\n"
        "RELIANCE\n"
        "\n",
        encoding="utf-8",
    )
    assert load_universe(f) == ["RVNL", "NTPC", "RELIANCE"]


def test_load_universe_dedupes(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_text("RVNL\nNTPC\nRVNL\nNTPC\nRVNL\n", encoding="utf-8")
    assert load_universe(f) == ["RVNL", "NTPC"]


def test_load_universe_preserves_order(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_text("ZULU\nALPHA\nMIKE\n", encoding="utf-8")
    assert load_universe(f) == ["ZULU", "ALPHA", "MIKE"]


def _seed_static(root: Path, *, nifty50: str | None, universe: str) -> None:
    static = root / "data" / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "universe.txt").write_text(universe, encoding="utf-8")
    if nifty50 is not None:
        (static / "nifty50.txt").write_text(nifty50, encoding="utf-8")


def test_load_candidate_universe_reads_nifty50_when_present(tmp_path: Path) -> None:
    _seed_static(
        tmp_path,
        nifty50="RELIANCE\nINFY\nTCS\n",
        universe="RELIANCE\nINFY\nTCS\nRVNL\nIREDA\n",
    )
    assert load_candidate_universe(get_paths(root=tmp_path)) == [
        "RELIANCE",
        "INFY",
        "TCS",
    ]


def test_load_candidate_universe_falls_back_to_universe(tmp_path: Path) -> None:
    _seed_static(
        tmp_path,
        nifty50=None,
        universe="RELIANCE\nINFY\nTCS\nRVNL\n",
    )
    assert load_candidate_universe(get_paths(root=tmp_path)) == [
        "RELIANCE",
        "INFY",
        "TCS",
        "RVNL",
    ]


def test_default_candidate_file_has_exactly_50_symbols() -> None:
    """The pinned nifty50.txt should hold exactly 50 candidate symbols."""
    symbols = load_candidate_universe()
    assert len(symbols) == 50
    assert "RELIANCE" in symbols
    assert "RVNL" not in symbols  # holding, not a candidate
