"""Phase 14.A — mid_day MVP orchestrator.

Two-phase invocation:
  prepare → write data/raw/<as_of>/_quote_symbols.txt
  /kite-quotes-snapshot skill (out-of-process) → write quotes_HHMM.json
  apply → read quotes → mtm_open_trades → write mid_day_update.md
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trading.config import Paths, get_paths
from trading.data.kite_snapshot import KiteSnapshotMissingError, read_holdings
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.repo import list_signals_by_date


class MidDayAborted(RuntimeError):  # noqa: N818 — "Aborted" is a state
    """Raised when run_mid_day cannot proceed (analogue of PreOpenAborted)."""


@dataclass(frozen=True)
class MidDayResult:
    as_of: date
    quotes_capture_ts: datetime | None
    bars_built: int
    trades_evaluated: int
    trades_closed: int
    trades_held: int
    trades_skipped: int
    update_path: Path | None
    symbols_path: Path | None
    warnings: list[str] = field(default_factory=list)


def gather_quote_symbols(
    conn: sqlite3.Connection, paths: Paths, as_of: date
) -> list[str]:
    """Sorted, deduped union of: open paper-trade symbols ∪ today's signals
    ∪ holdings.json symbols. Holdings degrade silently if snapshot absent.
    """
    symbols: set[str] = set()
    rows = conn.execute(
        "SELECT DISTINCT s.symbol FROM paper_trades pt "
        "JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL"
    ).fetchall()
    for r in rows:
        symbols.add(r["symbol"])
    for sig in list_signals_by_date(conn, as_of.isoformat()):
        symbols.add(sig.symbol)
    try:
        for h in read_holdings(paths, as_of):
            symbols.add(h.tradingsymbol)
    except KiteSnapshotMissingError:
        pass  # holdings optional
    return sorted(symbols)


def run_mid_day(
    as_of: date,
    *,
    paths: Paths | None = None,
    apply: bool = False,
) -> MidDayResult:
    """Orchestrate mid_day. apply=False → prepare mode (writes symbol file).
    apply=True → reads quotes + mtm + writes markdown (Task 3 wires this).
    """
    p = paths if paths is not None else get_paths()
    warnings: list[str] = []

    with get_conn(p.db_path) as conn:
        run_migrations(conn)

        if not apply:
            symbols = gather_quote_symbols(conn, p, as_of)
            base = p.raw_dir / as_of.isoformat()
            base.mkdir(parents=True, exist_ok=True)
            symbols_path = base / "_quote_symbols.txt"
            symbols_path.write_text(
                "\n".join(symbols) + "\n", encoding="utf-8"
            )
            return MidDayResult(
                as_of=as_of,
                quotes_capture_ts=None,
                bars_built=0,
                trades_evaluated=0,
                trades_closed=0,
                trades_held=0,
                trades_skipped=0,
                update_path=None,
                symbols_path=symbols_path,
                warnings=warnings,
            )

        # apply mode wired in Task 3
        raise NotImplementedError("apply mode wired in Task 3")
