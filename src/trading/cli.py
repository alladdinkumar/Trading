"""Typer CLI entry — `uv run trading <command>`."""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from trading.backtest import (
    BacktestConfig,
    BacktestResult,
    MetricsBundle,
    compute_metrics,
    run_backtest,
)
from trading.config import get_paths, get_settings, update_env_var
from trading.data.kite import (
    KiteAuthError,
    generate_session,
    get_gtts,
    get_holdings,
    is_authenticated,
    login_url,
    make_client,
)
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
)
from trading.data.kite_snapshot import (
    read_gtts as _snapshot_read_gtts,
)
from trading.data.kite_snapshot import (
    read_holdings as _snapshot_read_holdings,
)
from trading.data.macro import snapshot_and_classify
from trading.data.news import default_aliases, fetch_all_news
from trading.data.ohlcv_refresh import refresh_ohlcv
from trading.data.sector import fetch_all_sectors
from trading.data.universe import load_universe
from trading.data.yfinance import OhlcvFetchError, fetch_ohlcv
from trading.features.sentiment import aggregate_daily, score_news_items
from trading.features.technicals import add_indicators
from trading.jobs.mid_day import MidDayAborted, run_mid_day
from trading.jobs.post_close import PostCloseAborted, run_post_close
from trading.jobs.pre_open import PreOpenAborted, build_scan_context, run_pre_open
from trading.jobs.pre_open_iep import PreOpenIepAborted, run_pre_open_iep
from trading.llm.briefing import (
    MissingNarrativeError,
    StaleBundleError,
    compile_brief,
)
from trading.llm.context import ContextInputs
from trading.llm.context import assemble_context as _assemble_context
from trading.ops.notify import notify as _notify
from trading.ops.retention import DEFAULT_NEWS_KEEP_DAYS, DEFAULT_RAW_KEEP_DAYS
from trading.ops.run_status import StepStatus, compute_status, has_due_failure
from trading.ops.runner import SCHEDULE, _today_ist, fire_reminder
from trading.paper.ledger import log_signal_and_open_trade, open_trades
from trading.paper.mtm import build_bars_from_history, mtm_open_trades
from trading.paper.reconcile import INITIAL_CAPITAL, reconcile_day
from trading.portfolio.gtt import project_all_gtts
from trading.portfolio.health import (
    HoldingContext,
    SentimentSnapshot,
    score_holding,
    technicals_from_history,
)
from trading.store.db import get_conn
from trading.store.macro_store import upsert_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.news_store import get_sentiment_daily, insert_news_items
from trading.store.ohlcv import list_symbols, parquet_path, read_ohlcv, write_ohlcv
from trading.store.repo import Signal, get_signal
from trading.store.sector_store import upsert_sector_daily
from trading.strategy.rules import passing, scan

app = typer.Typer(help="Trading — research and paper-trading CLI.", add_completion=False)
console = Console()


def _force_utf8_io(*streams: object) -> None:
    """Reconfigure stdout/stderr (or the given streams) to UTF-8.

    Windows Task Scheduler runs tasks under the system console code page
    (cp1252), where printing characters the CLI routinely emits — `→` in the
    weekly-train table, `₹` in paper-status, the `🔔/⚠️/❌` notify emoji — raises
    `UnicodeEncodeError` and crashes the scheduled task (which then also posts
    the traceback to Slack via the error sink). Forcing UTF-8 here makes every
    `uv run trading …` invocation encoding-safe regardless of the console code
    page, so we no longer depend on each Task Scheduler XML remembering to set
    `PYTHONUTF8=1`. `errors="replace"` is a last-resort guard. Streams without
    `reconfigure` (capture buffers, plain `StringIO`) are skipped.
    """
    targets = streams or (sys.stdout, sys.stderr)
    for stream in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


@app.callback()
def _main() -> None:
    """Trading — research and paper-trading CLI.

    Empty callback so Typer treats `trading` as a multi-command app (otherwise
    a single-command app flattens and rejects the subcommand name).
    Calling `get_settings()` here also loads `.env` once per CLI invocation
    so subcommands that read env vars directly (e.g. `notify-test` reading
    `SLACK_WEBHOOK_URL` inside `post_slack`) see the configured values.
    """
    _force_utf8_io()
    get_settings()


@app.command("ingest-history")
def ingest_history(
    start: Annotated[
        str,
        typer.Option(help="Inclusive start date (YYYY-MM-DD)."),
    ] = "2023-01-01",
    end: Annotated[
        str | None,
        typer.Option(help="Exclusive end date (defaults to today)."),
    ] = None,
    symbols: Annotated[
        list[str] | None,
        typer.Option(
            "--symbols",
            "-s",
            help="Ticker(s) to ingest. Repeat the flag for multiple. Defaults to universe.txt.",
        ),
    ] = None,
    skip_existing: Annotated[
        bool,
        typer.Option(
            "--skip-existing",
            help="Skip symbols that already have a parquet file on disk.",
        ),
    ] = False,
) -> None:
    """Fetch historical OHLCV from yfinance and write per-symbol parquet files."""
    paths = get_paths()
    end_str = end or date.today().isoformat()
    tickers = list(symbols) if symbols else load_universe()
    if not tickers:
        console.print("[yellow]No symbols to ingest.[/yellow]")
        raise typer.Exit(code=0)

    console.print(f"[bold]Ingesting {len(tickers)} symbol(s)[/bold] from {start} to {end_str}")

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[str] = []
    total_rows = 0

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    with progress:
        task = progress.add_task("Fetching", total=len(tickers))
        for sym in tickers:
            if skip_existing and parquet_path(sym, paths).is_file():
                skipped.append(sym)
                progress.advance(task)
                continue
            try:
                df = fetch_ohlcv(sym, start, end_str)
                write_ohlcv(df, sym, paths)
                succeeded.append(sym)
                total_rows += len(df)
            except OhlcvFetchError as exc:
                failed.append((sym, str(exc)))
            except Exception as exc:  # broad catch: keep going across the universe
                failed.append((sym, f"{type(exc).__name__}: {exc}"))
            progress.advance(task)

    console.print(
        f"\n[green]{len(succeeded)} succeeded[/green]"
        f"  [yellow]{len(skipped)} skipped[/yellow]"
        f"  [red]{len(failed)} failed[/red]"
        f"  ({total_rows:,} bars written)"
    )
    if failed:
        console.print("\n[red]Failures:[/red]")
        for sym, reason in failed:
            console.print(f"  [red]{sym}[/red]: {reason}")


@app.command("refresh-ohlcv")
def refresh_ohlcv_cmd(
    date_str: Annotated[
        str | None,
        typer.Option("--date", help="As-of date YYYY-MM-DD (defaults to today)."),
    ] = None,
    symbols: Annotated[
        list[str] | None,
        typer.Option(
            "--symbols",
            "-s",
            help="Ticker(s) to refresh. Repeat the flag for multiple. Defaults to universe.txt.",
        ),
    ] = None,
) -> None:
    """Append any OHLCV bars missing up to D-1 for each symbol (F-018)."""
    paths = get_paths()
    as_of = date.fromisoformat(date_str) if date_str else date.today()
    result = refresh_ohlcv(paths, as_of, symbols=list(symbols) if symbols else None)
    console.print(
        f"[green]{result.symbols_refreshed} refreshed[/green]"
        f"  [red]{result.symbols_failed} failed[/red]"
        f"  ({result.bars_added:,} bars added)"
    )
    if result.warnings:
        console.print("\n[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")
    if result.symbols_failed:
        raise typer.Exit(code=1)


