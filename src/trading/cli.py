"""Typer CLI entry — `uv run trading <command>`."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Annotated, Any

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

from trading.config import get_paths, get_settings, update_env_var
from trading.data.kite import (
    KiteAuthError,
    generate_session,
    is_authenticated,
    login_url,
    make_client,
)
from trading.data.universe import load_universe
from trading.data.yfinance import OhlcvFetchError, fetch_ohlcv
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


if __name__ == "__main__":  # pragma: no cover — manual entry
    app()
