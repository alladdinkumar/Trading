"""NSE sectoral indices — relative-strength snapshot.

Mirrors data/macro.py: a defensive yfinance wrapper per source so a
single failing ticker doesn't abort the whole snapshot. RS is the simple
difference between sector and benchmark returns over a window. Per-row
`regime` ('LEADING' / 'NEUTRAL' / 'LAGGING') is derived from rs_20d.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from trading.config import Paths, get_paths

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 11 NSE sectoral indices we track. Keys are the codes that live in
# sector_daily.sector + data/static/sector_map.csv.
SECTOR_TICKERS: dict[str, str] = {
    "NIFTYBANK": "^NSEBANK",
    "IT": "^CNXIT",
    "AUTO": "^CNXAUTO",
    "FMCG": "^CNXFMCG",
    "PHARMA": "^CNXPHARMA",
    "METAL": "^CNXMETAL",
    "ENERGY": "^CNXENERGY",
    "REALTY": "^CNXREALTY",
    "PSUBANK": "^CNXPSUBANK",
    "FINSERV": "^CNXFIN",
    "INFRA": "^CNXINFRA",
}

BENCHMARK_TICKER = "^NSEI"  # Nifty 50
RS_WINDOWS: tuple[int, int, int] = (5, 20, 60)
LEADING_THRESHOLD = 0.02
LAGGING_THRESHOLD = -0.02


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def compute_rs(
    sector_closes: pd.Series, benchmark_closes: pd.Series, *, window: int
) -> float | None:
    """Simple-difference RS = sector_return_N - benchmark_return_N.

    Returns None if either series has fewer than window+1 bars or the
    lookback close is zero (would divide by zero).
    """
    if len(sector_closes) < window + 1 or len(benchmark_closes) < window + 1:
        return None
    s_now = float(sector_closes.iloc[-1])
    s_back = float(sector_closes.iloc[-(window + 1)])
    b_now = float(benchmark_closes.iloc[-1])
    b_back = float(benchmark_closes.iloc[-(window + 1)])
    if s_back == 0 or b_back == 0:
        return None
    sector_ret = (s_now / s_back) - 1.0
    bench_ret = (b_now / b_back) - 1.0
    return sector_ret - bench_ret


def _regime_for(rs_20d: float | None) -> str | None:
    """Apply leading/lagging thresholds. Strictly outside the band → label."""
    if rs_20d is None:
        return None
    if rs_20d > LEADING_THRESHOLD:
        return "LEADING"
    if rs_20d < LAGGING_THRESHOLD:
        return "LAGGING"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Sector map
# ---------------------------------------------------------------------------


def _default_sector_map_path(paths: Paths | None = None) -> Path:
    p = paths if paths is not None else get_paths()
    return p.project_root / "data" / "static" / "sector_map.csv"


def load_sector_map(paths: Paths | None = None) -> dict[str, str]:
    """Read `data/static/sector_map.csv` (header: symbol,sector).

    Returns `{symbol: sector_code}`. Skips blank lines and `#` comments.
    Returns `{}` if the file doesn't exist (graceful — callers degrade).
    """
    path = _default_sector_map_path(paths)
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
    out: dict[str, str] = {}
    for row in reader:
        sym = (row.get("symbol") or "").strip()
        sec = (row.get("sector") or "").strip()
        if sym and sec:
            out[sym] = sec
    return out


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_sector_history(ticker: str, *, lookback_days: int = 90) -> pd.DataFrame | None:
    """Pull a daily-close history for `ticker`. Returns None on any failure.

    Same defensive pattern as data.macro.fetch_yf_quote: HTTP error,
    rate-limit, deprecated symbol → None so the surrounding snapshot keeps
    going. Returned frame is single-level columns with a `close` column,
    indexed by trading date.
    """
    try:
        raw: Any = yf.download(
            ticker,
            period=f"{lookback_days}d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            actions=False,
        )
    except Exception:
        return None
    if raw is None or getattr(raw, "empty", True):
        return None
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    if "Close" not in raw.columns or len(raw) == 0:
        return None
    df = raw[["Close"]].rename(columns={"Close": "close"}).dropna()
    if df.empty:
        return None
    return df


def fetch_all_sectors(as_of: date) -> list[SectorRow]:
    """Pull benchmark + every sector ticker; build SectorRow per success.

    Per-sector fetch failures yield no row (so the count reflects success).
    Benchmark failure returns an empty list — without it, RS is undefined.
    All rows are tagged with `as_of.isoformat()` regardless of the actual
    last-bar date (yfinance may lag a day after market close).
    """
    bench = fetch_sector_history(BENCHMARK_TICKER)
    if bench is None or bench.empty:
        return []
    bench_closes = bench["close"]
    rows: list[SectorRow] = []
    for sector_code, ticker in SECTOR_TICKERS.items():
        history = fetch_sector_history(ticker)
        if history is None or history.empty:
            continue
        closes = history["close"]
        last_close = float(closes.iloc[-1])
        rs_values: dict[int, float | None] = {
            w: compute_rs(closes, bench_closes, window=w) for w in RS_WINDOWS
        }
        rows.append(
            SectorRow(
                date=as_of.isoformat(),
                sector=sector_code,
                close=last_close,
                rs_5d=rs_values[5],
                rs_20d=rs_values[20],
                rs_60d=rs_values[60],
                regime=_regime_for(rs_values[20]),
            )
        )
    return rows
