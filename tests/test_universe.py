"""Tests for trading.data.universe — load tickers from text file."""

from __future__ import annotations

from pathlib import Path

from trading.data.universe import load_universe


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
