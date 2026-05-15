"""Phase 10 dry-run against real Kite holdings + GTTs (one-shot script).

Reads the JSON snapshot at data/research/_phase10_live_input.json
(captured from the Kite MCP), runs every Phase 10 analyzer, prints
Rich tables and writes a markdown report.

Not wired into the CLI because the CLI command uses the kiteconnect SDK
path; this script lets us validate the algorithms end-to-end on real
data via the interactive MCP route the user actually has open right now.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from rich.console import Console
from rich.table import Table

from trading.config import get_paths
from trading.data.kite import GttOrder, Holding
from trading.features.technicals import add_indicators
from trading.portfolio.gtt import project_all_gtts
from trading.portfolio.health import (
    HoldingContext,
    SentimentSnapshot,
    score_holding,
    technicals_from_history,
)
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.news_store import get_sentiment_daily
from trading.store.ohlcv import read_ohlcv

console = Console()
paths = get_paths()
INPUT = paths.research_dir / "_phase10_live_input.json"


def _to_holding(d: dict) -> Holding:
    return Holding(
        tradingsymbol=d["tradingsymbol"],
        exchange=d["exchange"],
        isin=d.get("isin"),
        quantity=int(d["quantity"]),
        average_price=float(d["average_price"]),
        last_price=float(d["last_price"]),
        close_price=float(d["close_price"]),
        pnl=float(d["pnl"]),
        day_change=float(d["day_change"]),
        day_change_percentage=float(d["day_change_percentage"]),
    )


def _to_gtt(d: dict) -> GttOrder:
    return GttOrder(
        id=int(d["id"]),
        type=d["type"],
        status=d["status"],
        tradingsymbol=d["tradingsymbol"],
        exchange=d["exchange"],
        trigger_values=[float(v) for v in d["trigger_values"]],
        last_price=float(d["last_price"]) if d.get("last_price") is not None else None,
        created_at=d["created_at"],
        orders=[],
    )


def main() -> None:
    payload = json.loads(INPUT.read_text(encoding="utf-8"))
    holdings = [_to_holding(d) for d in payload["holdings"]]
    gtts = [_to_gtt(d) for d in payload["gtts"]]
    console.print(
        f"[bold]Loaded {len(holdings)} holding(s) and {len(gtts)} GTT(s) from "
        f"{INPUT.name}[/bold]"
    )

    # Load + enrich histories
    enriched = {}
    for h in holdings:
        try:
            raw = read_ohlcv(h.tradingsymbol, paths)
            if len(raw) >= 200:
                enriched[h.tradingsymbol] = add_indicators(raw)
        except FileNotFoundError:
            console.print(f"  [yellow]missing parquet: {h.tradingsymbol}[/yellow]")
    console.print(f"  Enriched {len(enriched)}/{len(holdings)} holdings with indicators.")

    # Score health (no fundamentals — Phase 10 ships the skeleton; sentiment pulled if present)
    today_iso = date.today().isoformat()
    health_rows = []
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        for h in holdings:
            sent_row = get_sentiment_daily(conn, today_iso, h.tradingsymbol)
            sent = SentimentSnapshot(
                score_30d=sent_row.score_30d if sent_row else None,
                has_critical=sent_row.has_critical if sent_row else False,
            )
            tech = (
                technicals_from_history(enriched[h.tradingsymbol])
                if h.tradingsymbol in enriched
                else technicals_from_history(None)
            )
            ctx = HoldingContext(
                symbol=h.tradingsymbol,
                qty=h.quantity,
                avg_price=h.average_price,
                last_price=h.last_price,
                technicals=tech,
                sentiment=sent,
            )
            health_rows.append(score_holding(ctx))

    # Health table
    htable = Table(title="Holdings health", show_header=True, header_style="bold")
    htable.add_column("Symbol")
    htable.add_column("Verdict")
    htable.add_column("Score", justify="right")
    htable.add_column("Votes", justify="right")
    htable.add_column("P&L %", justify="right")
    htable.add_column("Top reason")
    color = {"HOLD": "green", "TRIM": "yellow", "EXIT": "red"}
    for r in health_rows:
        c = color[r.verdict]
        pnl = f"{r.pnl_pct:+.1f}%" if r.pnl_pct is not None else "-"
        htable.add_row(
            r.symbol,
            f"[bold {c}]{r.verdict}[/bold {c}]",
            f"{r.score}/100",
            f"{r.net_votes:+d}/{r.votes_cast}",
            pnl,
            r.reasons[0] if r.reasons else "",
        )
    console.print(htable)

    # GTT viability
    viabilities = project_all_gtts(gtts, enriched, horizon_days=60, n_paths=2000, seed=42)
    gtable = Table(title="GTT viability (60d, 2000 paths)", show_header=True, header_style="bold")
    gtable.add_column("Symbol")
    gtable.add_column("Trigger", justify="right")
    gtable.add_column("Last", justify="right")
    gtable.add_column("Target Delta", justify="right")
    gtable.add_column("P(hit)", justify="right")
    gtable.add_column("Exp days", justify="right")
    gtable.add_column("Note")
    for v in viabilities:
        trigger = ", ".join(f"{t:.2f}" for t in v.trigger_values)
        last = f"{v.last_price:.2f}" if v.last_price is not None else "-"
        if v.last_price and v.trigger_values:
            delta = (v.trigger_values[0] - v.last_price) / v.last_price * 100
            delta_str = f"{delta:+.1f}%"
        else:
            delta_str = "-"
        prob = f"{v.probability_hit:.0%}" if v.probability_hit is not None else "-"
        days = f"{v.expected_days_to_hit:.0f}" if v.expected_days_to_hit else "-"
        gtable.add_row(v.symbol, trigger, last, delta_str, prob, days, v.note or "")
    console.print(gtable)

    # Markdown report
    report = paths.research_dir / f"portfolio_live_{datetime.now():%Y%m%d_%H%M%S}.md"
    lines = [
        f"# Live portfolio analysis - {today_iso}",
        "",
        f"Source: real Kite MCP snapshot ({len(holdings)} holdings, {len(gtts)} GTTs).",
        "",
        "## Holdings health",
        "",
        "| Symbol | Verdict | Score | Net votes | P&L % | Reasons |",
        "|---|---|---:|---:|---:|---|",
    ]
    for r in health_rows:
        pnl = f"{r.pnl_pct:+.1f}%" if r.pnl_pct is not None else "—"
        reasons = "; ".join(r.reasons) if r.reasons else "—"
        lines.append(
            f"| {r.symbol} | **{r.verdict}** | {r.score}/100 | "
            f"{r.net_votes:+d} of {r.votes_cast} | {pnl} | {reasons} |"
        )
    lines += [
        "",
        "## GTT viability",
        "",
        "| Symbol | Trigger | Last | Delta | P(hit) | Expected days | Note |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for v in viabilities:
        trigger = ", ".join(f"Rs {t:,.2f}" for t in v.trigger_values)
        last = f"Rs {v.last_price:,.2f}" if v.last_price is not None else "—"
        if v.last_price and v.trigger_values:
            delta = (v.trigger_values[0] - v.last_price) / v.last_price * 100
            delta_str = f"{delta:+.1f}%"
        else:
            delta_str = "—"
        prob = f"{v.probability_hit:.1%}" if v.probability_hit is not None else "—"
        days = f"{v.expected_days_to_hit:.1f}" if v.expected_days_to_hit else "—"
        lines.append(
            f"| {v.symbol} | {trigger} | {last} | {delta_str} | "
            f"{prob} | {days} | {v.note or ''} |"
        )

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"\n[green]Report written to[/green] {report}")


if __name__ == "__main__":
    main()
