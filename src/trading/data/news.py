"""News aggregation: RSS feeds + NSE corporate events.

Each source is wrapped behind an adapter so adding/removing feeds is local.
A single failing source must not abort the whole pull — markets news
infrastructure changes frequently (per spec §18 risk row).

The output is a list of `NewsItem` rows ready for the `news_items` SQLite
table. `sentiment` / `category` / `is_critical` are populated later by
`features.sentiment`, not here.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import feedparser

from trading.config import Paths, get_paths, get_settings
from trading.data.cache import get_cached_session
from trading.domain import NewsItem

# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawHeadline:
    """A headline as it came off a source feed (pre-symbol-attribution)."""

    ts: datetime  # tz-aware UTC
    source: str
    headline: str
    url: str
    summary: str | None = None


class NewsSource(Protocol):
    """Strategy interface — anything with a `fetch()` method works.

    Each implementation also exposes a `name` (set on the dataclass), but
    we don't put it in the Protocol because frozen-dataclass attributes
    can't satisfy a Protocol's settable-attribute declaration.
    """

    def fetch(self) -> list[RawHeadline]: ...


# ---------------------------------------------------------------------------
# RSS source
# ---------------------------------------------------------------------------

RSS_FEEDS: dict[str, str] = {
    "moneycontrol_markets": "https://www.moneycontrol.com/rss/marketsnews.xml",
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "bs_markets": "https://www.business-standard.com/rss/markets-106.rss",
}


@dataclass(frozen=True)
class RssFeed:
    """One RSS source. Pulls via the shared cached HTTP session."""

    name: str
    url: str
    timeout: int = 15

    def fetch(self) -> list[RawHeadline]:
        session = get_cached_session()
        ua = get_settings(load_dotenv=False).news_user_agent
        resp = session.get(self.url, headers={"User-Agent": ua}, timeout=self.timeout)
        resp.raise_for_status()
        return parse_rss(resp.text, self.name)


def parse_rss(xml_text: str, source: str) -> list[RawHeadline]:
    """Parse an RSS/Atom feed body into RawHeadlines. Tolerant of missing fields."""
    parsed = feedparser.parse(xml_text)
    out: list[RawHeadline] = []
    for entry in parsed.entries:
        headline = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        if not headline or not url:
            continue
        out.append(
            RawHeadline(
                ts=_entry_timestamp(entry),
                source=source,
                headline=headline,
                url=url,
                summary=(entry.get("summary") or None),
            )
        )
    return out


def _entry_timestamp(entry: Any) -> datetime:
    """Best-effort UTC timestamp from a feedparser entry; falls back to `now()`."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                y, m, d, hh, mm, ss = (int(x) for x in parsed[:6])
                return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# NSE corporate events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NseEventsSource:
    """NSE corporate event calendar via nsepython.

    Returned headlines look like "RVNL: Board Meeting on 2026-05-20".
    Best-effort: any failure (rate-limit, schema change, import) yields []
    rather than killing the wider pull. Mitigates spec §18 ("News RSS sources
    go down or change format").
    """

    name: str = "nse_events"

    def fetch(self) -> list[RawHeadline]:
        try:
            from nsepython import nse_events

            df = nse_events()
        except Exception:
            return []
        if df is None or df.empty:
            return []
        out: list[RawHeadline] = []
        for _, row in df.iterrows():
            symbol = (row.get("symbol") or "").strip()
            purpose = (row.get("purpose") or row.get("subject") or "").strip()
            date_str = (row.get("date") or row.get("BMDate") or "").strip()
            if not symbol or not purpose:
                continue
            headline = f"{symbol}: {purpose}" + (f" on {date_str}" if date_str else "")
            ts = _parse_event_date(date_str) or datetime.now(UTC)
            out.append(
                RawHeadline(
                    ts=ts,
                    source=self.name,
                    headline=headline,
                    url=event_url(symbol, purpose, date_str),
                )
            )
        return out


def event_url(symbol: str, purpose: str, date_str: str) -> str:
    """Per-event URL for an NSE corporate event.

    The NSE event-calendar page is the same per symbol, so keying dedup on the
    bare symbol URL collapsed distinct events into one row (F-016). We pin the
    event identity — `(purpose, date)` — into the URL fragment so each event is
    unique while still linking to the symbol's calendar page. The fragment is
    deterministic, so the same event re-fetched on a later run yields the same
    URL and dedupes cleanly (in-run and via the DB unique index).
    """
    base = (
        "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
        f"?symbol={symbol}"
    )
    fingerprint = re.sub(r"[^a-z0-9]+", "-", f"{purpose} {date_str}".lower()).strip("-")
    return f"{base}#{fingerprint}" if fingerprint else base


