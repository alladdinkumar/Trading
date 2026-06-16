"""Incremental OHLCV refresh + a Kite close cross-check (Phase 12.7, F-018).

`ingest_history` does a one-shot backfill; nothing kept the parquet store
current between manual runs, so the scan could silently compute on month-old
bars. `refresh_ohlcv` pulls only the missing tail per symbol and is cheap
enough to run inside every `pre_open`. `cross_check_closes` compares the
parquet's last close to the broker's official close for held symbols, flagging
stale data or unadjusted splits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from trading.config import Paths
from trading.data.kite import Holding
from trading.data.universe import load_universe
from trading.data.yfinance import fetch_ohlcv
from trading.store.ohlcv import read_ohlcv, write_ohlcv

# Match `ingest_history`'s default backfill window so a symbol that has no
# parquet yet gets the same history depth as a manual ingest.
DEFAULT_HISTORY_START = "2023-01-01"

# Relative deviation above which a parquet-vs-Kite close mismatch is worth a
# warning (split, stale bar, or wrong symbol).
CLOSE_DEVIATION_TOL = 0.005  # 0.5%


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of a `refresh_ohlcv` run."""

    symbols_refreshed: int
    symbols_failed: int
    bars_added: int
    warnings: list[str]


def refresh_ohlcv(
    paths: Paths,
    as_of: date,
    symbols: list[str] | None = None,
) -> RefreshResult:
    """Fetch and append any bars missing up to (but excluding) `as_of`.

    Defaults to the full ingest universe (`universe.txt`). Each symbol is
    isolated: a fetch failure appends a warning and the loop continues. A
    symbol already current through the last trading day is a cheap no-op
    (0 bars, not counted as refreshed). The forming `as_of` bar is never
    requested (`fetch_ohlcv` end is exclusive), so partial bars can't land.
    """
    syms = symbols if symbols is not None else load_universe()
    refreshed = 0
    failed = 0
    bars_added = 0
    warnings: list[str] = []

    for sym in syms:
        try:
            added = _refresh_one(sym, paths, as_of)
        except Exception as e:  # per-symbol isolation — never abort the loop
            failed += 1
            warnings.append(f"{sym}: refresh failed — {type(e).__name__}: {e}")
            continue
        if added > 0:
            refreshed += 1
            bars_added += added

    return RefreshResult(
        symbols_refreshed=refreshed,
        symbols_failed=failed,
        bars_added=bars_added,
        warnings=warnings,
    )


def _refresh_one(symbol: str, paths: Paths, as_of: date) -> int:
    """Refresh one symbol; return the count of genuinely new bars written."""
    try:
        existing = read_ohlcv(symbol, paths)
    except FileNotFoundError:
        existing = None

    if existing is not None and not existing.empty:
        last_bar = existing.index[-1].date()
        next_bar = last_bar + timedelta(days=1)
        if next_bar >= as_of:
            return 0  # already current — nothing between last bar and as_of
        start: date | str = next_bar
    else:
        existing = None
        start = DEFAULT_HISTORY_START

    fetched = fetch_ohlcv(symbol, start, as_of)  # end exclusive
    # Defensive: never let the forming/same-day bar through even if yfinance
    # returns it despite the exclusive end.
    fetched = fetched[fetched.index < pd.Timestamp(as_of)]
    if fetched.empty:
        return 0

    if existing is not None:
        combined = pd.concat([existing, fetched])
        # New bars win on any date collision, then restore chronological order.
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        added = len(combined) - len(existing)
    else:
        combined = fetched.sort_index()
        added = len(combined)

    write_ohlcv(combined, symbol, paths)
    return added


def cross_check_closes(
    paths: Paths,
    as_of: date,
    holdings: list[Holding],
) -> list[str]:
    """Compare each holding's parquet last close to its Kite close_price.

    Returns one warning string per holding whose relative deviation exceeds
    `CLOSE_DEVIATION_TOL`. Holdings without parquet are skipped silently —
    the refresh/scan staleness paths already surface those.
    """
    warnings: list[str] = []
    for h in holdings:
        try:
            df = read_ohlcv(h.tradingsymbol, paths, end=as_of)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        parquet_close = float(df["close"].iloc[-1])
        kite_close = h.close_price
        if not kite_close:  # None or 0.0 — nothing to compare against
            continue
        dev = (parquet_close - kite_close) / kite_close
        if abs(dev) > CLOSE_DEVIATION_TOL:
            warnings.append(
                f"{h.tradingsymbol}: parquet close {parquet_close:.2f} vs "
                f"Kite close {kite_close:.2f} ({dev * 100:+.1f}%) — stale or split?"
            )
    return warnings