@app.command("kite-emergency-login")
def kite_emergency_login(
    request_token: Annotated[
        str | None,
        typer.Option(
            "--request-token",
            "-t",
            help="Skip the interactive prompt and pass the request_token directly.",
        ),
    ] = None,
) -> None:
    """FALLBACK: interactive Kite Connect SDK login.

    Production paths use the /kite-snapshot skill (MCP). Use this command
    only when MCP is broken and you need to populate KITE_ACCESS_TOKEN
    in .env so `kite-emergency-snapshot` can run.
    """
    settings = get_settings()
    if not settings.kite_api_key or not settings.kite_api_secret:
        console.print("[red]KITE_API_KEY and KITE_API_SECRET must be set in .env first.[/red]")
        raise typer.Exit(code=1)

    client = make_client(settings.kite_api_key)
    url = login_url(client)
    console.print(
        f"\n[bold]1. Open this URL in your browser and complete login:[/bold]\n  {url}\n"
        f"[bold]2. After redirect, copy the [cyan]request_token[/cyan] from the URL.[/bold]"
    )
    token_input = request_token or typer.prompt("\nPaste request_token")

    try:
        access_token = generate_session(client, token_input, settings.kite_api_secret)
    except KiteAuthError as exc:
        console.print(f"[red]Kite rejected the request_token: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]Login failed: {type(exc).__name__}: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    env_path = get_paths().project_root / ".env"
    update_env_var(env_path, "KITE_ACCESS_TOKEN", access_token)
    console.print(f"\n[green]Login successful.[/green] Token written to {env_path}")

    if is_authenticated(client):
        try:
            profile = client.profile()
            console.print(f"Logged in as: [bold]{profile.get('user_name', '?')}[/bold]")
        except Exception:  # profile is just informational
            pass


@app.command("kite-emergency-snapshot")
def kite_emergency_snapshot(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD.")],
) -> None:
    """FALLBACK: write data/raw/<date>/{holdings,gtts}.json via SDK when MCP is broken.

    Same on-disk contract as the /kite-snapshot skill, but uses
    src/trading/data/kite.py + KITE_ACCESS_TOKEN from .env. Tags
    _meta.source as "sdk-fallback" so audits can see which path produced
    the file.
    """
    from dataclasses import asdict as _dc_asdict

    settings = get_settings()
    if not (settings.kite_api_key and settings.kite_access_token):
        console.print(
            "[red]Kite credentials missing. Run `trading kite-emergency-login` first.[/red]"
        )
        raise typer.Exit(code=1)

    paths = get_paths()
    as_of = date.fromisoformat(date_str)
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)

    client = make_client(settings.kite_api_key, settings.kite_access_token)
    try:
        holdings_list = get_holdings(client)
        gtts_list = get_gtts(client)
    except KiteAuthError as exc:
        console.print(f"[red]Kite auth failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    def _atomic_dump(rows: list[Any], filename: str) -> None:
        tmp = base / (filename + ".tmp")
        final = base / filename
        tmp.write_text(json.dumps([_dc_asdict(r) for r in rows]), encoding="utf-8")
        tmp.replace(final)

    _atomic_dump(holdings_list, "holdings.json")
    _atomic_dump(gtts_list, "gtts.json")

    meta = {
        "snapshot_at": datetime.now().isoformat(),
        "source": "sdk-fallback",
        "skill_version": "1",
    }
    meta_tmp = base / "_meta.tmp"
    meta_tmp.write_text(json.dumps(meta), encoding="utf-8")
    meta_tmp.replace(base / "_meta.json")

    console.print(
        f"[green]Wrote {len(holdings_list)} holdings + {len(gtts_list)} GTTs "
        f"to {base} (sdk-fallback).[/green]"
    )


@app.command("scan")
def scan_cmd(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Scan date (YYYY-MM-DD). Defaults to the latest bar on disk.",
        ),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option("--show-all", help="Include candidates that failed at least one rule."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of a table."),
    ] = False,
) -> None:
    """Run Layer A rules across the universe and print passing candidates."""
    paths = get_paths()
    if as_of is None:
        # Default: use the latest date present in any parquet
        symbols_on_disk = list_symbols(paths)
        if not symbols_on_disk:
            console.print("[red]No parquet data on disk. Run `trading ingest-history` first.[/red]")
            raise typer.Exit(code=1)
        latest = max(read_ohlcv(s, paths).index.max() for s in symbols_on_disk)
        scan_date = latest.date()
    else:
        scan_date = date.fromisoformat(as_of)

    # Build the same risk-gate context pre_open uses, best-effort: when a macro
    # snapshot / sentiment rollup exists for scan_date the regime + critical
    # vetoes are live; otherwise they degrade to the indicator-only preview.
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        ctx = build_scan_context(conn, scan_date)
    candidates = scan(paths, scan_date, ctx=ctx)
    surfaced = candidates if show_all else passing(candidates)

    if json_output:
        payload = [_candidate_to_dict(c) for c in surfaced]
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    console.print(
        f"\n[bold]Scan {scan_date.isoformat()}[/bold]: "
        f"{len(candidates)} evaluated, {len(passing(candidates))} passed all rules."
    )
    if not surfaced:
        console.print("[yellow]Nothing surfaces.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Symbol")
    table.add_column("Close", justify="right")
    table.add_column("RSI", justify="right")
    table.add_column("vs SMA20%", justify="right")
    table.add_column("vs SMA50%", justify="right")
    table.add_column("ATR", justify="right")
    if show_all:
        table.add_column("Rules passed")
        table.add_column("First failure")

    for c in surfaced:
        sma20_pct = f"{(c.close - c.sma_20) / c.sma_20 * 100:+.1f}" if c.sma_20 == c.sma_20 else "-"
        sma50_pct = f"{(c.close - c.sma_50) / c.sma_50 * 100:+.1f}" if c.sma_50 == c.sma_50 else "-"
        row = [
            c.symbol,
            f"{c.close:.2f}",
            f"{c.rsi_14:.1f}" if c.rsi_14 == c.rsi_14 else "-",
            sma20_pct,
            sma50_pct,
            f"{c.atr_14:.2f}" if c.atr_14 == c.atr_14 else "-",
        ]
        if show_all:
            n_passed = sum(1 for r in c.rules if r.passed)
            row.append(f"{n_passed}/{len(c.rules)}")
            first_fail = next((r for r in c.rules if not r.passed), None)
            row.append(f"{first_fail.name}: {first_fail.reason}" if first_fail else "")
        table.add_row(*row)
    console.print(table)


def _candidate_to_dict(c: Any) -> dict[str, Any]:
    """Compact JSON representation of a Candidate."""
    return {
        "symbol": c.symbol,
        "scan_date": c.scan_date.isoformat(),
        "close": c.close,
        "rsi_14": c.rsi_14,
        "sma_20": c.sma_20,
        "sma_50": c.sma_50,
        "sma_200": c.sma_200,
        "atr_14": c.atr_14,
        "all_passed": c.all_passed,
        "rules": [asdict(r) for r in c.rules],
    }


@app.command("backtest")
def backtest_cmd(
    start: Annotated[
        str,
        typer.Option(help="Inclusive backtest start date (YYYY-MM-DD)."),
    ] = "2023-01-01",
    end: Annotated[
        str | None,
        typer.Option(help="Inclusive end date (defaults to latest bar on disk)."),
    ] = None,
    capital: Annotated[
        float,
        typer.Option(help="Initial capital in ₹."),
    ] = 500_000.0,
    risk_pct: Annotated[
        float,
        typer.Option(help="Per-trade risk as fraction of capital (e.g. 0.02 = 2%)."),
    ] = 0.02,
    report: Annotated[
        str | None,
        typer.Option(help="Path to write the markdown report (default: data/research/...)."),
    ] = None,
) -> None:
    """Run a rules-only backtest over the ingested universe and emit a markdown report."""
    paths = get_paths()
    symbols = list_symbols(paths)
    if not symbols:
        console.print("[red]No parquet data on disk. Run `trading ingest-history` first.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]Loading + enriching {len(symbols)} symbols…[/bold]")
    enriched: dict[str, pd.DataFrame] = {}
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    with progress:
        task = progress.add_task("Enriching", total=len(symbols))
        for sym in symbols:
            try:
                raw = read_ohlcv(sym, paths)
                if len(raw) >= 200:
                    enriched[sym] = add_indicators(raw)
            except FileNotFoundError:
                pass
            progress.advance(task)

    if not enriched:
        console.print("[red]No symbol had ≥200 bars after enrichment.[/red]")
        raise typer.Exit(code=1)

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else max(df.index.max() for df in enriched.values())
    console.print(
        f"[bold]Running backtest:[/bold] {start_ts.date()} to {end_ts.date()} "
        f"on {len(enriched)} symbols (Rs {capital:,.0f} capital, {risk_pct:.0%} risk)"
    )

    config = BacktestConfig(initial_capital=capital, risk_pct=risk_pct)
    result = run_backtest(enriched, config, start_ts, end_ts)
    metrics = compute_metrics(result)

    _print_metrics_table(metrics, result)

    report_path = Path(report) if report else paths.data_dir / "research" / _default_report_name()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report(metrics, result, start_ts, end_ts), encoding="utf-8")
    console.print(f"\n[green]Report written to[/green] {report_path}")


