"""Phase 15 — cached read-only data layer for the Streamlit dashboard.

Every dashboard read goes through one of these helpers. Each is wrapped in
``@st.cache_data(ttl=60)`` so a live session re-reads state at most once a
minute; cache key is the function args (usually a date string).

All helpers degrade gracefully — missing data returns ``None`` / empty
DataFrame / empty list rather than raising, so pages branch on truthiness
and render an empty-state instead of a stack trace.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from trading.config import Paths, get_paths
from trading.data import kite_snapshot, quotes_snapshot
from trading.data.kite import GttOrder, Holding
from trading.features.technicals import add_indicators
from trading.store.db import get_conn
from trading.store.ohlcv import read_ohlcv

# ---------------------------------------------------------------------------
# Paths + connection
# ---------------------------------------------------------------------------


def paths() -> Paths:
    """Single accessor so pages don't import config directly."""
    return get_paths()


def _date_arg(d: date | str) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(d)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60)
def available_research_dates() -> list[str]:
    """Sorted YYYY-MM-DD dir names under data/research/ (most recent first)."""
    research = paths().research_dir
    if not research.is_dir():
        return []
    out = []
    for entry in research.iterdir():
        if entry.is_dir() and len(entry.name) == 10 and entry.name[4] == "-":
            out.append(entry.name)
    out.sort(reverse=True)
    return out


# ---------------------------------------------------------------------------
# Macro / regime
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60)
def load_macro_snapshot(as_of: str) -> dict[str, Any] | None:
    """Return the macro_snapshot row for `as_of` or None if missing."""
    with get_conn(paths().db_path) as conn:
        row = conn.execute(
            "SELECT * FROM macro_snapshot WHERE date = ?", (as_of,)
        ).fetchone()
    return dict(row) if row else None


@st.cache_data(ttl=60)
def load_macro_history(limit: int = 90) -> pd.DataFrame:
    """All macro snapshots (newest last) for a regime-history strip chart."""
    with get_conn(paths().db_path) as conn:
        rows = conn.execute(
            "SELECT date, regime, vix, fii_flow_cr, usdinr "
            "FROM macro_snapshot ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["date", "regime", "vix", "fii_flow_cr", "usdinr"])
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Portfolio snapshots / equity
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60)
def load_portfolio_snapshots(
    start: str | None = None, end: str | None = None
) -> pd.DataFrame:
    """DataFrame of portfolio_snapshots in [start, end] inclusive (date strings)."""
    query = "SELECT date, cash, equity, drawdown_pct FROM portfolio_snapshots"
    args: list[Any] = []
    where: list[str] = []
    if start:
        where.append("date >= ?")
        args.append(start)
    if end:
        where.append("date <= ?")
        args.append(end)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY date"
    with get_conn(paths().db_path) as conn:
        rows = conn.execute(query, args).fetchall()
    if not rows:
        return pd.DataFrame(columns=["date", "cash", "equity", "drawdown_pct"])
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    return df


def latest_equity_snapshot() -> tuple[float | None, float | None, float | None]:
    """Return (equity, prev_equity, drawdown_pct) for the latest two snapshots.

    All three are ``None`` if no snapshots exist; ``prev_equity`` is ``None``
    when only one snapshot exists.
    """
    df = load_portfolio_snapshots()
    if df.empty:
        return None, None, None
    df = df.sort_values("date")
    equity = float(df["equity"].iloc[-1])
    prev = float(df["equity"].iloc[-2]) if len(df) > 1 else None
    dd = float(df["drawdown_pct"].iloc[-1]) if pd.notna(df["drawdown_pct"].iloc[-1]) else None
    return equity, prev, dd


