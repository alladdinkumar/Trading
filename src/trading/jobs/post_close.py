"""Phase 14.B — post_close MVP orchestrator.

Two-phase invocation (mirrors mid_day):
  prepare → write data/raw/<as_of>/_quote_symbols.txt
  /kite-quotes-snapshot skill (out-of-process) → write quotes_HHMM.json
  apply → read quotes → mtm_open_trades (final stops + TIME exits)
        → reconcile_day (matured predictions + portfolio snapshot)
        → write data/research/<as_of>/post_close_summary.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trading.config import Paths, get_paths
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)
from trading.jobs.mid_day import _quotes_to_bars, gather_quote_symbols
from trading.ops.logging_setup import configure_logging
from trading.paper.mtm import MtmResult, mtm_open_trades
from trading.paper.reconcile import ReconcileResult, reconcile_day
from trading.store.db import get_conn
from trading.store.migrations import run_migrations


class PostCloseAborted(RuntimeError):  # noqa: N818 — "Aborted" is a state
    """Raised when run_post_close cannot proceed (analogue of MidDayAborted)."""


@dataclass(frozen=True)
class PostCloseResult:
    as_of: date
    quotes_capture_ts: datetime | None
    bars_built: int
    trades_evaluated: int
    trades_closed: int
    trades_held: int
    trades_skipped: int
    predictions_matured: int
    equity: float | None
    drawdown_pct: float | None
    summary_path: Path | None
    symbols_path: Path | None
    warnings: list[str] = field(default_factory=list)


def run_post_close(
    as_of: date,
    *,
    paths: Paths | None = None,
    apply: bool = False,
    cash: float = 100_000.0,
) -> PostCloseResult:
    """Orchestrate post_close. apply=False → prepare mode. apply=True → MTM
    + reconcile + write summary."""
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
            return PostCloseResult(
                as_of=as_of,
                quotes_capture_ts=None,
                bars_built=0, trades_evaluated=0, trades_closed=0,
                trades_held=0, trades_skipped=0, predictions_matured=0,
                equity=None, drawdown_pct=None,
                summary_path=None, symbols_path=symbols_path,
                warnings=warnings,
            )

        # apply mode
        try:
            quotes, capture_ts = read_latest_quotes(p, as_of)
        except (QuoteSnapshotMissingError, QuoteSnapshotStaleError) as e:
            raise PostCloseAborted(str(e)) from e

        bars = _quotes_to_bars(quotes)
        mtm_results = mtm_open_trades(conn, bars, as_of=capture_ts)

        closed = sum(1 for r in mtm_results if r.action.startswith("EXIT_"))
        held = sum(1 for r in mtm_results if r.action == "HOLD")
        skipped = sum(1 for r in mtm_results if r.action == "SKIP")

        reconcile_result = reconcile_day(
            conn, as_of=as_of, cash=cash, bars=bars
        )

        summary_dir = p.research_dir / as_of.isoformat()
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_path = summary_dir / "post_close_summary.md"
        summary_path.write_text(
            _render_post_close_summary(
                capture_ts, mtm_results, reconcile_result
            ),
            encoding="utf-8",
        )

        return PostCloseResult(
            as_of=as_of,
            quotes_capture_ts=capture_ts,
            bars_built=len(bars),
            trades_evaluated=len(mtm_results),
            trades_closed=closed,
            trades_held=held,
            trades_skipped=skipped,
            predictions_matured=len(reconcile_result.prediction_updates),
            equity=reconcile_result.snapshot.equity,
            drawdown_pct=reconcile_result.snapshot.drawdown_pct,
            summary_path=summary_path,
            symbols_path=None,
            warnings=warnings,
        )


def _render_post_close_summary(
    capture_ts: datetime,
    mtm_results: list[MtmResult],
    reconcile_result: ReconcileResult,
) -> str:
    closed = [r for r in mtm_results if r.action.startswith("EXIT_")]
    held = [r for r in mtm_results if r.action == "HOLD"]
    skipped = [r for r in mtm_results if r.action == "SKIP"]
    snap = reconcile_result.snapshot
    updates = reconcile_result.prediction_updates

    lines = [
        f"## Post-close summary — captured {capture_ts.isoformat(timespec='seconds')}",
        "",
        f"### Final MTM ({len(mtm_results)} open trades evaluated)",
        "",
        "| symbol | action | exit price | reason | new stop |",
        "|---|---|---|---|---|",
    ]
    for r in mtm_results:
        ep = f"{r.exit_price:.2f}" if r.exit_price is not None else "—"
        ns = f"{r.new_stop:.2f}" if r.new_stop is not None else "—"
        lines.append(
            f"| {r.symbol} | {r.action} | {ep} | {r.reason or '—'} | {ns} |"
        )

    open_positions = sum(1 for r in mtm_results if r.action == "HOLD")
    drawdown = (
        f"{snap.drawdown_pct:+.2f}%" if snap.drawdown_pct is not None else "—"
    )
    lines.extend([
        "",
        "### Portfolio snapshot",
        "",
        f"- equity: ₹{snap.equity:,.0f}",
        f"- cash: ₹{snap.cash:,.0f}",
        f"- drawdown from peak: {drawdown}",
        f"- open positions: {open_positions}",
        "",
        f"### Matured predictions ({len(updates)})",
        "",
    ])
    if updates:
        lines.extend([
            "| symbol | predicted % | actual % | error % |",
            "|---|---|---|---|",
        ])
        for u in updates:
            lines.append(
                f"| {u.symbol} | {u.predicted_return_pct:+.2f} | "
                f"{u.actual_return_pct:+.2f} | {u.error_pct:+.2f} |"
            )
    else:
        lines.append("_(none today)_")

    lines.extend([
        "",
        f"{len(closed)} closed (EXIT_STOP/TARGET/TIME); "
        f"{len(held)} held; "
        f"{len(skipped)} skipped (no quote).",
    ])
    return "\n".join(lines) + "\n"


def _main(
    date_str: str,
    apply: bool = False,
    cash: float = 100_000.0,
) -> None:
    """`python -m trading.jobs.post_close <YYYY-MM-DD> [--apply] [--cash N]` entry."""
    configure_logging("post_close")
    from loguru import logger

    try:
        result = run_post_close(
            date.fromisoformat(date_str), apply=apply, cash=cash
        )
    except PostCloseAborted as e:
        print(f"Post-close aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("post_close failed")
        raise
    if result.symbols_path:
        print(f"wrote {result.symbols_path}")
        print("Now run /kite-quotes-snapshot skill, then re-run with --apply")
    if result.summary_path:
        print(f"wrote {result.summary_path}")
        print(
            f"trades evaluated={result.trades_evaluated} "
            f"closed={result.trades_closed} held={result.trades_held} "
            f"predictions_matured={result.predictions_matured} "
            f"equity={result.equity}"
        )


if __name__ == "__main__":  # pragma: no cover
    import typer
    typer.run(_main)