def _parse_event_date(s: str) -> datetime | None:
    """NSE returns dates in mixed formats; try the common ones."""
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Symbol attribution
# ---------------------------------------------------------------------------

# Built-in fallback used only when `data/static/aliases.csv` is absent (fresh
# checkout) — the shipped CSV (F-015) covers the full Nifty-50 + holdings ingest
# universe. The matcher is whole-word and case-insensitive.
DEFAULT_ALIASES: dict[str, list[str]] = {
    "RELIANCE": ["Reliance", "RIL", "Reliance Industries"],
    "TATAPOWER": ["Tata Power"],
    "NTPC": ["NTPC"],
    "RVNL": ["RVNL", "Rail Vikas"],
    "IRB": ["IRB Infra", "IRB Infrastructure"],
    "PFC": ["Power Finance", "PFC Ltd"],
    "RECLTD": ["REC Ltd", "REC Limited"],
    "COALINDIA": ["Coal India"],
    "IDFCFIRSTB": ["IDFC First Bank", "IDFC First"],
    "IREDA": ["IREDA"],
    "JIOFIN": ["Jio Financial", "Jio Fin"],
    "MAZDOCK": ["Mazagon Dock", "MDL"],
}


def _default_aliases_path(paths: Paths | None = None) -> Path:
    p = paths if paths is not None else get_paths()
    return p.project_root / "data" / "static" / "aliases.csv"


def load_aliases_map(paths: Paths | None = None) -> dict[str, list[str]]:
    """Read `data/static/aliases.csv` → `{symbol: [aliases]}`.

    Header: `symbol,aliases` where `aliases` is a `|`-separated list of
    company-name variants (the bare ticker is always matched too, so the cell
    may be blank). Blank lines and `#` comments are skipped. Returns `{}` when
    the file is absent so callers can fall back to `DEFAULT_ALIASES`.
    """
    path = _default_aliases_path(paths)
    if not path.is_file():
        return {}
    cleaned: list[str] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cleaned.append(line)
    if not cleaned:
        return {}
    reader = csv.DictReader(cleaned)
    out: dict[str, list[str]] = {}
    for row in reader:
        sym = (row.get("symbol") or "").strip()
        if not sym:
            continue
        raw_aliases = (row.get("aliases") or "").strip()
        out[sym] = [a.strip() for a in raw_aliases.split("|") if a.strip()]
    return out


def default_aliases(paths: Paths | None = None) -> dict[str, list[str]]:
    """The alias map for attribution + the rollup watch-list.

    Prefers the maintained `data/static/aliases.csv`; falls back to the built-in
    `DEFAULT_ALIASES` on a fresh checkout that hasn't shipped the CSV.
    """
    return load_aliases_map(paths) or DEFAULT_ALIASES


def attribute_symbol(headline: str, aliases: dict[str, list[str]]) -> str | None:
    """Return the first symbol whose ticker or alias appears as a whole word
    in `headline`. Case-insensitive. Returns None when nothing matches."""
    upper = headline.upper()
    for symbol, alias_list in aliases.items():
        for needle in (symbol, *alias_list):
            if re.search(rf"\b{re.escape(needle.upper())}\b", upper):
                return symbol
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def default_sources(include_nse: bool = True) -> list[NewsSource]:
    """The built-in source list. Tests pass a custom list to stub network."""
    sources: list[NewsSource] = [RssFeed(name=k, url=v) for k, v in RSS_FEEDS.items()]
    if include_nse:
        sources.append(NseEventsSource())
    return sources


def fetch_all_news(
    *,
    sources: Iterable[NewsSource] | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> list[NewsItem]:
    """Pull every source, dedup by URL, attribute symbols, return DB-ready rows.

    Sources are isolated: a raising adapter contributes nothing but doesn't
    abort the others. Symbol attribution leaves `symbol=None` when no alias
    matches — those rows still feed sector/macro narrative downstream.
    """
    src_list = list(sources) if sources is not None else default_sources()
    alias_map = aliases if aliases is not None else default_aliases()

    seen: set[str] = set()
    out: list[NewsItem] = []
    for src in src_list:
        try:
            raws = src.fetch()
        except Exception:
            continue
        for raw in raws:
            if raw.url in seen:
                continue
            seen.add(raw.url)
            out.append(
                NewsItem(
                    ts=raw.ts.isoformat(),
                    symbol=attribute_symbol(raw.headline, alias_map),
                    source=raw.source,
                    headline=raw.headline,
                    url=raw.url,
                )
            )
    return out
