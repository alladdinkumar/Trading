"""Phase 18 — weekly_train job: Sunday retrain + weekly performance review.

Spec: docs/superpowers/specs/2026-06-11-phase-18-support-tooling-design.md
Runs unattended via Task Scheduler every Sunday 10:00 IST. The retrain
step is graceful (guarded, InsufficientDataError continues); the review
markdown is always written.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

from trading.backtest.metrics import (
    avg_r_multiple,
    expectancy,
    hit_rate,
    profit_factor,
    sharpe,
)
from trading.config import Paths, get_paths
from trading.ops.notify import notify
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.model_registry import (
    RegistryRow,
    has_row_for_train_end,
    register,
    save_model,
)
from trading.strategy.ranker_features import FEATURE_NAMES
from trading.strategy.ranker_io import load_training_inputs
from trading.strategy.ranker_train import InsufficientDataError, train_walkforward

_IST = timezone(timedelta(hours=5, minutes=30))

TRAIN_WINDOW_YEARS = 3
REVIEW_WINDOW_DAYS = 7

_NO_DATA = "_(no data)_"


# ---------------------------------------------------------------------------
# Result / data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosedTrade:
    """TradeLike adapter over one paper_trades ⋈ signals row."""

    symbol: str
    ts_entry: str
    ts_exit: str
    entry_price: float
    exit_price: float
    qty: int
    exit_reason: str
    days_held: int
    net_pnl: float
    gross_pnl: float
    initial_stop: float
    pnl_pct: float


@dataclass(frozen=True)
class OpenTradeRow:
    symbol: str
    ts_entry: str
    entry_price: float
    qty: int
    current_stop: float | None


@dataclass(frozen=True)
class CalibrationBucket:
    predicted_pct: float
    n: int
    actual_mean_pct: float | None
    realized_hit_rate: float


@dataclass(frozen=True)
class ReviewData:
    week_start: date
    as_of: date
    closed_week: list[ClosedTrade]
    closed_all: list[ClosedTrade]
    open_trades: list[OpenTradeRow]
    equity: pd.Series
    calibration: list[CalibrationBucket]


@dataclass(frozen=True)
class RetrainOutcome:
    ran: bool
    skip_reason: str | None
    examples: int | None
    oos_sharpe: float | None
    promoted: bool
    model_path: str | None


@dataclass(frozen=True)
class WeeklyTrainResult:
    as_of: date
    window_start: date
    window_end: date
    retrain_ran: bool
    retrain_skip_reason: str | None
    examples: int | None
    oos_sharpe: float | None
    promoted: bool
    model_path: str | None
    review_path: Path


# ---------------------------------------------------------------------------
# Review — gathering
# ---------------------------------------------------------------------------

_CLOSED_SQL = (
    "SELECT s.symbol, pt.ts_entry, pt.ts_exit, pt.entry_price, pt.exit_price, "
    "       pt.qty, pt.exit_reason, pt.days_held, pt.pnl, pt.pnl_pct, s.stop "
    "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
    "WHERE pt.ts_exit IS NOT NULL{extra} "
    "ORDER BY pt.ts_exit"
)


def _row_to_closed(r: sqlite3.Row) -> ClosedTrade:
    return ClosedTrade(
        symbol=r["symbol"],
        ts_entry=r["ts_entry"],
        ts_exit=r["ts_exit"],
        entry_price=r["entry_price"],
        exit_price=r["exit_price"],
        qty=r["qty"],
        exit_reason=r["exit_reason"] or "",
        days_held=r["days_held"] or 0,
        net_pnl=r["pnl"] or 0.0,
        gross_pnl=r["pnl"] or 0.0,
        initial_stop=r["stop"],
        pnl_pct=r["pnl_pct"] or 0.0,
    )


def gather_review_data(conn: sqlite3.Connection, as_of: date) -> ReviewData:
    """Pull the week's + cumulative ledger state from SQLite. Pure read."""
    week_start = as_of - timedelta(days=REVIEW_WINDOW_DAYS)
    closed_week = [
        _row_to_closed(r)
        for r in conn.execute(
            _CLOSED_SQL.format(
                extra=" AND substr(pt.ts_exit, 1, 10) > ? AND substr(pt.ts_exit, 1, 10) <= ?"
            ),
            (week_start.isoformat(), as_of.isoformat()),
        )
    ]
    closed_all = [_row_to_closed(r) for r in conn.execute(_CLOSED_SQL.format(extra=""))]
    open_trades = [
        OpenTradeRow(
            symbol=r["symbol"],
            ts_entry=r["ts_entry"],
            entry_price=r["entry_price"],
            qty=r["qty"],
            current_stop=r["current_stop"],
        )
        for r in conn.execute(
            "SELECT s.symbol, pt.ts_entry, pt.entry_price, pt.qty, pt.current_stop "
            "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
            "WHERE pt.ts_exit IS NULL ORDER BY pt.ts_entry"
        )
    ]
    eq_rows = conn.execute("SELECT date, equity FROM portfolio_snapshots ORDER BY date").fetchall()
    equity = pd.Series(
        [r["equity"] for r in eq_rows],
        index=pd.to_datetime([r["date"] for r in eq_rows]),
        dtype=float,
    )
    calibration = [
        CalibrationBucket(
            predicted_pct=r["predicted_return_pct"],
            n=r["n"],
            actual_mean_pct=r["avg_actual"],
            realized_hit_rate=r["hit"],
        )
        for r in conn.execute(
            "SELECT predicted_return_pct, COUNT(*) AS n, "
            "       AVG(actual_return_at_horizon) AS avg_actual, "
            "       AVG(CASE WHEN actual_return_at_horizon > 0 THEN 1.0 ELSE 0.0 END) AS hit "
            "FROM predictions WHERE evaluated_at IS NOT NULL "
            "GROUP BY predicted_return_pct ORDER BY predicted_return_pct"
        )
    ]
    return ReviewData(
        week_start=week_start,
        as_of=as_of,
        closed_week=closed_week,
        closed_all=closed_all,
        open_trades=open_trades,
        equity=equity,
        calibration=calibration,
    )