def _default_report_name() -> str:
    return f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"


def _print_metrics_table(metrics: MetricsBundle, result: BacktestResult) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("CAGR", f"{metrics.cagr * 100:.2f}%")
    table.add_row("Sharpe", f"{metrics.sharpe:.2f}")
    table.add_row("Sortino", f"{metrics.sortino:.2f}")
    table.add_row("Max drawdown", f"{metrics.max_drawdown * 100:.2f}%")
    table.add_row("Hit rate", f"{metrics.hit_rate * 100:.1f}%")
    pf = "∞" if metrics.profit_factor == float("inf") else f"{metrics.profit_factor:.2f}"
    table.add_row("Profit factor", pf)
    table.add_row("Expectancy / trade", f"Rs {metrics.expectancy:,.0f}")
    table.add_row("Avg R-multiple", f"{metrics.avg_r_multiple:.2f}")
    table.add_row("Total trades", str(metrics.total_trades))
    table.add_row("Total costs paid", f"Rs {metrics.total_costs:,.0f}")
    table.add_row("Final cash", f"Rs {result.final_cash:,.0f}")
    if result.equity_curve.size:
        table.add_row("Final equity", f"Rs {result.equity_curve.iloc[-1]:,.0f}")
    console.print(table)


def _render_report(
    metrics: MetricsBundle,
    result: BacktestResult,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> str:
    lines: list[str] = []
    lines.append(f"# Backtest report — {start_ts.date()} to {end_ts.date()}")
    lines.append("")
    lines.append(f"Initial capital: ₹{result.config.initial_capital:,.0f}")
    lines.append(f"Risk per trade: {result.config.risk_pct:.1%}")
    lines.append(f"Regime: {result.config.regime}")
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| CAGR | {metrics.cagr * 100:.2f}% |")
    lines.append(f"| Sharpe | {metrics.sharpe:.2f} |")
    lines.append(f"| Sortino | {metrics.sortino:.2f} |")
    lines.append(f"| Max drawdown | {metrics.max_drawdown * 100:.2f}% |")
    lines.append(f"| Hit rate | {metrics.hit_rate * 100:.1f}% |")
    pf = "∞" if metrics.profit_factor == float("inf") else f"{metrics.profit_factor:.2f}"
    lines.append(f"| Profit factor | {pf} |")
    lines.append(f"| Expectancy / trade | ₹{metrics.expectancy:,.0f} |")
    lines.append(f"| Avg R-multiple | {metrics.avg_r_multiple:.2f} |")
    lines.append(f"| Total trades | {metrics.total_trades} |")
    lines.append(f"| Total costs paid | ₹{metrics.total_costs:,.0f} |")
    lines.append(
        f"| Final equity | ₹{result.equity_curve.iloc[-1]:,.0f} |"
        if result.equity_curve.size
        else "| Final equity | — |"
    )
    lines.append("")
    lines.append("## Trades (first 20)")
    lines.append("")
    lines.append("| Symbol | Entry | Exit | Qty | Entry ₹ | Exit ₹ | Reason | Net P&L |")
    lines.append("|---|---|---|---:|---:|---:|---|---:|")
    for t in result.trades[:20]:
        lines.append(
            f"| {t.symbol} | {t.entry_date.date()} | {t.exit_date.date()} | {t.qty} "
            f"| {t.entry_price:.2f} | {t.exit_price:.2f} | {t.exit_reason} | "
            f"₹{t.net_pnl:+,.0f} |"
        )
    if len(result.trades) > 20:
        lines.append(f"\n_… {len(result.trades) - 20} more trades._")
    return "\n".join(lines) + "\n"


@app.command("ingest-news")
def ingest_news(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Aggregation date (YYYY-MM-DD). Defaults to today.",
        ),
    ] = None,
    skip_score: Annotated[
        bool,
        typer.Option(
            "--skip-score",
            help="Insert raw headlines without running FinBERT (fast, no model load).",
        ),
    ] = False,
    skip_aggregate: Annotated[
        bool,
        typer.Option(
            "--skip-aggregate",
            help="Don't write the daily sentiment_daily rollups after insert.",
        ),
    ] = False,
) -> None:
    """Fetch news from all sources, score with FinBERT, write news_items and sentiment_daily."""
    paths = get_paths()
    target_date = date.fromisoformat(as_of) if as_of else date.today()

    console.print("[bold]Fetching news from all sources…[/bold]")
    items = fetch_all_news()
    if not items:
        console.print("[yellow]No headlines retrieved.[/yellow]")
        raise typer.Exit(code=0)
    console.print(f"  Retrieved {len(items)} headline(s) after dedup.")

    if skip_score:
        console.print("[yellow]Skipping FinBERT (--skip-score).[/yellow]")
        scored = items
    else:
        console.print("[bold]Scoring with FinBERT (this loads ~440MB on first run)…[/bold]")
        scored = score_news_items(items)
        console.print(f"  Scored {len(scored)} headline(s).")

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        n = insert_news_items(conn, scored)
        console.print(f"[green]Inserted {n} row(s) into news_items.[/green]")

        if skip_aggregate:
            console.print("[yellow]Skipping daily aggregation (--skip-aggregate).[/yellow]")
            return

        watched = sorted(default_aliases().keys())
        rollups = aggregate_daily(conn, watched, target_date)
        console.print(
            f"[green]Wrote sentiment_daily for {len(rollups)} symbol(s) "
            f"as of {target_date.isoformat()}.[/green]"
        )
        if rollups:
            table = Table(show_header=True, header_style="bold")
            table.add_column("Symbol")
            table.add_column("7d", justify="right")
            table.add_column("30d", justify="right")
            table.add_column("News", justify="right")
            table.add_column("Neg", justify="right")
            table.add_column("Critical")
            for r in rollups:
                table.add_row(
                    r.symbol,
                    f"{r.score_7d:+.2f}" if r.score_7d is not None else "-",
                    f"{r.score_30d:+.2f}" if r.score_30d is not None else "-",
                    str(r.news_count),
                    str(r.negative_news_count),
                    "[red]YES[/red]" if r.has_critical else "no",
                )
            console.print(table)


@app.command("macro")
def macro_cmd(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Snapshot date (YYYY-MM-DD). Defaults to today.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the snapshot without writing to SQLite."),
    ] = False,
) -> None:
    """Pull macro indicators, classify the risk regime, write macro_snapshot."""
    paths = get_paths()
    target_date = date.fromisoformat(as_of) if as_of else date.today()

    console.print(f"[bold]Pulling macro snapshot for {target_date}…[/bold]")
    snap, result = snapshot_and_classify(target_date)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Indicator")
    table.add_column("Value", justify="right")
    table.add_row("S&P 500", _fmt(snap.sp500))
    table.add_row("Nasdaq", _fmt(snap.nasdaq_fut))
    table.add_row("Dow", _fmt(snap.dow_fut))
    table.add_row("USDINR", _fmt(snap.usdinr))
    table.add_row("Brent", _fmt(snap.crude))
    table.add_row("India VIX", _fmt(snap.vix))
    table.add_row("US 10y yield", _fmt(snap.us_10y))
    table.add_row("FII flow (Rs cr)", _fmt(snap.fii_flow_cr))
    table.add_row("DII flow (Rs cr)", _fmt(snap.dii_flow_cr))
    console.print(table)

    color = {"RISK_ON": "green", "NEUTRAL": "yellow", "RISK_OFF": "red"}[result.regime]
    console.print(
        f"\n[bold {color}]Regime: {result.regime}[/bold {color}]  "
        f"(composite score {result.composite_score:+d})"
    )
    for reason in result.reasons:
        console.print(f"  - {reason}")

    if dry_run:
        console.print("\n[yellow]Dry run — nothing written.[/yellow]")
        return

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_macro_snapshot(conn, snap)
    console.print(f"\n[green]macro_snapshot written for {snap.date}.[/green]")


