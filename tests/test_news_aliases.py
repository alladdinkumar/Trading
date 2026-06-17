"""Tests for the CSV-backed news alias map (F-015).

`data/static/aliases.csv` drives symbol attribution + the sentiment-rollup
watch-list. These cover the loader, the built-in fallback, and the shipped
file's coverage of the full ingest universe.
"""

from __future__ import annotations

import pytest

from trading.config import get_paths
from trading.data.news import (
    DEFAULT_ALIASES,
    attribute_symbol,
    default_aliases,
    load_aliases_map,
)
from trading.data.universe import load_universe


@pytest.fixture
def paths(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def _write_csv(paths, body: str) -> None:  # type: ignore[no-untyped-def]
    static = paths.project_root / "data" / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "aliases.csv").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty_map(paths) -> None:  # type: ignore[no-untyped-def]
    assert load_aliases_map(paths) == {}


def test_parses_pipe_separated_aliases(paths) -> None:  # type: ignore[no-untyped-def]
    _write_csv(paths, "symbol,aliases\nRELIANCE,Reliance|RIL|Reliance Industries\n")
    m = load_aliases_map(paths)
    assert m["RELIANCE"] == ["Reliance", "RIL", "Reliance Industries"]


def test_blank_aliases_cell_yields_empty_list(paths) -> None:  # type: ignore[no-untyped-def]
    """A symbol with no extra names still attributes off its bare ticker."""
    _write_csv(paths, "symbol,aliases\nITC,\n")
    assert load_aliases_map(paths)["ITC"] == []
    assert attribute_symbol("ITC hikes cigarette prices", load_aliases_map(paths)) == "ITC"


def test_comments_and_blank_lines_skipped(paths) -> None:  # type: ignore[no-untyped-def]
    _write_csv(
        paths,
        "# a comment\n\nsymbol,aliases\n# inline\nINFY,Infosys\n",
    )
    assert set(load_aliases_map(paths)) == {"INFY"}


# ---------------------------------------------------------------------------
# default_aliases fallback
# ---------------------------------------------------------------------------


def test_default_aliases_falls_back_to_builtin_when_csv_missing(paths) -> None:  # type: ignore[no-untyped-def]
    assert default_aliases(paths) == DEFAULT_ALIASES


def test_default_aliases_prefers_csv_when_present(paths) -> None:  # type: ignore[no-untyped-def]
    _write_csv(paths, "symbol,aliases\nWONK,Wonky Corp\n")
    assert default_aliases(paths) == {"WONK": ["Wonky Corp"]}


# ---------------------------------------------------------------------------
# shipped CSV — the F-015 fix proper
# ---------------------------------------------------------------------------


def test_shipped_csv_covers_full_ingest_universe() -> None:
    """Every symbol in universe.txt (Nifty 50 + holdings) has an alias entry."""
    shipped = load_aliases_map()  # real project paths
    universe = set(load_universe())
    missing = universe - set(shipped)
    assert not missing, f"alias map missing {sorted(missing)}"


def test_shipped_csv_attributes_hyphen_and_ampersand_tickers() -> None:
    aliases = default_aliases()
    assert attribute_symbol("Bajaj Auto Q1 sales jump 12%", aliases) == "BAJAJ-AUTO"
    assert attribute_symbol("Mahindra & Mahindra unveils new SUV", aliases) == "M&M"


def test_shipped_csv_disambiguates_sbi_life_from_sbin() -> None:
    aliases = default_aliases()
    assert attribute_symbol("SBI Life Insurance posts higher APE", aliases) == "SBILIFE"
    assert attribute_symbol("SBI cuts home loan rates", aliases) == "SBIN"
