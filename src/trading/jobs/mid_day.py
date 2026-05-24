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
from trading.data.kite import Quote
from trading.data.kite_snapshot import KiteSnapshotMissingError, read_holdings
from trading.ops.logging_setup import configure_logging
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)
from trading.paper.mtm import MtmResult, mtm_open_trades
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.repo import list_signals_by_date
from trading.strategy.exits import Bar


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

        # apply mode
        try:
            quotes, capture_ts = read_latest_quotes(p, as_of)
        except (QuoteSnapshotMissingError, QuoteSnapshotStaleError) as e:
            raise MidDayAborted(str(e)) from e

        bars = _quotes_to_bars(quotes)
        mtm_results = mtm_open_trades(conn, bars, as_of=capture_ts)

        closed = sum(1 for r in mtm_results if r.action.startswith("EXIT_"))
        held = sum(1 for r in mtm_results if r.action == "HOLD")
        skipped = sum(1 for r in mtm_results if r.action == "SKIP")

        update_dir = p.research_dir / as_of.isoformat()
        update_dir.mkdir(parents=True, exist_ok=True)
        update_path = update_dir / "mid_day_update.md"
        update_path.write_text(
            _render_mid_day_update(capture_ts, mtm_results),
            encoding="utf-8",
        )

        return MidDayResult(
            as_of=as_of,
            quotes_capture_ts=capture_ts,
            bars_built=len(bars),
            trades_evaluated=len(mtm_results),
            trades_closed=closed,
            trades_held=held,
            trades_skipped=skipped,
            update_path=update_path,
            symbols_path=None,
            warnings=warnings,
        )


def _quotes_to_bars(quotes: dict[str, Quote]) -> dict[str, Bar]:
    """Translate Quote → Bar. close = last_price (current LTP), NOT
    quote.close (which is yesterday's close in Kite's convention).
    """
    return {
        sym: Bar(
            open=q.open, high=q.high, low=q.low, close=q.last_price,
        )
        for sym, q in quotes.items()
    }


def _render_mid_day_update(
    capture_ts: datetime, results: list[MtmResult]
) -> str:
    closed = [r for r in results if r.action.startswith("EXIT_")]
    held = [r for r in results if r.action == "HOLD"]
    skipped = [r for r in results if r.action == "SKIP"]

    lines = [
        f"## Mid-day update — captured {capture_ts.isoformat(timespec='seconds')}",
        "",
        "| symbol | action | exit price | reason | new stop |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        ep = f"{r.exit_price:.2f}" if r.exit_price is not None else "—"
        ns = f"{r.new_stop:.2f}" if r.new_stop is not None else "—"
        lines.append(
            f"| {r.symbol} | {r.action} | {ep} | {r.reason or '—'} | {ns} |"
        )
    lines.append("")
    lines.append(
        f"{len(results)} open trades evaluated; "
        f"{len(closed)} closed (EXIT_STOP/TARGET/TIME); "
        f"{len(held)} held; "
        f"{len(skipped)} skipped (no quote)."
    )
    return "\n".join(lines) + "\n"


def _main(
    date_str: str,
    apply: bool = False,
) -> None:
    """`python -m trading.jobs.mid_day <YYYY-MM-DD> [--apply]` entry."""
    configure_logging("mid_day")
    from loguru import logger

    try:
        result = run_mid_day(date.fromisoformat(date_str), apply=apply)
    except MidDayAborted as e:
        print(f"Mid-day aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("mid_day failed")
        raise
    if result.symbols_path:
        print(f"wrote {result.symbols_path}")
        print("Now run /kite-quotes-snapshot skill, then re-run with --apply")
    if result.update_path:
        print(f"wrote {result.update_path}")
        print(
            f"trades evaluated={result.trades_evaluated} "
            f"closed={result.trades_closed} held={result.trades_held} "
            f"skipped={result.trades_skipped}"
        )


if __name__ == "__main__":  # pragma: no cover
    import typer
    typer.run(_main)
