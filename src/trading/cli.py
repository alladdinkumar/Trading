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


if __name__ == "__main__":  # pragma: no cover — manual entry
    app()