@app.command("sector")
def sector_cmd(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Snapshot date (YYYY-MM-DD). Defaults to today.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the snapshot without writing to SQLite."),
    ] = False,
) -> None:
    """Pull NSE sectoral indices, compute RS vs Nifty 50, upsert sector_daily."""
    paths = get_paths()
    target_date = date.fromisoformat(as_of) if as_of else date.today()

    console.print(f"[bold]Pulling sector snapshot for {target_date}…[/bold]")
    rows = fetch_all_sectors(target_date)

    if not rows:
        console.print("[red]No sector rows fetched (benchmark or all sectors failed).[/red]")
        raise typer.Exit(code=1)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Sector")
    table.add_column("Close", justify="right")
    table.add_column("5d RS", justify="right")
    table.add_column("20d RS", justify="right")
    table.add_column("60d RS", justify="right")
    table.add_column("Regime", justify="center")
    for r in rows:
        table.add_row(
            r.sector,
            f"{r.close:,.0f}",
            _fmt_rs_pct(r.rs_5d),
            _fmt_rs_pct(r.rs_20d),
            _fmt_rs_pct(r.rs_60d),
            r.regime or "—",
        )
    console.print(table)

    if dry_run:
        console.print("\n[yellow]Dry run — nothing written.[/yellow]")
        return

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(conn, rows)
    console.print(
        f"\n[green]sector_daily written for {target_date.isoformat()} ({len(rows)} rows).[/green]"
    )


def _fmt_rs_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.2f}%"


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}"


