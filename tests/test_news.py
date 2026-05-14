"""Tests for trading.data.news — RSS parsing, attribution, orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading.data.news import (
    DEFAULT_ALIASES,
    NewsSource,
    NseEventsSource,
    RawHeadline,
    attribute_symbol,
    fetch_all_news,
    parse_rss,
)

FIXTURES = Path(__file__).parent / "fixtures" / "news"


# ---------------------------------------------------------------------------
# parse_rss
# ---------------------------------------------------------------------------


def test_parse_rss_moneycontrol() -> None:
    xml = (FIXTURES / "moneycontrol_sample.xml").read_text(encoding="utf-8")
    items = parse_rss(xml, "moneycontrol_markets")
    assert len(items) == 3
    first = items[0]
    assert first.source == "moneycontrol_markets"
    assert "Reliance" in first.headline
    assert first.url.startswith("https://")
    assert first.ts.tzinfo is not None  # tz-aware
    assert first.ts.year == 2026


def test_parse_rss_skips_entries_without_title_or_url() -> None:
    xml = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title><link>x</link>
  <item><title></title><link>https://a</link></item>
  <item><title>has title</title><link></link></item>
  <item><title>good</title><link>https://good</link></item>
</channel></rss>"""
    items = parse_rss(xml, "test")
    assert [i.headline for i in items] == ["good"]


def test_parse_rss_handles_missing_pubdate_with_now() -> None:
    xml = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title><link>x</link>
  <item><title>news</title><link>https://x</link></item>
</channel></rss>"""
    before = datetime.now(UTC)
    items = parse_rss(xml, "test")
    after = datetime.now(UTC)
    assert len(items) == 1
    assert before <= items[0].ts <= after


# ---------------------------------------------------------------------------
# attribute_symbol
# ---------------------------------------------------------------------------


def test_attribute_symbol_matches_ticker_directly() -> None:
    assert attribute_symbol("NTPC commissions 800 MW solar capacity", DEFAULT_ALIASES) == "NTPC"


def test_attribute_symbol_matches_alias() -> None:
    assert (
        attribute_symbol("Reliance Industries Q4 beats", DEFAULT_ALIASES)
        == "RELIANCE"
    )


def test_attribute_symbol_case_insensitive() -> None:
    assert attribute_symbol("reliance gains 3%", DEFAULT_ALIASES) == "RELIANCE"


def test_attribute_symbol_whole_word_only() -> None:
    # "PFCREEK" must not match "PFC"
    assert attribute_symbol("PFCREEK Foods reports loss", DEFAULT_ALIASES) is None


def test_attribute_symbol_returns_none_when_no_match() -> None:
    assert attribute_symbol("Nifty closes 1% higher on macro relief", DEFAULT_ALIASES) is None


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


@dataclass
class FixtureSource:
    """Stub NewsSource that replays a saved RSS fixture."""

    name: str
    fixture_path: Path

    def fetch(self) -> list[RawHeadline]:
        xml = self.fixture_path.read_text(encoding="utf-8")
        return parse_rss(xml, self.name)


@dataclass
class BoomSource:
    name: str = "boom"

    def fetch(self) -> list[RawHeadline]:
        raise RuntimeError("simulated source failure")


def test_fetch_all_news_dedupes_by_url() -> None:
    """MC & BS fixtures share one URL — orchestrator must keep only the first."""
    sources: list[NewsSource] = [
        FixtureSource(name="mc", fixture_path=FIXTURES / "moneycontrol_sample.xml"),
        FixtureSource(name="bs", fixture_path=FIXTURES / "bs_sample.xml"),
    ]
    items = fetch_all_news(sources=sources, aliases=DEFAULT_ALIASES)
    urls = [i.url for i in items]
    assert len(urls) == len(set(urls))
    # The duplicate (reliance-q4-results-12345) is present exactly once
    assert (
        sum(1 for u in urls if u and "reliance-q4-results-12345" in u) == 1
    )


def test_fetch_all_news_attributes_symbols() -> None:
    sources: list[NewsSource] = [
        FixtureSource(name="mc", fixture_path=FIXTURES / "moneycontrol_sample.xml"),
    ]
    items = fetch_all_news(sources=sources)
    by_url = {i.url: i for i in items}
    reliance_item = next(i for u, i in by_url.items() if u and "reliance" in u.lower())
    assert reliance_item.symbol == "RELIANCE"
    ntpc_item = next(i for u, i in by_url.items() if u and "ntpc" in u.lower())
    assert ntpc_item.symbol == "NTPC"
    # The SEBI story has no portfolio-symbol → symbol stays None
    sebi_item = next(i for u, i in by_url.items() if u and "sebi-bars" in u.lower())
    assert sebi_item.symbol is None


def test_fetch_all_news_isolates_failing_source() -> None:
    sources: list[NewsSource] = [
        BoomSource(),
        FixtureSource(name="mc", fixture_path=FIXTURES / "moneycontrol_sample.xml"),
    ]
    items = fetch_all_news(sources=sources)
    # MC still yields 3 items even though BoomSource raised
    assert len(items) == 3


def test_fetch_all_news_returns_iso_timestamps() -> None:
    sources: list[NewsSource] = [
        FixtureSource(name="mc", fixture_path=FIXTURES / "moneycontrol_sample.xml"),
    ]
    items = fetch_all_news(sources=sources)
    for item in items:
        assert isinstance(item.ts, str)
        assert item.ts.startswith("2026")
        assert "+" in item.ts or item.ts.endswith("+00:00") or "T" in item.ts


# ---------------------------------------------------------------------------
# NseEventsSource — failure isolation (the actual call requires live NSE)
# ---------------------------------------------------------------------------


def test_nse_events_source_returns_empty_on_import_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If nsepython is missing or its API raises, the source must return []."""
    import sys

    monkeypatch.setitem(sys.modules, "nsepython", None)  # forces ImportError
    out = NseEventsSource().fetch()
    assert out == []


def test_nse_events_source_returns_empty_on_call_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If nse_events() raises (rate-limited / schema drift), source returns []."""
    import sys
    import types

    fake = types.ModuleType("nsepython")

    def boom() -> None:
        raise RuntimeError("rate-limited by NSE")

    fake.nse_events = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nsepython", fake)
    assert NseEventsSource().fetch() == []
