"""Phase 12 input-bundle renderer.

Reads DB-resident state (macro_snapshot, sentiment_daily, news_items,
paper_trades, predictions) directly via `conn`. Ephemeral upstream outputs
(scanner candidates, portfolio health) arrive via the typed `ContextInputs`
dataclass — Phase 13's pre_open job is responsible for computing them once
and passing them in. Pure renderer: no scanner / portfolio invocation here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from trading.config import Paths
from trading.portfolio.health import HealthScore
from trading.strategy.rules import Candidate

Mode = Literal["pre_open", "post_close"]


@dataclass(frozen=True)
class ContextInputs:
    """Caller-supplied ephemeral inputs that don't live in any DB table."""

    candidates: list[Candidate] = field(default_factory=list)
    holdings_health: list[HealthScore] = field(default_factory=list)


def assemble_context(
    *,
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    mode: Mode,
    inputs: ContextInputs,
) -> Path:
    """Render `_context.md` for `as_of` and return the written path.

    Sections rendered (in order): header, macro, candidates, holdings health,
    open paper-trades, (post_close only) matured predictions. Empty sections
    render as `_(no data)_` so the skill can flag the gap explicitly.
    """
    date_dir = paths.research_dir / as_of.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    out_path = date_dir / "_context.md"

    parts: list[str] = []
    parts.append(_render_header(as_of, mode))
    parts.append(_render_macro(conn, as_of))
    parts.append(_render_candidates(conn, as_of, inputs.candidates))
    parts.append(_render_holdings_health(inputs.holdings_health))
    parts.append(_render_open_trades(conn))
    if mode == "post_close":
        parts.append(_render_matured_predictions(conn, as_of))

    out_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return out_path


def _render_header(as_of: date, mode: Mode) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    return (
        f"# Trading context bundle — {as_of.isoformat()}  (mode: {mode})\n"
        f"\n"
        f"_Assembled at {ts}._"
    )


def _render_macro(conn: sqlite3.Connection, as_of: date) -> str:
    row = conn.execute(
        "SELECT vix, usdinr, fii_flow_cr, dii_flow_cr, regime "
        "FROM macro_snapshot WHERE date = ?",
        (as_of.isoformat(),),
    ).fetchone()
    if row is None:
        return "## Macro snapshot\n\n_(no data)_"
    lines = ["## Macro snapshot", "", "| field | value |", "|---|---|"]
    if row["vix"] is not None:
        lines.append(f"| VIX | {row['vix']:.2f} |")
    if row["usdinr"] is not None:
        lines.append(f"| USDINR | {row['usdinr']:.2f} |")
    if row["fii_flow_cr"] is not None:
        lines.append(f"| FII flow (₹ cr) | {row['fii_flow_cr']:+.0f} |")
    if row["dii_flow_cr"] is not None:
        lines.append(f"| DII flow (₹ cr) | {row['dii_flow_cr']:+.0f} |")
    if row["regime"] is not None:
        lines.append(f"| Regime | {row['regime']} |")
    return "\n".join(lines)


def _render_candidates(
    conn: sqlite3.Connection, as_of: date, candidates: list[Candidate]
) -> str:
    if not candidates:
        return "## Today's candidates\n\n_(no data)_"

    sorted_cands = sorted(
        candidates,
        key=lambda c: (-sum(1 for r in c.rules if r.passed), c.symbol),
    )
    blocks: list[str] = ["## Today's candidates"]
    for c in sorted_cands[:5]:
        n_passed = sum(1 for r in c.rules if r.passed)
        n_total = len(c.rules)
        blocks.append("")
        blocks.append(f"### {c.symbol} — passes {n_passed}/{n_total} rules")
        blocks.append(
            f"- close {c.close:.2f}, RSI {c.rsi_14:.1f}, ATR(14) {c.atr_14:.2f}"
        )
        blocks.append(
            f"- SMA20 {c.sma_20:.2f} · SMA50 {c.sma_50:.2f} · SMA200 {c.sma_200:.2f}"
        )
        blocks.extend(_render_news_for_symbol(conn, c.symbol, as_of))
    return "\n".join(blocks)