# ---------------------------------------------------------------------------
# Signals / paper trades / predictions
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60)
def load_signals_by_date(as_of: str) -> pd.DataFrame:
    """Signals whose `ts` starts with `as_of` (YYYY-MM-DD)."""
    with get_conn(paths().db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE ts LIKE ? ORDER BY ts",
            (f"{as_of}%",),
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=60)
def load_paper_trades(*, open_only: bool = False) -> pd.DataFrame:
    """Paper trades joined with signals so callers get symbol/side/conviction.

    Columns include both paper_trade fields and the originating signal's
    symbol/side/conviction/target.
    """
    where = "WHERE pt.ts_exit IS NULL" if open_only else ""
    with get_conn(paths().db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT pt.*,
                   s.symbol, s.side, s.target, s.stop AS signal_stop,
                   s.conviction
              FROM paper_trades pt
              LEFT JOIN signals s ON s.id = pt.signal_id
              {where}
             ORDER BY pt.ts_entry
            """
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=60)
def load_predictions() -> pd.DataFrame:
    """All predictions (matured rows have actual_return_at_horizon non-null)."""
    with get_conn(paths().db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY ts"
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Kite snapshots (holdings / GTTs / quotes)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60)
def load_holdings(as_of: str) -> list[dict[str, Any]]:
    """Read holdings.json for `as_of`. Returns [] when missing/stale (no raise)."""
    try:
        holdings = kite_snapshot.read_holdings(paths(), _date_arg(as_of))
    except (
        kite_snapshot.KiteSnapshotMissingError,
        kite_snapshot.KiteSnapshotStaleError,
    ):
        return []
    return [asdict(h) for h in holdings]


@st.cache_data(ttl=60)
def load_gtts(as_of: str) -> list[dict[str, Any]]:
    """Read gtts.json for `as_of`. Returns [] when missing/stale."""
    try:
        orders = kite_snapshot.read_gtts(paths(), _date_arg(as_of))
    except (
        kite_snapshot.KiteSnapshotMissingError,
        kite_snapshot.KiteSnapshotStaleError,
    ):
        return []
    return [asdict(g) for g in orders]


def load_holdings_typed(as_of: str) -> list[Holding]:
    """Same as load_holdings but returns dataclasses (for portfolio.health/gtt)."""
    try:
        return kite_snapshot.read_holdings(paths(), _date_arg(as_of))
    except (
        kite_snapshot.KiteSnapshotMissingError,
        kite_snapshot.KiteSnapshotStaleError,
    ):
        return []


def load_gtts_typed(as_of: str) -> list[GttOrder]:
    try:
        return kite_snapshot.read_gtts(paths(), _date_arg(as_of))
    except (
        kite_snapshot.KiteSnapshotMissingError,
        kite_snapshot.KiteSnapshotStaleError,
    ):
        return []


@st.cache_data(ttl=60)
def load_quote_snapshot(
    as_of: str, max_age_minutes: int = 30
) -> tuple[dict[str, dict[str, Any]], str] | None:
    """Latest intraday quote snapshot for `as_of`.

    Returns ``({symbol: quote-as-dict}, capture_iso)`` or ``None`` when no
    snapshot exists for the date. Stale snapshots are *returned* (the UI shows
    an amber tag) rather than raising — staleness is enforced upstream by
    the mid_day job before applying MTM.
    """
    try:
        quotes, capture_ts = quotes_snapshot.read_latest_quotes(
            paths(), _date_arg(as_of), max_age_minutes=max_age_minutes
        )
    except quotes_snapshot.QuoteSnapshotMissingError:
        return None
    except quotes_snapshot.QuoteSnapshotStaleError:
        # Stale but present — re-read without the staleness gate so the UI can
        # surface "stale" status visually instead of nothing.
        try:
            quotes, capture_ts = quotes_snapshot.read_latest_quotes(
                paths(), _date_arg(as_of), max_age_minutes=60 * 24 * 365
            )
        except quotes_snapshot.QuoteSnapshotMissingError:
            return None
    by_symbol = {sym: asdict(q) for sym, q in quotes.items()}
    return by_symbol, capture_ts.isoformat()


# ---------------------------------------------------------------------------
# OHLCV (parquet)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300)
def load_ohlcv_enriched(symbol: str, tail: int | None = 180) -> pd.DataFrame:
    """Parquet OHLCV with indicators, optionally trimmed to the last N rows.

    Returns an empty DataFrame when the parquet is missing.
    """
    try:
        df = read_ohlcv(symbol, paths())
    except FileNotFoundError:
        return pd.DataFrame()
    if df.empty:
        return df
    enriched = add_indicators(df)
    if tail is not None and len(enriched) > tail:
        enriched = enriched.tail(tail).copy()
    return enriched


# ---------------------------------------------------------------------------
# Brief markdown
# ---------------------------------------------------------------------------


_KNOWN_BRIEF_FILES = {
    "context": "_context.md",
    "brief": "brief.md",
    "macro": "macro_brief.md",
    "sector": "sector_commentary.md",
    "mid_day": "mid_day_update.md",
    "post_close": "post_close_summary.md",
    "post_close_recap": "post_close_recap.md",
}


@st.cache_data(ttl=60)
def load_brief_section(as_of: str, key: str) -> str | None:
    """Read a known brief markdown file for `as_of`. Returns None when missing."""
    filename = _KNOWN_BRIEF_FILES.get(key, key)
    target = paths().research_dir / as_of / filename
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


@st.cache_data(ttl=60)
def list_candidate_briefs(as_of: str) -> list[str]:
    """List per-candidate brief files (symbol stems) under candidates/."""
    cdir = paths().research_dir / as_of / "candidates"
    if not cdir.is_dir():
        return []
    return sorted(p.stem for p in cdir.glob("*.md"))
