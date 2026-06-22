"""Neutral domain types — the single home for cross-layer DTOs (F-006).

These frozen dataclasses describe *what a row is*. They used to live in the
ingestion modules (`data.macro`, `data.news`, `data.sector`, `data.yfinance`),
which forced the persistence layer (`store`) to depend "up" into `data` just to
name a row. Hosting them here lets both the producer (`data`) and the persister
(`store`) import the shape from a layer below both — the dependency now points
the right way.

This module imports nothing from other `trading` packages (it sits at the
foundation alongside `config`), so it can never participate in a layering cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- OHLCV schema -----------------------------------------------------------

#: yfinance names NSE tickers with this suffix; storage strips it at the
#: boundary so on-disk filenames are the plain NSE symbol.
NSE_SUFFIX = ".NS"

#: The canonical, ordered OHLCV column schema. Ingestion normalises to it and
#: storage validates against it.
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


# --- Row DTOs ---------------------------------------------------------------


@dataclass(frozen=True)
class SectorRow:
    """One row of `sector_daily`."""

    date: str  # YYYY-MM-DD
    sector: str
    close: float
    rs_5d: float | None
    rs_20d: float | None
    rs_60d: float | None
    regime: str | None  # 'LEADING' | 'NEUTRAL' | 'LAGGING' | None


@dataclass(frozen=True)
class MacroSnapshot:
    """One row of `macro_snapshot`. All fields nullable per schema."""

    date: str  # YYYY-MM-DD
    sgx_nifty: float | None
    dow_fut: float | None
    nasdaq_fut: float | None
    sp500: float | None
    usdinr: float | None
    crude: float | None
    vix: float | None
    us_10y: float | None
    fii_flow_cr: float | None
    dii_flow_cr: float | None
    regime: str | None  # 'RISK_ON' | 'NEUTRAL' | 'RISK_OFF' | None


@dataclass(frozen=True)
class NewsItem:
    """One row of the `news_items` table.

    `sentiment` / `category` / `is_critical` are filled by the scorer in a
    later pass — we keep them as Optional fields so the news fetch and
    scoring steps stay independently testable.
    """

    ts: str  # ISO 8601 with tz
    symbol: str | None
    source: str
    headline: str
    url: str | None
    sentiment: float | None = None
    category: str | None = None
    is_critical: bool = False
