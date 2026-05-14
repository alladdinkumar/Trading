"""Typer CLI entry — `uv run trading <command>`."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
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
    is_authenticated,
    login_url,
    make_client,
)
from trading.data.macro import snapshot_and_classify
from trading.data.news import DEFAULT_ALIASES, fetch_all_news
from trading.data.universe import load_universe
from trading.data.yfinance import OhlcvFetchError, fetch_ohlcv
from trading.features.sentiment import aggregate_daily, score_news_items
from trading.features.technicals import add_indicators
from trading.store.db import get_conn
from trading.store.macro_store import upsert_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.news_store import insert_news_items
from trading.store.ohlcv import list_symbols, parquet_path, read_ohlcv, write_ohlcv
from trading.strategy.rules import ScanContext, passing, scan

app = typer.Typer(help="Trading — research and paper-trading CLI.", add_completion=False)
console = Console()


@app.callback()
def _main() -> None:
    """Trading — research and paper-trading CLI.

    Empty callback so Typer treats `trading` as a multi-command app (otherwise
    a single-command app flattens and rejects the subcommand name).
    """


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


@app.command("kite-login")
def kite_login(
    request_token: Annotated[
        str | None,
        typer.Option(
            "--request-token",
            "-t",
            help="Skip the interactive prompt and pass the request_token directly.",
        ),
    ] = None,
) -> None:
    """Interactive Kite Connect login. Prints the URL, accepts the request_token,
    fetches a fresh access token, and writes it to `.env`."""
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

    ctx = ScanContext(scan_date=scan_date)
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

        watched = sorted(DEFAULT_ALIASES.keys())
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


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}"


if __name__ == "__main__":  # pragma: no cover — manual entry
    app()