@app.command("portfolio")
def portfolio_cmd(
    date_str: Annotated[
        str, typer.Option("--date", help="ISO date YYYY-MM-DD for the snapshot to read.")
    ],
    horizon_days: Annotated[
        int,
        typer.Option(help="GTT viability horizon in trading days."),
    ] = 60,
    n_paths: Annotated[
        int,
        typer.Option(help="Monte Carlo paths per GTT."),
    ] = 1000,
    seed: Annotated[
        int | None,
        typer.Option(help="Optional seed for reproducible GTT simulation."),
    ] = None,
    report: Annotated[
        str | None,
        typer.Option(help="Markdown report path (default: data/research/portfolio_<ts>.md)."),
    ] = None,
) -> None:
    """Score portfolio health + project GTT viability from today's Kite snapshot."""
    paths = get_paths()
    as_of = date.fromisoformat(date_str)
    try:
        holdings_list = _snapshot_read_holdings(paths, as_of)
        gtts_list = _snapshot_read_gtts(paths, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    console.print(
        f"[bold]Loaded {len(holdings_list)} holding(s), "
        f"{len(gtts_list)} GTT(s) from snapshot.[/bold]"
    )

    # Enrich each holding with parquet history + sentiment
    enriched: dict[str, pd.DataFrame] = {}
    health_rows = []
    today_iso = date.today().isoformat()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        for h in holdings_list:
            sym = h.tradingsymbol
            try:
                raw = read_ohlcv(sym, paths)
                if len(raw) >= 200:
                    enriched[sym] = add_indicators(raw)
            except FileNotFoundError:
                pass
            sent_row = get_sentiment_daily(conn, today_iso, sym)
            sent = SentimentSnapshot(
                score_30d=sent_row.score_30d if sent_row else None,
                has_critical=sent_row.has_critical if sent_row else False,
            )
            tech = (
                technicals_from_history(enriched[sym])
                if sym in enriched
                else technicals_from_history(None)  # falls through to empty
            )
            ctx = HoldingContext(
                symbol=sym,
                qty=h.quantity,
                avg_price=h.average_price,
                last_price=h.last_price,
                technicals=tech,
                sentiment=sent,
            )
            health_rows.append(score_holding(ctx))

    _print_health_table(health_rows)

    # GTT viability
    viabilities = project_all_gtts(
        gtts_list,
        enriched,
        horizon_days=horizon_days,
        n_paths=n_paths,
        seed=seed,
    )
    _print_gtt_table(viabilities)

    # Markdown report
    report_path = (
        Path(report)
        if report
        else paths.research_dir / f"portfolio_{datetime.now():%Y%m%d_%H%M%S}.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_portfolio_report(health_rows, viabilities),
        encoding="utf-8",
    )
    console.print(f"\n[green]Report written to[/green] {report_path}")


def _print_health_table(rows: list) -> None:  # type: ignore[type-arg]
    if not rows:
        console.print("[yellow]No holdings to score.[/yellow]")
        return
    table = Table(
        title="Holdings health (HOLD / TRIM / EXIT)",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Symbol")
    table.add_column("Verdict")
    table.add_column("Score", justify="right")
    table.add_column("Net votes", justify="right")
    table.add_column("P&L %", justify="right")
    table.add_column("Top reason")
    color = {"HOLD": "green", "TRIM": "yellow", "EXIT": "red"}
    for r in rows:
        c = color[r.verdict]
        pnl = f"{r.pnl_pct:+.1f}%" if r.pnl_pct is not None else "-"
        table.add_row(
            r.symbol,
            f"[bold {c}]{r.verdict}[/bold {c}]",
            f"{r.score}/100",
            f"{r.net_votes:+d} of {r.votes_cast}",
            pnl,
            r.reasons[0] if r.reasons else "",
        )
    console.print(table)


def _print_gtt_table(viabilities: list) -> None:  # type: ignore[type-arg]
    if not viabilities:
        console.print("[yellow]No GTTs to project.[/yellow]")
        return
    table = Table(
        title="GTT viability (Monte Carlo)",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Symbol")
    table.add_column("Trigger", justify="right")
    table.add_column("Last", justify="right")
    table.add_column("P(hit)", justify="right")
    table.add_column("Exp days", justify="right")
    table.add_column("Note")
    for v in viabilities:
        trigger = ", ".join(f"{t:.2f}" for t in v.trigger_values)
        prob = f"{v.probability_hit:.0%}" if v.probability_hit is not None else "-"
        days = f"{v.expected_days_to_hit:.0f}" if v.expected_days_to_hit else "-"
        last = f"{v.last_price:.2f}" if v.last_price is not None else "-"
        table.add_row(v.symbol, trigger, last, prob, days, v.note or "")
    console.print(table)


def _render_portfolio_report(health_rows: list, viabilities: list) -> str:  # type: ignore[type-arg]
    lines: list[str] = []
    lines.append(f"# Portfolio analysis — {date.today().isoformat()}")
    lines.append("")
    lines.append("## Holdings health")
    lines.append("")
    lines.append("| Symbol | Verdict | Score | Net votes | P&L % | Reasons |")
    lines.append("|---|---|---:|---:|---:|---|")
    for r in health_rows:
        pnl = f"{r.pnl_pct:+.1f}%" if r.pnl_pct is not None else "—"
        reasons = "; ".join(r.reasons) if r.reasons else "—"
        lines.append(
            f"| {r.symbol} | **{r.verdict}** | {r.score}/100 | "
            f"{r.net_votes:+d} of {r.votes_cast} | {pnl} | {reasons} |"
        )

    lines.append("")
    lines.append("## GTT viability")
    lines.append("")
    lines.append("| Symbol | Trigger | Last | P(hit) | Expected days | Note |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for v in viabilities:
        trigger = ", ".join(f"₹{t:,.2f}" for t in v.trigger_values)
        prob = f"{v.probability_hit:.1%}" if v.probability_hit is not None else "—"
        days = f"{v.expected_days_to_hit:.1f}" if v.expected_days_to_hit else "—"
        last = f"₹{v.last_price:,.2f}" if v.last_price is not None else "—"
        lines.append(f"| {v.symbol} | {trigger} | {last} | {prob} | {days} | {v.note or ''} |")
    return "\n".join(lines) + "\n"


@app.command("paper-open")
def paper_open_cmd(
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="NSE ticker.")],
    entry: Annotated[float, typer.Option(help="Entry price (Rs).")],
    stop: Annotated[float, typer.Option(help="Initial stop price (Rs).")],
    target: Annotated[float, typer.Option(help="Target price (Rs).")],
    qty: Annotated[int, typer.Option(help="Position quantity.")],
    horizon: Annotated[int, typer.Option(help="Horizon in trading days.")] = 15,
    atr: Annotated[float, typer.Option("--atr", help="ATR at entry (for trailing).")] = 0.0,
    rationale: Annotated[str, typer.Option(help="One-line rationale.")] = "manual entry",
) -> None:
    """Manually log a signal and open a paper trade — ad-hoc seeding."""
    paths = get_paths()
    signal = Signal(
        id=None,
        ts=datetime.now().isoformat(),
        symbol=symbol.upper(),
        side="LONG",
        entry=entry,
        stop=stop,
        target=target,
        horizon_days=horizon,
        rationale=rationale,
        created_by="manual",
    )
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        result = log_signal_and_open_trade(
            conn,
            signal=signal,
            entry_ts=datetime.now(),
            entry_price=entry,
            qty=qty,
            atr_at_entry=atr,
        )
    console.print(
        f"[green]Opened paper trade[/green] id={result.paper_trade_id} "
        f"(signal {result.signal_id}, prediction {result.prediction_id}) "
        f"on {symbol.upper()} @ Rs {entry:,.2f}, qty {qty}, stop Rs {stop:,.2f}, target Rs {target:,.2f}"
    )


@app.command("paper-mtm")
def paper_mtm_cmd(
    as_of: Annotated[
        str | None,
        typer.Option("--date", help="MTM date (YYYY-MM-DD). Defaults to today."),
    ] = None,
) -> None:
    """Run MTM against parquet bars for the given date."""
    paths = get_paths()
    target_date = date.fromisoformat(as_of) if as_of else date.today()
    target_dt = datetime.combine(target_date, datetime.min.time().replace(hour=15, minute=30))

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        trades = open_trades(conn)
        if not trades:
            console.print("[yellow]No open paper trades.[/yellow]")
            return
        symbols = set()
        for t in trades:
            sig = get_signal(conn, t.signal_id)
            if sig is not None:
                symbols.add(sig.symbol)
        histories = {}
        for sym in symbols:
            with contextlib.suppress(FileNotFoundError):
                histories[sym] = read_ohlcv(sym, paths)
        bars = build_bars_from_history(histories, target_dt)
        results = mtm_open_trades(conn, bars, as_of=target_dt)

    table = Table(title=f"MTM for {target_date}", show_header=True, header_style="bold")
    table.add_column("Trade")
    table.add_column("Symbol")
    table.add_column("Action")
    table.add_column("New stop", justify="right")
    table.add_column("Exit price", justify="right")
    table.add_column("Reason")
    color = {
        "HOLD": "green",
        "EXIT_TARGET": "green",
        "EXIT_STOP": "red",
        "EXIT_TIME": "yellow",
        "SKIP": "yellow",
    }
    for r in results:
        c = color.get(r.action, "white")
        new_stop = f"{r.new_stop:.2f}" if r.new_stop is not None else "-"
        exit_p = f"{r.exit_price:.2f}" if r.exit_price is not None else "-"
        table.add_row(
            str(r.paper_trade_id),
            r.symbol,
            f"[bold {c}]{r.action}[/bold {c}]",
            new_stop,
            exit_p,
            r.reason,
        )
    console.print(table)
    n_closed = sum(1 for r in results if r.action.startswith("EXIT_"))
    n_hold = sum(1 for r in results if r.action == "HOLD")
    n_skip = sum(1 for r in results if r.action == "SKIP")
    console.print(f"\n[bold]{n_closed} closed[/bold] · {n_hold} held · {n_skip} skipped")


@app.command("paper-status")
def paper_status_cmd() -> None:
    """Show open paper trades + most-recent closes + portfolio snapshot."""
    paths = get_paths()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        opens = open_trades(conn)
        # Resolve each open trade's signal *while the connection is open* —
        # doing it in the render loop below crashed with "Cannot operate on a
        # closed database" once the `with` block exited.
        open_display: list[tuple[int | None, str, float, float, int, int]] = []
        for t in opens:
            sig = get_signal(conn, t.signal_id)
            sym = sig.symbol if sig else "?"
            curr_stop = t.current_stop if t.current_stop is not None else (sig.stop if sig else 0)
            open_display.append((t.id, sym, t.entry_price, curr_stop, t.qty, t.days_held or 0))
        closed_rows = conn.execute(
            """SELECT pt.*, s.symbol FROM paper_trades pt
               JOIN signals s ON s.id = pt.signal_id
               WHERE pt.ts_exit IS NOT NULL
               ORDER BY pt.ts_exit DESC LIMIT 10"""
        ).fetchall()
        snap_row = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()

    # Open trades
    if open_display:
        table = Table(title="Open paper trades", show_header=True, header_style="bold")
        table.add_column("Trade")
        table.add_column("Symbol")
        table.add_column("Entry", justify="right")
        table.add_column("Qty", justify="right")
        table.add_column("Stop (curr)", justify="right")
        table.add_column("Days", justify="right")
        for trade_id, sym, entry_price, curr_stop, qty, days_held in open_display:
            table.add_row(
                str(trade_id),
                sym,
                f"{entry_price:.2f}",
                str(qty),
                f"{curr_stop:.2f}",
                str(days_held),
            )
        console.print(table)
    else:
        console.print("[yellow]No open paper trades.[/yellow]")

    # Recent closes
    if closed_rows:
        rt = Table(title="Recent closes (last 10)", show_header=True, header_style="bold")
        rt.add_column("Symbol")
        rt.add_column("Exit", justify="right")
        rt.add_column("Qty", justify="right")
        rt.add_column("Reason")
        rt.add_column("P&L %", justify="right")
        rt.add_column("Days", justify="right")
        for r in closed_rows:
            pnl_pct = r["pnl_pct"] if r["pnl_pct"] is not None else 0.0
            c = "green" if pnl_pct >= 0 else "red"
            rt.add_row(
                r["symbol"],
                f"{r['exit_price']:.2f}" if r["exit_price"] is not None else "-",
                str(r["qty"]),
                r["exit_reason"] or "-",
                f"[{c}]{pnl_pct:+.1f}%[/{c}]",
                str(r["days_held"] or 0),
            )
        console.print(rt)

    # Snapshot
    if snap_row:
        console.print(
            f"\n[bold]Latest snapshot ({snap_row['date']})[/bold]: "
            f"cash Rs {snap_row['cash']:,.0f}, "
            f"equity Rs {snap_row['equity']:,.0f}, "
            f"drawdown {snap_row['drawdown_pct']:.2f}%"
            if snap_row["drawdown_pct"] is not None
            else f"\n[bold]Latest snapshot ({snap_row['date']})[/bold]: "
            f"cash Rs {snap_row['cash']:,.0f}, equity Rs {snap_row['equity']:,.0f}"
        )


@app.command("paper-reconcile")
def paper_reconcile_cmd(
    as_of: Annotated[
        str | None,
        typer.Option("--date", help="Reconcile date (YYYY-MM-DD). Defaults to today."),
    ] = None,
    capital: Annotated[
        float,
        typer.Option(help="Starting paper capital; live cash is derived from the trade ledger."),
    ] = INITIAL_CAPITAL,
) -> None:
    """Evaluate matured predictions + write today's portfolio_snapshots row."""
    paths = get_paths()
    target_date = date.fromisoformat(as_of) if as_of else date.today()
    target_dt = datetime.combine(target_date, datetime.min.time().replace(hour=15, minute=30))

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        # Build bars from parquet for any open positions (used for both MTM
        # of equity and the bar-path of matured predictions).
        trades = open_trades(conn)
        symbols = set()
        for t in trades:
            sig = get_signal(conn, t.signal_id)
            if sig is not None:
                symbols.add(sig.symbol)
        histories = {}
        for sym in symbols:
            with contextlib.suppress(FileNotFoundError):
                histories[sym] = read_ohlcv(sym, paths)
        bars = build_bars_from_history(histories, target_dt)
        result = reconcile_day(conn, as_of=target_date, bars=bars, initial_capital=capital)

    console.print(
        f"[green]Reconciled {target_date}[/green]: "
        f"equity Rs {result.snapshot.equity:,.0f}, "
        f"drawdown {result.snapshot.drawdown_pct or 0:.2f}%, "
        f"{len(result.prediction_updates)} prediction(s) matured."
    )
    if result.prediction_updates:
        pt = Table(title="Matured predictions", show_header=True, header_style="bold")
        pt.add_column("Symbol")
        pt.add_column("Predicted %", justify="right")
        pt.add_column("Actual %", justify="right")
        pt.add_column("Error %", justify="right")
        for u in result.prediction_updates:
            c = "green" if u.error_pct >= 0 else "red"
            pt.add_row(
                u.symbol,
                f"{u.predicted_return_pct:+.1f}",
                f"{u.actual_return_pct:+.1f}",
                f"[{c}]{u.error_pct:+.1f}[/{c}]",
            )
        console.print(pt)


brief_app = typer.Typer(help="Daily-brief context assembly + compilation (Phase 12).")
app.add_typer(brief_app, name="brief")


@brief_app.command("assemble-context")
def brief_assemble_context_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    mode: Annotated[str, typer.Option("--mode", help="pre_open or post_close")] = "pre_open",
) -> None:
    """Write data/research/YYYY-MM-DD/_context.md for the analyst skill."""
    if mode not in ("pre_open", "post_close"):
        console.print(f"[red]invalid --mode: {mode}[/red]")
        raise typer.Exit(code=2)
    as_of = date.fromisoformat(date_str)
    paths = get_paths()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        out = _assemble_context(
            conn=conn,
            paths=paths,
            as_of=as_of,
            mode=mode,  # type: ignore[arg-type]
            inputs=ContextInputs(),
        )
    console.print(f"[green]wrote[/green] {out}")
    console.print("[bold]now run /analyst skill in Claude Code[/bold]")


@brief_app.command("compile")
def brief_compile_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    allow_stale: Annotated[
        bool,
        typer.Option(
            "--allow-stale",
            help="Compile even if the bundle is older than 12h (F-026).",
        ),
    ] = False,
) -> None:
    """Concatenate analyst-produced narrative parts into brief.md."""
    paths = get_paths()
    date_dir = paths.research_dir / date_str
    try:
        out = compile_brief(date_dir, allow_stale=allow_stale)
    except StaleBundleError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    except MissingNarrativeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    console.print(f"[green]wrote[/green] {out}")


