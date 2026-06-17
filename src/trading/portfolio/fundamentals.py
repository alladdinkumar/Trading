"""Static fundamentals source for the health scorer (F-022).

The project has no live fundamentals fetcher — yfinance's `Ticker.info` is
sparse and flaky for Indian equities and would drag a network call into the
pre-open hot path. Instead we read an offline, hand-maintained CSV (mirroring
`data/static/sector_map.csv`): deterministic, testable, and swappable for a
real fetcher later behind this same `load_fundamentals_map` seam.

The file is optional. A missing/empty CSV yields `{}`, so holdings fall back to
a technicals(+sentiment)-only ballot — which, with the F-022 votes_cast
scaling, still produces real HOLD/EXIT verdicts.
"""

from __future__ import annotations

import csv
from pathlib import Path

from trading.config import Paths, get_paths
from trading.portfolio.health import FundamentalsSnapshot

_FIELDS = (
    "profit_growth_yoy",
    "profit_cagr_3y",
    "debt_to_equity",
    "roe",
    "pe_percentile_5y",
)


def _default_fundamentals_path(paths: Paths | None = None) -> Path:
    p = paths if paths is not None else get_paths()
    return p.project_root / "data" / "static" / "fundamentals.csv"


def _parse_float(value: str | None) -> float | None:
    """Empty / blank / unparseable → None (contributes 0 votes, never penalises)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_fundamentals_map(paths: Paths | None = None) -> dict[str, FundamentalsSnapshot]:
    """Read `data/static/fundamentals.csv` → `{symbol: FundamentalsSnapshot}`.

    Header: `symbol,profit_growth_yoy,profit_cagr_3y,debt_to_equity,roe,
    pe_percentile_5y` (decimals: 0.15 = 15%). Blank lines and `#` comments are
    skipped; blank cells become None. Returns `{}` when the file is absent so
    callers degrade gracefully.
    """
    path = _default_fundamentals_path(paths)
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
    out: dict[str, FundamentalsSnapshot] = {}
    for row in reader:
        sym = (row.get("symbol") or "").strip()
        if not sym:
            continue
        out[sym] = FundamentalsSnapshot(**{f: _parse_float(row.get(f)) for f in _FIELDS})
    return out
