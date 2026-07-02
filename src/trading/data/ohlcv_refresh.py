"""Incremental OHLCV refresh + a Kite close cross-check (Phase 12.7, F-018).

`ingest_history` does a one-shot backfill; nothing kept the parquet store
current between manual runs, so the scan could silently compute on month-old
bars. `refresh_ohlcv` pulls only the missing tail per symbol and is cheap
enough to run inside every `pre_open`. `cross_check_closes` compares the
parquet's last close to the broker's official close for held symbols, flagging
stale data or unadjusted splits.

F-054: a plain tail-append never re-adjusts already-stored history. yfinance
re-scales the *entire* series to the latest corporate action on every fetch
(`auto_adjust=True`), so a split/bonus between refreshes leaves a step
discontinuity — old bars on the pre-action scale, new bars on the post-action
scale. `_refresh_one` now fetches a small **overlap** window (`last_bar - 7`
calendar days) and compares the re-fetched overlap closes to the stored ones.
A match means a normal tail-append; a mismatch (or no shared dates at all)
means the stored history was re-scaled underneath us, so the symbol's full
history is re-fetched and the parquet is overwritten instead of appended.
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
# warning (split, stale bar, or wrong symbol). The same tolerance decides when
# the refresh's re-fetched overlap closes count as re-scaled history (F-054).
CLOSE_DEVIATION_TOL = 0.005  # 0.5%

# Calendar days re-fetched *before* the stored last bar so the refresh can
# compare closes on shared dates (F-054). 7 calendar days spans >=5 NSE
# trading days outside long holiday runs — enough anchors to spot a re-scale.
OVERLAP_CALENDAR_DAYS = 7


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
    When the re-fetched overlap diverges from stored closes (split/bonus),
    the symbol's full history is re-fetched and overwritten and a warning
    naming the symbol is appended (F-054).
    """
    syms = symbols if symbols is not None else load_universe()
    refreshed = 0
    failed = 0
    bars_added = 0
    warnings: list[str] = []

    for sym in syms:
        try:
            added, note = _refresh_one(sym, paths, as_of)
        except Exception as e:  # per-symbol isolation — never abort the loop
            failed += 1
            warnings.append(f"{sym}: refresh failed — {type(e).__name__}: {e}")
            continue
        if note:
            warnings.append(note)
        if added > 0:
            refreshed += 1
            bars_added += added

    return RefreshResult(
        symbols_refreshed=refreshed,
        symbols_failed=failed,
        bars_added=bars_added,
        warnings=warnings,
    )


def _refresh_one(symbol: str, paths: Paths, as_of: date) -> tuple[int, str | None]:
    """Refresh one symbol.

    Returns `(genuinely_new_bars_written, note)`; `note` is a warning string
    when the overlap check forced a full re-backfill (F-054), else None.
    """
    try:
        existing = read_ohlcv(symbol, paths)
    except FileNotFoundError:
        existing = None
    if existing is not None and existing.empty:
        existing = None

    if existing is None:
        # No usable parquet — behave like a manual ingest: full backfill.
        full = _fetch_window(symbol, DEFAULT_HISTORY_START, as_of)
        if full.empty:
            return 0, None
        write_ohlcv(full.sort_index(), symbol, paths)
        return len(full), None

    last_bar = existing.index[-1].date()
    if last_bar + timedelta(days=1) >= as_of:
        return 0, None  # already current — nothing between last bar and as_of

    # F-054: fetch a small overlap window so the re-fetched closes can be
    # compared to the stored ones on shared dates. A divergence means yfinance
    # re-adjusted the whole series (split/bonus) since the last refresh.
    overlap_start = last_bar - timedelta(days=OVERLAP_CALENDAR_DAYS)
    fetched = _fetch_window(symbol, overlap_start, as_of)
    if fetched.empty:
        return 0, None

    shared = existing.index.intersection(fetched.index)
    if len(shared) == 0:
        # No anchor dates to compare against — safest is a full re-fetch.
        note = (
            f"{symbol}: overlap re-fetch shares no dates with stored history — "
            "full re-backfill"
        )
        return _rebackfill(symbol, paths, as_of, existing), note

    stored_closes = existing.loc[shared, "close"]
    refetched_closes = fetched.loc[shared, "close"]
    max_dev = float(((refetched_closes - stored_closes).abs() / stored_closes.abs()).max())
    if max_dev > CLOSE_DEVIATION_TOL:
        note = (
            f"{symbol}: overlap closes diverge {max_dev * 100:.1f}% from stored — "
            "history re-scaled (split/bonus?), full re-backfill"
        )
        return _rebackfill(symbol, paths, as_of, existing), note

    combined = pd.concat([existing, fetched])
    # New bars win on any date collision, then restore chronological order.
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    added = len(combined) - len(existing)
    write_ohlcv(combined, symbol, paths)
    return added, None


def _fetch_window(symbol: str, start: date | str, as_of: date) -> pd.DataFrame:
    """Fetch `[start, as_of)` bars, dropping any row on/after `as_of`.

    Defensive: never let the forming/same-day bar through even if yfinance
    returns it despite the exclusive end.
    """
    fetched = fetch_ohlcv(symbol, start, as_of)  # end exclusive
    return fetched[fetched.index < pd.Timestamp(as_of)]


def _rebackfill(symbol: str, paths: Paths, as_of: date, existing: pd.DataFrame) -> int:
    """Re-fetch the full history and overwrite the parquet (F-054 self-heal).

    Returns the count of dates not already present — keeps `bars_added`
    meaning "genuinely new bars" even when every stored value was rewritten.
    """
    full = _fetch_window(symbol, DEFAULT_HISTORY_START, as_of)
    if full.empty:
        return 0  # keep the stored history rather than wiping it
    write_ohlcv(full.sort_index(), symbol, paths)
    return len(full.index.difference(existing.index))


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