# ---------------------------------------------------------------------------
# Review — rendering
# ---------------------------------------------------------------------------


def _fmt_pf(v: float) -> str:
    return "∞" if math.isinf(v) else f"{v:.2f}"


def _stats_row(label: str, trades: list[ClosedTrade]) -> str:
    if not trades:
        return f"| {label} | 0 | — | — | — | — |"
    return (
        f"| {label} | {len(trades)} | {hit_rate(trades):.0%} "
        f"| {_fmt_pf(profit_factor(trades))} | ₹{expectancy(trades):,.0f} "
        f"| {avg_r_multiple(trades):.2f} |"
    )


def render_weekly_review(data: ReviewData, retrain: RetrainOutcome) -> str:
    """Pure markdown renderer. Empty sources render `_(no data)_`."""
    lines: list[str] = [
        f"# Weekly review — {data.as_of.isoformat()}",
        "",
        f"_Window: {data.week_start.isoformat()} → {data.as_of.isoformat()}_",
        "",
        "## Week's closed trades",
        "",
    ]
    if data.closed_week:
        lines += [
            "| Symbol | Entry | Exit | P&L | P&L % | Reason | Days |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {t.symbol} | {t.entry_price:.2f} | {t.exit_price:.2f} "
            f"| ₹{t.net_pnl:,.0f} | {t.pnl_pct:+.1f}% | {t.exit_reason} | {t.days_held} |"
            for t in data.closed_week
        ]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Stats — week vs cumulative", ""]
    if data.closed_all:
        lines += [
            "| Scope | Trades | Hit rate | Profit factor | Expectancy | Avg R |",
            "|---|---|---|---|---|---|",
            _stats_row("Week", data.closed_week),
            _stats_row("All time", data.closed_all),
        ]
        if len(data.equity) >= 3:
            eq_sharpe = sharpe(data.equity.pct_change().dropna())
            lines += ["", f"Cumulative Sharpe (portfolio snapshots): {eq_sharpe:.2f}"]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Open positions", ""]
    if data.open_trades:
        lines += [
            "| Symbol | Entered | Entry | Qty | Current stop |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {t.symbol} | {t.ts_entry[:10]} | {t.entry_price:.2f} | {t.qty} "
            f"| {t.current_stop if t.current_stop is not None else '—'} |"
            for t in data.open_trades
        ]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Prediction calibration", ""]
    if data.calibration:
        lines += [
            "| Predicted % | N | Mean actual % | Realized hit rate |",
            "|---|---|---|---|",
        ]
        for b in data.calibration:
            actual = f"{b.actual_mean_pct:+.2f}" if b.actual_mean_pct is not None else "—"
            lines.append(
                f"| {b.predicted_pct:+.1f} | {b.n} | {actual} | {b.realized_hit_rate:.0%} |"
            )
        lines += ["", "_Scatter plot: dashboard → Paper Journal → calibration._"]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Retrain outcome", ""]
    if retrain.ran:
        sharpe_txt = (
            f"{retrain.oos_sharpe:.3f}"
            if retrain.oos_sharpe is not None and not math.isnan(retrain.oos_sharpe)
            else "n/a"
        )
        lines += [
            f"- Trained: yes — {retrain.examples} examples, OOS Sharpe {sharpe_txt}",
            f"- Promoted: {'yes' if retrain.promoted else 'no (deadband held)'}",
            f"- Model: `{retrain.model_path}`",
        ]
    else:
        lines.append(f"- Trained: no — {retrain.skip_reason}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Retrain step
# ---------------------------------------------------------------------------


def _step_retrain(
    paths: Paths,
    conn: sqlite3.Connection,
    window_start: date,
    window_end: date,
    skip_train: bool,
) -> RetrainOutcome:
    """Rolling 3y retrain via Phase 16 train_walkforward. Graceful — never
    blocks the review. The registry guard makes Sunday re-runs idempotent."""
    if skip_train:
        return RetrainOutcome(False, "skip_train requested", None, None, False, None)
    end_iso = window_end.isoformat()
    if has_row_for_train_end(paths, end_iso):
        return RetrainOutcome(
            False, f"registry already has a run for {end_iso}", None, None, False, None
        )
    inputs = load_training_inputs(paths, conn)
    if not inputs.enriched:
        return RetrainOutcome(False, "no parquet symbols with 200+ bars", None, None, False, None)
    try:
        result = train_walkforward(
            enriched=inputs.enriched,
            macro_history=inputs.macro_history,
            sentiment_lookup=inputs.sentiment_lookup,
            negative_news_lookup={},
            start=pd.Timestamp(window_start),
            end=pd.Timestamp(window_end),
        )
    except InsufficientDataError as e:
        logger.warning(f"weekly retrain skipped: {e}")
        return RetrainOutcome(False, str(e), None, None, False, None)

    pkl_rel = f"models/ranker_{end_iso}.pkl"
    save_model(paths.project_root / pkl_rel, result.final_model, FEATURE_NAMES)
    row = RegistryRow(
        version=end_iso,
        trained_at=datetime.now(UTC).isoformat(),
        train_start=str(result.final_train_start.date()),
        train_end=end_iso,
        oos_sharpe=result.oos_sharpe_mean,
        oos_hit_rate=result.oos_hit_rate_mean,
        n_train_examples=result.n_final_examples,
        n_features=len(FEATURE_NAMES),
        path=pkl_rel,
        active=False,
        notes="weekly_train",
    )
    promoted = register(paths, row=row, promote=True)
    return RetrainOutcome(
        True, None, result.n_final_examples, result.oos_sharpe_mean, promoted, pkl_rel
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _today_ist() -> date:
    return datetime.now(_IST).date()


def _slack_body(data: ReviewData, retrain: RetrainOutcome) -> str:
    week_pnl = sum(t.net_pnl for t in data.closed_week)
    lines = [f"Week: {len(data.closed_week)} closed, P&L ₹{week_pnl:,.0f}"]
    if data.closed_all:
        lines.append(
            f"All time: hit {hit_rate(data.closed_all):.0%}, "
            f"PF {_fmt_pf(profit_factor(data.closed_all))}, "
            f"expectancy ₹{expectancy(data.closed_all):,.0f}"
        )
    lines.append(f"Open positions: {len(data.open_trades)}")
    if retrain.ran:
        lines.append(
            f"Retrain: {retrain.examples} examples, "
            f"{'PROMOTED' if retrain.promoted else 'not promoted'}"
        )
    else:
        lines.append(f"Retrain: skipped — {retrain.skip_reason}")
    return "\n".join(lines)


def run_weekly_train(
    as_of: date | None = None,
    *,
    paths: Paths | None = None,
    skip_train: bool = False,
) -> WeeklyTrainResult:
    """Sunday job: rolling 3y retrain (graceful) + weekly review markdown
    + Slack summary. The review is always written, even when the retrain
    is skipped or fails on insufficient data."""
    p = paths if paths is not None else get_paths()
    d = as_of or _today_ist()
    window_start = (pd.Timestamp(d) - pd.DateOffset(years=TRAIN_WINDOW_YEARS)).date()

    with get_conn(p.db_path) as conn:
        run_migrations(conn)
        retrain = _step_retrain(p, conn, window_start, d, skip_train)
        data = gather_review_data(conn, d)

    review_dir = p.research_dir / "weekly"
    review_dir.mkdir(parents=True, exist_ok=True)
    review_path = review_dir / f"{d.isoformat()}_review.md"
    review_path.write_text(render_weekly_review(data, retrain), encoding="utf-8")

    notify("info", f"📊 Weekly review {d.isoformat()}", _slack_body(data, retrain))

    return WeeklyTrainResult(
        as_of=d,
        window_start=window_start,
        window_end=d,
        retrain_ran=retrain.ran,
        retrain_skip_reason=retrain.skip_reason,
        examples=retrain.examples,
        oos_sharpe=retrain.oos_sharpe,
        promoted=retrain.promoted,
        model_path=retrain.model_path,
        review_path=review_path,
    )


def _main(date_str: str = "", skip_train: bool = False) -> None:
    """`python -m trading.jobs.weekly_train [YYYY-MM-DD]` entry."""
    from trading.ops.logging_setup import configure_logging

    configure_logging("weekly_train")
    try:
        result = run_weekly_train(
            date.fromisoformat(date_str) if date_str else None,
            skip_train=skip_train,
        )
    except Exception:
        logger.exception("weekly_train failed")
        raise
    print(f"wrote {result.review_path}")


if __name__ == "__main__":  # pragma: no cover
    import typer

    typer.run(_main)