@app.command("pre-open")
def pre_open_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    skip_news: Annotated[bool, typer.Option("--skip-news")] = False,
    capital: Annotated[float, typer.Option(help="Capital per trade.")] = 100_000.0,
    risk_pct: Annotated[float, typer.Option(help="Risk per trade.")] = 0.02,
) -> None:
    """Phase 13 MVP — orchestrate Phases 1-12 and write the analyst bundle."""
    as_of = date.fromisoformat(date_str)
    try:
        result = run_pre_open(
            as_of,
            skip_news=skip_news,
            capital_per_trade=capital,
            risk_pct=risk_pct,
        )
    except PreOpenAborted as e:
        console.print(f"[red]Pre-open aborted:[/red] {e}")
        raise typer.Exit(code=2) from e
    table = Table(title=f"pre_open {as_of.isoformat()}", show_header=True)
    table.add_column("step")
    table.add_column("count", justify="right")
    table.add_row("ohlcv_bars_added", str(result.ohlcv_bars_added))
    table.add_row("macro_written", "yes" if result.macro_written else "no")
    table.add_row("sector_written", "yes" if result.sector_written else "no")
    table.add_row("news_inserted", str(result.news_inserted))
    table.add_row("sentiment_rows", str(result.sentiment_rows))
    table.add_row("candidates_total", str(result.candidates_total))
    table.add_row("candidates_passing", str(result.candidates_passing))
    table.add_row("candidates_selected", str(result.candidates_selected))
    table.add_row("paper_trades_opened", str(result.paper_trades_opened))
    table.add_row("holdings_scored", str(result.holdings_scored))
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")
    console.print(f"[green]wrote[/green] {result.bundle_path}")
    console.print(
        f"[bold]Now run /analyst skill, then `trading brief compile --date {date_str}`[/bold]"
    )


@app.command("mid-day")
def mid_day_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Apply mode: read quotes + run MTM. Without --apply runs prepare mode.",
        ),
    ] = False,
) -> None:
    """Phase 14.A — mid-day MTM. Two-phase: prepare → /kite-quotes-snapshot → apply."""
    as_of = date.fromisoformat(date_str)
    try:
        result = run_mid_day(as_of, apply=apply)
    except MidDayAborted as e:
        console.print(f"[red]Mid-day aborted:[/red] {e}")
        raise typer.Exit(code=2) from e

    if result.symbols_path is not None:
        console.print(f"[green]wrote[/green] {result.symbols_path}")
        console.print(
            "[bold]Now run /kite-quotes-snapshot skill in Claude Code, "
            f"then `trading mid-day --date {date_str} --apply`[/bold]"
        )
        return

    table = Table(title=f"mid-day {as_of.isoformat()}", show_header=True)
    table.add_column("step")
    table.add_column("count", justify="right")
    table.add_row("quotes_captured_at", str(result.quotes_capture_ts))
    table.add_row("bars_built", str(result.bars_built))
    table.add_row("trades_evaluated", str(result.trades_evaluated))
    table.add_row("trades_closed", str(result.trades_closed))
    table.add_row("trades_held", str(result.trades_held))
    table.add_row("trades_skipped", str(result.trades_skipped))
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")
    console.print(f"[green]wrote[/green] {result.update_path}")


@app.command("post-close")
def post_close_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Apply mode: read quotes + run MTM + reconcile. Without --apply runs prepare mode.",
        ),
    ] = False,
    capital: Annotated[
        float,
        typer.Option(help="Starting paper capital; live cash is derived from the trade ledger."),
    ] = INITIAL_CAPITAL,
) -> None:
    """Phase 14.B — end-of-day MTM + reconcile + summary."""
    as_of = date.fromisoformat(date_str)
    try:
        result = run_post_close(as_of, apply=apply, initial_capital=capital)
    except PostCloseAborted as e:
        console.print(f"[red]Post-close aborted:[/red] {e}")
        raise typer.Exit(code=2) from e

    if result.symbols_path is not None:
        console.print(f"[green]wrote[/green] {result.symbols_path}")
        console.print(
            "[bold]Now run /kite-quotes-snapshot skill in Claude Code, "
            f"then `trading post-close --date {date_str} --apply`[/bold]"
        )
        return

    table = Table(title=f"post-close {as_of.isoformat()}", show_header=True)
    table.add_column("step")
    table.add_column("count", justify="right")
    table.add_row("quotes_captured_at", str(result.quotes_capture_ts))
    table.add_row("bars_built", str(result.bars_built))
    table.add_row("trades_evaluated", str(result.trades_evaluated))
    table.add_row("trades_closed", str(result.trades_closed))
    table.add_row("trades_held", str(result.trades_held))
    table.add_row("trades_skipped", str(result.trades_skipped))
    table.add_row("predictions_matured", str(result.predictions_matured))
    table.add_row(
        "equity",
        f"Rs {result.equity:,.0f}" if result.equity is not None else "—",
    )
    table.add_row(
        "drawdown_pct",
        f"{result.drawdown_pct:+.2f}%" if result.drawdown_pct is not None else "—",
    )
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")
    console.print(f"[green]wrote[/green] {result.summary_path}")