def _render_news_for_symbol(
    conn: sqlite3.Connection, symbol: str, as_of: date
) -> list[str]:
    """Last 7 days of headlines + sentiment_daily summary + critical flag."""
    cutoff = (as_of - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT ts, headline, sentiment, category, is_critical "
        "FROM news_items WHERE symbol = ? AND ts >= ? "
        "ORDER BY ts DESC LIMIT 5",
        (symbol, cutoff),
    ).fetchall()
    sd = conn.execute(
        "SELECT score_7d, news_count, negative_news_count, has_critical "
        "FROM sentiment_daily WHERE date = ? AND symbol = ?",
        (as_of.isoformat(), symbol),
    ).fetchone()
    out: list[str] = []
    if sd is not None:
        score_7d = sd["score_7d"]
        score_str = f"{score_7d:+.2f}" if score_7d is not None else "—"
        out.append(
            f"- sentiment 7d {score_str} · "
            f"{sd['news_count']} headlines ({sd['negative_news_count']} negative)"
        )
        out.append(
            f"- Critical news flag: {'YES' if sd['has_critical'] else 'NO'}"
        )
    else:
        out.append("- sentiment: _(no daily aggregate)_")
        out.append("- Critical news flag: NO")
    if rows:
        out.append("- Recent headlines:")
        for r in rows:
            score = f"{r['sentiment']:+.2f}" if r["sentiment"] is not None else "—"
            cat = r["category"] or "—"
            out.append(f"  - {r['ts'][:10]} · [{cat}] {r['headline']} ({score})")
    return out


def _render_holdings_health(rows: list[HealthScore]) -> str:
    if not rows:
        return "## Holdings health\n\n_(no data)_"
    sorted_rows = sorted(rows, key=lambda h: (h.score, h.symbol))
    blocks: list[str] = ["## Holdings health"]
    for h in sorted_rows[:10]:
        blocks.append("")
        blocks.append(
            f"### {h.symbol} — verdict {h.verdict} (score {h.score}/100, "
            f"net votes {h.net_votes:+d}/{h.votes_cast})"
        )
        if h.pnl_pct is not None:
            blocks.append(f"- unrealised P&L: {h.pnl_pct:+.2f}%")
        for reason in h.reasons:
            blocks.append(f"- {reason}")
    return "\n".join(blocks)


def _render_open_trades(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT pt.id, s.symbol, pt.entry_price, pt.qty, pt.ts_entry, "
        "pt.current_stop "
        "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL ORDER BY pt.ts_entry"
    ).fetchall()
    if not rows:
        return "## Open paper-trades\n\n_(no data)_"
    out = [
        "## Open paper-trades",
        "",
        "| symbol | entry | qty | entered | trailing stop |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        stop = f"{r['current_stop']:.2f}" if r["current_stop"] is not None else "—"
        out.append(
            f"| {r['symbol']} | {r['entry_price']:.2f} | {r['qty']} | "
            f"{r['ts_entry'][:10]} | {stop} |"
        )
    return "\n".join(out)


def _render_matured_predictions(conn: sqlite3.Connection, as_of: date) -> str:
    rows = conn.execute(
        "SELECT symbol, predicted_return_pct, actual_return_at_horizon, "
        "error_pct, predicted_horizon_days "
        "FROM predictions "
        "WHERE evaluated_at IS NOT NULL AND substr(evaluated_at, 1, 10) = ? "
        "ORDER BY symbol",
        (as_of.isoformat(),),
    ).fetchall()
    if not rows:
        return "## Matured predictions\n\n_(no data)_"
    out = [
        "## Matured predictions",
        "",
        "| symbol | predicted % | actual % | error % | horizon (d) |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['symbol']} | {r['predicted_return_pct']:.2f} | "
            f"{r['actual_return_at_horizon']:.2f} | "
            f"{r['error_pct']:+.2f} | {r['predicted_horizon_days']} |"
        )
    return "\n".join(out)
