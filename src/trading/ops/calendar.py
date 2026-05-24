"""NSE trading-day calendar.

`is_trading_day(d)` returns False for weekends OR any date in
`nse_holidays(d.year)`. Holidays are fetched from `nsepython` once per
year (cached), with a bundled JSON fallback at
`data/static/nse_holidays_<year>.json`. If both fail, only the weekday
check applies (best-effort — we'd rather over-trade on a forgotten
holiday than miss a real session).
"""

from __future__ import annotations

import functools
import json
from datetime import date, datetime
from pathlib import Path

from loguru import logger

from trading.config import get_paths


def _fetch_holidays_from_nsepython(year: int) -> frozenset[date]:
    """Pull NSE trading holidays for `year` from nsepython.

    Indirected through a module function so tests can monkeypatch
    without touching the real API. Raises any nsepython exception
    upward — the caller handles fallback.
    """
    from nsepython import holiday_master  # local import: keeps import-time light

    raw = holiday_master()
    rows = raw.get("CM", []) if isinstance(raw, dict) else []
    out: set[date] = set()
    for row in rows:
        s = row.get("tradingDate") or row.get("date")
        if not s:
            continue
        try:
            out.add(date.fromisoformat(_to_iso(s)))
        except ValueError:
            continue
    return frozenset(d for d in out if d.year == year)


def _to_iso(s: str) -> str:
    """Convert "26-Jan-2026" or "2026-01-26" to ISO 'YYYY-MM-DD'."""
    if "-" in s and len(s.split("-")[0]) == 4:
        return s
    return datetime.strptime(s, "%d-%b-%Y").date().isoformat()


def _bundled_holidays_path(year: int) -> Path:
    return get_paths().project_root / "data" / "static" / f"nse_holidays_{year}.json"


def _load_bundled_holidays(year: int) -> frozenset[date]:
    path = _bundled_holidays_path(year)
    if not path.exists():
        return frozenset()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return frozenset(
            date.fromisoformat(item["date"])
            for item in payload.get("holidays", [])
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Failed to parse bundled holidays for {year}: {e}")
        return frozenset()


@functools.cache
def nse_holidays(year: int) -> frozenset[date]:
    """Return NSE trading holidays for `year`.

    Tries nsepython first; on any failure, falls back to the bundled
    JSON. If both fail, returns an empty frozenset (and weekday check
    becomes the only gate).
    """
    try:
        return _fetch_holidays_from_nsepython(year)
    except Exception as e:
        logger.warning(
            f"nsepython holiday fetch failed for {year}: {e} — using bundled fallback"
        )
        return _load_bundled_holidays(year)


def is_trading_day(d: date) -> bool:
    """True iff `d` is Mon-Fri AND not in `nse_holidays(d.year)`."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in nse_holidays(d.year)