@app.command("pre-open-iep")
def pre_open_iep_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
) -> None:
    """Phase 14.C — pre-open gap filter + rerank of pre_open candidates (08:55)."""
    as_of = date.fromisoformat(date_str)
    try:
        result = run_pre_open_iep(as_of)
    except PreOpenIepAborted as e:
        console.print(f"[red]Pre-open IEP aborted:[/red] {e}")
        raise typer.Exit(code=2) from e

    table = Table(title=f"pre-open-iep {as_of.isoformat()}", show_header=True)
    table.add_column("step")
    table.add_column("count", justify="right")
    table.add_row("regime", result.regime)
    table.add_row("candidates_input", str(result.candidates_input))
    table.add_row("candidates_filtered", str(result.candidates_filtered))
    table.add_row("candidates_removed", str(result.candidates_removed))
    table.add_row("rerank_applied", "yes" if result.rerank_applied else "no")
    console.print(table)
    if result.removed_symbols:
        console.print(f"[yellow]Removed:[/yellow] {', '.join(result.removed_symbols)}")
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")
    if result.context_path is not None:
        console.print(f"[green]updated[/green] {result.context_path}")
        console.print("[bold]Ready for /analyst skill[/bold]")


@app.command("remind")
def remind_cmd(
    slot: Annotated[str, typer.Option(help="Slot name from ops.runner.SCHEDULE")],
) -> None:
    """Fire a single reminder. Invoked by Windows Task Scheduler entries."""
    if slot not in SCHEDULE:
        console.print(f"[red]unknown slot:[/red] {slot}")
        console.print(f"valid slots: {', '.join(sorted(SCHEDULE.keys()))}")
        raise typer.Exit(code=2)
    fire_reminder(slot)


@app.command("notify-test")
def notify_test_cmd() -> None:
    """Fire a sanity-check notification on both Slack and Windows toast.

    Useful after first-time setup to verify SLACK_WEBHOOK_URL and plyer
    are wired correctly.
    """
    _notify(
        "info",
        "Trading notify-test",
        "If you see this in Slack AND as a Windows toast, you're wired up correctly.",
    )
    console.print("[green]ok[/green] — check Slack + Windows notification area")


@app.command("status")
def status_cmd(
    date_str: Annotated[
        str | None, typer.Option("--date", help="ISO date YYYY-MM-DD (default: today IST)")
    ] = None,
) -> None:
    """Report which daily-flow checkpoints have run (half-run detection, F-003).

    Infers per-step completion from on-disk artifacts; time-aware, so a step is
    only flagged missing once its IST slot time has passed. Exits 1 if any due
    step is missing (so cron/scripts can gate on it).
    """
    paths = get_paths()
    as_of = date.fromisoformat(date_str) if date_str else _today_ist()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        statuses = compute_status(paths, as_of, conn=conn)

    glyph = {"done": "[green]✅[/green]", "missing": "[red]❌[/red]",
             "pending": "[grey50]·[/grey50]", "n/a": "[grey50]-[/grey50]"}
    table = Table(title=f"daily status — {as_of.isoformat()}", show_header=True)
    table.add_column("")
    table.add_column("block")
    table.add_column("when", justify="right")
    table.add_column("step")
    table.add_column("detail")
    for s in statuses:
        table.add_row(
            glyph.get(s.state, s.state),
            s.block,
            s.when.strftime("%H:%M"),
            s.label,
            s.detail,
        )
    console.print(table)

    by_block: dict[str, list[StepStatus]] = {}
    for s in statuses:
        by_block.setdefault(s.block, []).append(s)
    parts = []
    for block, items in by_block.items():
        done = sum(1 for s in items if s.state == "done")
        flag = " [red]❌[/red]" if any(s.state == "missing" for s in items) else ""
        parts.append(f"{block} {done}/{len(items)}{flag}")
    console.print("  ·  ".join(parts))

    if has_due_failure(statuses):
        console.print("[red]Half-run detected:[/red] a due step has not run.")
        raise typer.Exit(code=1)


@app.command("ranker-status")
def ranker_status() -> None:
    """Show the current model registry (Phase 16)."""
    from trading.store.model_registry import all_rows

    paths = get_paths()
    rows = all_rows(paths)
    if not rows:
        console.print("no models registered")
        raise typer.Exit(code=0)
    table = Table(title="Model registry")
    table.add_column("version")
    table.add_column("trained_at")
    table.add_column("OOS Sharpe", justify="right")
    table.add_column("OOS hit", justify="right")
    table.add_column("examples", justify="right")
    table.add_column("active")
    table.add_column("path")
    for r in rows:
        trained_at_short = r.trained_at.split("T")[0] if "T" in r.trained_at else r.trained_at
        table.add_row(
            r.version,
            trained_at_short,
            f"{r.oos_sharpe:.2f}" if r.oos_sharpe == r.oos_sharpe else "—",
            f"{r.oos_hit_rate:.2f}" if r.oos_hit_rate == r.oos_hit_rate else "—",
            str(r.n_train_examples),
            "✓" if r.active else "",
            r.path,
        )
    console.print(table)


