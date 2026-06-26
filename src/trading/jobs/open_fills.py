"""Post-open block — fill paper entries at the live LTP (2026-06-23 design).

Two-phase invocation, mirroring jobs/mid_day.py:
  prepare → read _pending_entries.json → write _quote_symbols.txt
  /kite-quotes-snapshot (out-of-process) → quotes_HHMM.json
  apply → live LTP → re-plan at LTP → open funded trades → open_fills.md

Entry price is the live last-traded price (`Quote.last_price`); qty, stop, and
target are recomputed from it via the unchanged daily-budget planner, so the
paper book reflects a market order placed when the operator acts rather than an
unobtainable previous-close fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import cast

from trading.config import Paths, get_paths
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)
from trading.data.sector import load_sector_map
from trading.ops.logging_setup import configure_logging
from trading.paper.ledger import log_signal_and_open_trade
from trading.paper.pending import (
    PendingEntriesMissingError,
    PendingEntry,
    read_pending_entries,
)
from trading.paper.positions import (
    already_opened_today,
    deployed_by_symbol,
    open_lots_by_symbol,
)
from trading.paper.reconcile import compute_paper_cash
from trading.ranking.ranker import conviction_from_score
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.news_store import negative_news_count_7d
from trading.store.repo import (
    EntryAttribution,
    Signal,
    insert_signal,
    matured_score_outcomes,
)
from trading.strategy.calibration import build_score_calibration
from trading.strategy.daily_budget import BudgetCandidate, plan_daily_entries
from trading.strategy.exits import target_price
from trading.strategy.sizing import Regime


class OpenFillsAborted(RuntimeError):  # noqa: N818 — "Aborted" is a state
    """Raised when apply mode cannot proceed (no fresh quotes)."""


@dataclass(frozen=True)
class OpenFillsResult:
    as_of: date
    symbols_path: Path | None
    update_path: Path | None
    quotes_capture_ts: datetime | None
    trades_opened: int
    trades_skipped: int
    warnings: list[str] = field(default_factory=list)


def run_open_fills(
    as_of: date,
    *,
    paths: Paths | None = None,
    apply: bool = False,
) -> OpenFillsResult:
    """Prepare mode (apply=False) writes the quote-symbols file from the pending
    entries. Apply mode reads the live quotes, re-plans at LTP, opens funded
    trades, and writes the open_fills.md done-marker.
    """
    p = paths if paths is not None else get_paths()
    warnings: list[str] = []

    # No pending file → graceful no-op in both phases.
    try:
        regime, pending = read_pending_entries(p, as_of)
    except PendingEntriesMissingError as e:
        warnings.append(str(e))
        return OpenFillsResult(as_of, None, None, None, 0, 0, warnings)

    if not apply:
        base = p.raw_dir / as_of.isoformat()
        base.mkdir(parents=True, exist_ok=True)
        symbols_path = base / "_quote_symbols.txt"
        symbols = sorted({e.symbol for e in pending})
        symbols_path.write_text("\n".join(symbols) + "\n", encoding="utf-8")
        return OpenFillsResult(as_of, symbols_path, None, None, 0, 0, warnings)

    # apply mode
    try:
        quotes, capture_ts = read_latest_quotes(p, as_of)
    except (QuoteSnapshotMissingError, QuoteSnapshotStaleError) as e:
        raise OpenFillsAborted(str(e)) from e

    sector_map = load_sector_map()
    opened = 0
    skipped = 0
    opened_rows: list[tuple[str, float, int, float]] = []  # sym, ltp, qty, ref_close

    with get_conn(p.db_path) as conn:
        run_migrations(conn)

        budget_cands: list[BudgetCandidate] = []
        meta: dict[str, PendingEntry] = {}
        for pend in pending:
            q = quotes.get(pend.symbol)
            if q is None:
                warnings.append(f"{pend.symbol}: no live quote — skipped")
                skipped += 1
                continue
            ltp = float(q.last_price)
            stop = ltp - 1.5 * pend.atr_14
            if ltp <= stop:
                warnings.append(f"{pend.symbol}: LTP {ltp:.2f} <= stop {stop:.2f} — skipped")
                skipped += 1
                continue
            if already_opened_today(conn, pend.symbol, as_of):
                continue
            budget_cands.append(
                BudgetCandidate(
                    symbol=pend.symbol,
                    entry=ltp,
                    stop=stop,
                    target=target_price(ltp, stop),
                    ml_score=pend.ml_score,
                    sector=sector_map.get(pend.symbol),
                )
            )
            meta[pend.symbol] = pend

        # F-048: feed open-lot counts so the planner caps day-over-day re-entry into
        # a held name and per-sector concentration (the correlated-cluster drawdown).
        lots_by_symbol = open_lots_by_symbol(conn)
        lots_by_sector: dict[str, int] = {}
        for sym, lots in lots_by_symbol.items():
            sec = sector_map.get(sym)
            if sec is not None:
                lots_by_sector[sec] = lots_by_sector.get(sec, 0) + lots

        p_win_cal = build_score_calibration(matured_score_outcomes(conn))
        plan = plan_daily_entries(
            budget_cands,
            available_cash=compute_paper_cash(conn, as_of=as_of),
            deployed_by_symbol=deployed_by_symbol(conn),
            regime=cast(Regime, regime),
            p_win_calibration=p_win_cal,
            open_lots_by_symbol=lots_by_symbol,
            open_lots_by_sector=lots_by_sector,
        )

        planned = {pe.symbol for pe in plan.entries}
        for pe in plan.entries:
            pend = meta[pe.symbol]
            signal = Signal(
                id=None,
                ts=capture_ts.isoformat(),
                symbol=pe.symbol,
                side="LONG",
                entry=pe.entry,
                stop=pe.stop,
                target=pe.target,
                horizon_days=25,
                rules_passed_json="[]",
                ml_score=pend.ml_score,
                conviction=conviction_from_score(pend.ml_score),
                created_by="open_fills",
            )
            log_signal_and_open_trade(
                conn,
                signal=signal,
                entry_ts=capture_ts,
                entry_price=pe.entry,
                qty=pe.qty,
                atr_at_entry=pend.atr_14,
                attribution=EntryAttribution(
                    regime=cast(Regime, regime),
                    sector=sector_map.get(pe.symbol),
                    neg_news_7d=negative_news_count_7d(conn, pe.symbol, as_of),
                ),
            )
            opened += 1
            opened_rows.append((pe.symbol, pe.entry, pe.qty, pend.ref_close))

        # Selected-but-unfunded: log a visibility signal at LTP plus skip reason.
        for symbol, reason in plan.skipped:
            if symbol in planned:
                continue
            pend_skip = meta.get(symbol)
            if pend_skip is not None:
                ltp = float(quotes[symbol].last_price)
                stop = ltp - 1.5 * pend_skip.atr_14
                insert_signal(
                    conn,
                    Signal(
                        id=None,
                        ts=capture_ts.isoformat(),
                        symbol=symbol,
                        side="LONG",
                        entry=ltp,
                        stop=stop,
                        target=target_price(ltp, stop),
                        horizon_days=25,
                        rules_passed_json="[]",
                        ml_score=pend_skip.ml_score,
                        conviction=conviction_from_score(pend_skip.ml_score),
                        created_by="open_fills",
                    ),
                )
            warnings.append(f"{symbol}: not opened — {reason}")
            skipped += 1

    update_dir = p.research_dir / as_of.isoformat()
    update_dir.mkdir(parents=True, exist_ok=True)
    update_path = update_dir / "open_fills.md"
    update_path.write_text(_render_open_fills(capture_ts, opened_rows, warnings), encoding="utf-8")

    return OpenFillsResult(
        as_of=as_of,
        symbols_path=None,
        update_path=update_path,
        quotes_capture_ts=capture_ts,
        trades_opened=opened,
        trades_skipped=skipped,
        warnings=warnings,
    )


def _render_open_fills(
    capture_ts: datetime,
    opened: list[tuple[str, float, int, float]],
    warnings: list[str],
) -> str:
    lines = [
        f"## Open-fills — filled at live LTP, captured {capture_ts.isoformat(timespec='seconds')}",
        "",
        "| symbol | LTP | qty | prev close | drift |",
        "|---|---|---|---|---|",
    ]
    for sym, ltp, qty, ref in opened:
        drift = (ltp - ref) / ref * 100.0 if ref else 0.0
        lines.append(f"| {sym} | {ltp:.2f} | {qty} | {ref:.2f} | {drift:+.2f}% |")
    lines.append("")
    lines.append(f"{len(opened)} trade(s) opened at live LTP.")
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"- {w}")
    return "\n".join(lines) + "\n"


def _main(date_str: str, apply: bool = False) -> None:  # pragma: no cover
    configure_logging("open_fills")
    from loguru import logger

    try:
        result = run_open_fills(date.fromisoformat(date_str), apply=apply)
    except OpenFillsAborted as e:
        print(f"Open-fills aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("open_fills failed")
        raise
    if result.symbols_path:
        print(f"wrote {result.symbols_path}")
        print("Now run /kite-quotes-snapshot skill, then re-run with --apply")
    if result.update_path:
        print(f"wrote {result.update_path} — opened {result.trades_opened}")


if __name__ == "__main__":  # pragma: no cover
    import typer

    typer.run(_main)
