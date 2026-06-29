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
    """One row of `macro_snapshot`. All fields nullable per schema.

    Naming caveat (F-017): despite the `_fut` suffix, `dow_fut` and
    `nasdaq_fut` hold the SPOT index closes (`^DJI` / `^IXIC`), not index
    futures — see the fetcher in `data/macro.py` (`YF_TICKERS`). They are
    used as overnight global-direction proxies; the regime voter reads the
    underlying quote dict, not these fields, so the misnomer is cosmetic.
    `sgx_nifty` is reserved/unpopulated (always None): there is no reliable
    free yfinance GIFT/SGX-Nifty ticker since the 2023 SGX->IFSC move.
    """

    date: str  # YYYY-MM-DD
    sgx_nifty: float | None  # reserved — never populated (no free feed); always None
    dow_fut: float | None  # SPOT ^DJI close (NOT a future) — see class docstring
    nasdaq_fut: float | None  # SPOT ^IXIC close (NOT a future) — see class docstring
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