@app.command("train-ranker")
def train_ranker(
    start: Annotated[str, typer.Option(help="Train period start (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option(help="Train period end (YYYY-MM-DD).")],
    promote: Annotated[
        bool, typer.Option("--promote/--no-promote", help="Apply soft-promotion gate.")
    ] = False,
    report: Annotated[
        bool, typer.Option("--report", help="Write markdown report to data/research/.")
    ] = False,
) -> None:
    """Train the Phase 16 LightGBM ranker over a walk-forward window."""
    from datetime import datetime

    from trading.store.model_registry import (
        RegistryRow,
        register,
        save_model,
    )
    from trading.strategy.ranker_features import FEATURE_NAMES
    from trading.strategy.ranker_io import load_training_inputs
    from trading.strategy.ranker_train import (
        InsufficientDataError,
        train_walkforward,
    )

    paths = get_paths()
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    syms = list_symbols(paths)
    if not syms:
        console.print("[red]no parquet symbols found — run ingest-history first[/red]")
        raise typer.Exit(code=2)

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        inputs = load_training_inputs(paths, conn)

    if not inputs.enriched:
        console.print("[red]no symbols with sufficient history (200+ bars)[/red]")
        raise typer.Exit(code=2)

    try:
        result = train_walkforward(
            enriched=inputs.enriched,
            macro_history=inputs.macro_history,
            sentiment_lookup=inputs.sentiment_lookup,
            negative_news_lookup={},
            start=start_ts,
            end=end_ts,
        )
    except InsufficientDataError as e:
        console.print(f"[red]train-ranker:[/red] {e}")
        raise typer.Exit(code=2) from e

    table = Table(title="Walk-forward fold metrics")
    table.add_column("train")
    table.add_column("test")
    table.add_column("examples", justify="right")
    table.add_column("OOS trades", justify="right")
    table.add_column("OOS Sharpe", justify="right")
    table.add_column("OOS hit", justify="right")
    table.add_column("skipped")
    for f in result.folds:
        table.add_row(
            f"{f.train_start.date()}→{f.train_end.date()}",
            f"{f.test_start.date()}→{f.test_end.date()}",
            str(f.n_train_examples),
            str(f.n_trades_oos),
            f"{f.sharpe_oos:.2f}" if f.sharpe_oos == f.sharpe_oos else "—",
            f"{f.hit_rate_oos:.2f}" if f.hit_rate_oos == f.hit_rate_oos else "—",
            "✓" if f.skipped else "",
        )
    console.print(table)
    console.print(
        f"OOS Sharpe mean: {result.oos_sharpe_mean:.3f} | "
        f"OOS hit-rate mean: {result.oos_hit_rate_mean:.3f} | "
        f"final examples: {result.n_final_examples}"
    )

    version = end_ts.strftime("%Y-%m-%d")
    pkl_rel = f"models/ranker_{version}.pkl"
    pkl_path = paths.project_root / pkl_rel
    save_model(pkl_path, result.final_model, FEATURE_NAMES)
    row = RegistryRow(
        version=version,
        trained_at=datetime.now(UTC).isoformat(),
        train_start=str(result.final_train_start.date()),
        train_end=str(result.final_train_end.date()),
        oos_sharpe=result.oos_sharpe_mean,
        oos_hit_rate=result.oos_hit_rate_mean,
        n_train_examples=result.n_final_examples,
        n_features=len(FEATURE_NAMES),
        path=pkl_rel,
        active=False,
        notes="",
    )
    became_active = register(paths, row=row, promote=promote)
    console.print(f"saved {pkl_rel} (active={became_active})")

    if report:
        out_path = paths.research_dir / f"ranker_{datetime.now().strftime('%Y%m%dT%H%M%S')}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            f"# Ranker training run {version}\n\n"
            f"- Train period: {result.final_train_start.date()} → {result.final_train_end.date()}\n"
            f"- Final examples: {result.n_final_examples}\n"
            f"- OOS Sharpe mean: {result.oos_sharpe_mean:.3f}\n"
            f"- OOS hit-rate mean: {result.oos_hit_rate_mean:.3f}\n"
            f"- Active: {became_active}\n\n"
            f"## Per-fold metrics\n\n"
            + "\n".join(
                f"- {f.train_start.date()}→{f.train_end.date()} test "
                f"{f.test_start.date()}→{f.test_end.date()}: "
                f"n={f.n_train_examples} sharpe={f.sharpe_oos:.2f} hit={f.hit_rate_oos:.2f} "
                f"{'(skipped)' if f.skipped else ''}"
                for f in result.folds
            )
            + "\n",
            encoding="utf-8",
        )
        console.print(f"report written to {out_path}")


@app.command("weekly-train")
def weekly_train_cmd(
    date_str: Annotated[
        str, typer.Option("--date", help="Review date YYYY-MM-DD (default: today IST).")
    ] = "",
    skip_train: Annotated[
        bool, typer.Option("--skip-train", help="Skip retraining; write the review only.")
    ] = False,
) -> None:
    """Sunday weekly_train job (Phase 18): rolling retrain + weekly review."""
    from datetime import date as _date

    from loguru import logger

    from trading.jobs.weekly_train import run_weekly_train
    from trading.ops.logging_setup import configure_logging

    configure_logging("weekly_train")
    try:
        result = run_weekly_train(
            _date.fromisoformat(date_str) if date_str else None,
            skip_train=skip_train,
        )
    except Exception:
        # Record the failure durably (failures.log + per-job log) so the
        # unattended Sunday run can be flagged and fixed, then re-raise so
        # Task Scheduler marks the task failed.
        logger.exception("weekly_train failed")
        raise

    table = Table(title=f"weekly_train — {result.as_of.isoformat()}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("window", f"{result.window_start} → {result.window_end}")
    table.add_row(
        "retrain_ran",
        "yes" if result.retrain_ran else f"no — {result.retrain_skip_reason}",
    )
    table.add_row("examples", str(result.examples) if result.examples is not None else "—")
    table.add_row("promoted", "yes" if result.promoted else "no")
    table.add_row("model", result.model_path or "—")
    table.add_row("review", str(result.review_path))
    console.print(table)


@app.command("daily-unattended")
def daily_unattended_cmd(
    date_str: Annotated[
        str, typer.Option("--date", help="ISO date YYYY-MM-DD (default: today IST).")
    ] = "",
    force: Annotated[
        bool, typer.Option("--force", help="Run even if the spine already ran today.")
    ] = False,
) -> None:
    """Unattended broker-free daily gap-filler (F-032).

    Runs the macro/scan/auto-open/bundle spine without a Kite snapshot, but only
    on trading days the operator hasn't already covered. Scheduled like
    weekly-train; failures are recorded durably in failures.log.
    """
    from loguru import logger

    from trading.jobs.daily_unattended import run_daily_unattended
    from trading.ops.logging_setup import configure_logging

    configure_logging("daily_unattended")
    as_of = date.fromisoformat(date_str) if date_str else _today_ist()
    try:
        result = run_daily_unattended(as_of, force=force)
    except Exception:
        # Unattended — record durably (failures.log) before propagating so the
        # scheduled run can be flagged and fixed.
        logger.exception("daily_unattended failed")
        raise

    if not result.ran:
        console.print(f"[yellow]skipped[/yellow] {as_of.isoformat()} — {result.skipped_reason}")
        return

    r = result.pre_open
    assert r is not None  # ran=True always carries a pre_open result
    table = Table(title=f"daily_unattended — {as_of.isoformat()}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("candidates_passing", str(r.candidates_passing))
    table.add_row("candidates_selected", str(r.candidates_selected))
    table.add_row("paper_trades_opened", str(r.paper_trades_opened))
    table.add_row("bundle", str(r.bundle_path))
    console.print(table)
    for w in r.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")


@app.command("prune")
def prune_cmd(
    date_str: Annotated[
        str, typer.Option("--date", help="As-of date YYYY-MM-DD (default: today IST).")
    ] = "",
    raw_days: Annotated[
        int, typer.Option("--raw-days", help="Keep this many days of raw/<date>/ dirs.")
    ] = DEFAULT_RAW_KEEP_DAYS,
    news_days: Annotated[
        int, typer.Option("--news-days", help="Keep this many days of news_items rows.")
    ] = DEFAULT_NEWS_KEEP_DAYS,
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually delete (default: dry-run report only).")
    ] = False,
) -> None:
    """Prune stale raw date-dirs + old news_items rows (F-013).

    Dry-run by default — pass --apply to delete. Also runs automatically as a
    housekeeping step inside the Sunday weekly-train job.
    """
    from trading.ops.retention import run_retention

    paths = get_paths()
    as_of = date.fromisoformat(date_str) if date_str else _today_ist()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        result = run_retention(
            paths,
            conn,
            as_of=as_of,
            raw_keep_days=raw_days,
            news_keep_days=news_days,
            apply=apply,
        )

    mode = "APPLIED" if result.applied else "dry-run"
    table = Table(title=f"prune — {result.as_of.isoformat()} ({mode})")
    table.add_column("target")
    table.add_column("keep days", justify="right")
    table.add_column("deleted", justify="right")
    table.add_row(
        "raw/<date>/ dirs", str(result.raw_keep_days), str(len(result.raw_dirs_deleted))
    )
    table.add_row("news_items rows", str(result.news_keep_days), str(result.news_rows_deleted))
    console.print(table)
    for name in result.raw_dirs_deleted:
        console.print(f"  raw/{name}")
    if not result.applied and (result.raw_dirs_deleted or result.news_rows_deleted):
        console.print("[yellow]dry-run — re-run with --apply to delete.[/yellow]")


@app.command("sip")
def sip_cmd(
    date_str: Annotated[str, typer.Option("--date", help="Plan date (YYYY-MM-DD).")],
    budget: Annotated[float, typer.Option("--budget", help="Monthly SIP budget in ₹.")] = 100_000.0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print only — no file write, no Slack.")
    ] = False,
) -> None:
    """Monthly ₹1L SIP allocation plan (Phase 18). Needs today's /kite-snapshot."""
    from datetime import date as _date

    from loguru import logger

    from trading.jobs.monthly_sip import MonthlySipAborted, run_monthly_sip
    from trading.ops.logging_setup import configure_logging

    configure_logging("monthly_sip")
    try:
        result = run_monthly_sip(_date.fromisoformat(date_str), budget=budget, dry_run=dry_run)
    except MonthlySipAborted as e:
        # Expected, user-actionable (missing/stale snapshot) — not a failure to log.
        console.print(f"[red]sip aborted:[/red] {e}")
        console.print("Run /kite-snapshot in Claude Code first, then retry.")
        raise typer.Exit(code=2) from e
    except Exception:
        # Unexpected — record durably (failures.log) before propagating.
        logger.exception("monthly_sip failed")
        raise

    table = Table(title=f"monthly_sip — {result.as_of.isoformat()}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("budget", f"₹{result.budget:,.0f}")
    table.add_row("holdings", str(result.holdings_count))
    table.add_row("candidates", str(result.candidates_considered))
    table.add_row("deployed", f"₹{result.deployed:,.0f}")
    table.add_row("cash_reserve", f"₹{result.cash_reserve:,.0f}")
    table.add_row("allocations", str(result.allocations))
    table.add_row("plan", str(result.plan_path) if result.plan_path else "— (dry run)")
    console.print(table)
    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")


if __name__ == "__main__":  # pragma: no cover — manual entry
    app()
