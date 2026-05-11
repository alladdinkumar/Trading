"""Typer CLI entry — `uv run trading <command>`."""

from __future__ import annotations

from datetime import date
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from trading.config import get_paths
from trading.data.universe import load_universe
from trading.data.yfinance import OhlcvFetchError, fetch_ohlcv
from trading.store.ohlcv import parquet_path, write_ohlcv

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


if __name__ == "__main__":  # pragma: no cover — manual entry
    app()
