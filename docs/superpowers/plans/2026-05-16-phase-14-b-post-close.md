# Phase 14.B — post_close MVP: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire end-of-day MTM + reconcile + summary markdown end-to-end. Two-phase Python (`prepare`/`apply`) wrapping the existing `/kite-quotes-snapshot` skill, calling `paper.mtm.mtm_open_trades` + `paper.reconcile.reconcile_day` unchanged, and writing a numbers-only `post_close_summary.md` that `compile_brief` opportunistically picks up.

**Architecture:** Same file-handshake pattern as Phase 14.A. `jobs/post_close.py` reuses `gather_quote_symbols` and `_quotes_to_bars` from `jobs/mid_day.py` (one-line imports — no new abstraction). Compile_brief gains a single new opportunistic-include for `post_close_summary.md` after the existing `mid_day_update.md` include.

**Tech Stack:** Python 3.11 · sqlite3 · `typer` (CLI) · `pytest` · existing project modules (Phases 1–14.A). No new MCP tool, no new skill.

**Spec:** [`docs/superpowers/specs/2026-05-16-phase-14-b-post-close-design.md`](../specs/2026-05-16-phase-14-b-post-close-design.md)

---

## File structure

| Path | Created/Modified | Responsibility |
|------|------------------|----------------|
| `src/trading/jobs/post_close.py` | Create | `PostCloseAborted`, `PostCloseResult`, `run_post_close(prepare/apply)`, `_render_post_close_summary`, `_main` |
| `tests/test_jobs_post_close.py` | Create | prepare + apply (with TIME-stop closure) + abort + idempotency + markdown-shape tests |
| `src/trading/jobs/__init__.py` | Modify | Re-export `PostCloseAborted`, `PostCloseResult`, `run_post_close` |
| `src/trading/cli.py` | Modify | New `trading post-close --date YYYY-MM-DD [--apply] [--cash N]` subcommand |
| `tests/test_cli.py` | Modify | prepare + apply-happy + apply-aborts |
| `src/trading/llm/briefing.py` | Modify | Opportunistic include for `post_close_summary.md` after `mid_day_update.md` |
| `tests/test_llm_briefing.py` | Modify | One test — both summaries present and rendered in order |
| `scripts/post_close.bat` | Create | Two-step Windows launcher |
| `PROGRESS.md` | Modify | Mark 14.B done; insert sub-task block; update pointers |

---

## Task 1: `jobs/post_close.py` + tests

**Files:**
- Create: `src/trading/jobs/post_close.py`
- Create: `tests/test_jobs_post_close.py`
- Modify: `src/trading/jobs/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jobs_post_close.py`:

```python
"""Tests for trading.jobs.post_close — orchestrator + helpers."""

from __future__ import annotations

import json as _j
import sqlite3
from datetime import date
from datetime import datetime as _dt
from pathlib import Path

import pytest
from freezegun import freeze_time

from tests.conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.jobs.post_close import (
    PostCloseAborted,
    PostCloseResult,
    run_post_close,
)
from trading.store.db import get_conn
from trading.store.migrations import run_migrations


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_HOLDING_ROW = {
    "tradingsymbol": "RVNL", "exchange": "NSE", "isin": "INE415G01027",
    "quantity": 594, "average_price": 328.0, "last_price": 283.0,
    "close_price": 287.2, "pnl": -26851.0, "day_change": -4.2,
    "day_change_percentage": -1.46,
}


_QUOTE_ROW_RVNL_TIME = {
    "instrument_token": 2445313,
    "last_price": 290.0,           # > current_stop=289 so STOP doesn't fire
    "volume": 100,
    "open": 287.0, "high": 291.0, "low": 286.0, "close": 287.2,
    "bid": 289.9, "ask": 290.1, "oi": None,
    "upper_circuit_limit": None, "lower_circuit_limit": None,
    "tradingsymbol": "RVNL",
}


def _seed_open_trade_at_day_24(conn: sqlite3.Connection) -> None:
    """Trade with days_held=24 so the +1 in mtm_open_trades pushes it to 25 (TIME exit)."""
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-04-21T08:30:00", "RVNL", 305.0, 289.0, 366.0),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, "
        "current_stop, atr_at_entry, days_held) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sig_id, "2026-04-21T08:30:00", 305.0, 32, 289.0, 8.4, 24),
    )
    conn.commit()


def _write_quotes(paths, as_of, hhmm: str, rows: list) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(_j.dumps(rows), encoding="utf-8")
    return target


def test_run_post_close_prepare_writes_symbol_file(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    seed_kite_snapshot(paths, date(2026, 5, 16), holdings=[_HOLDING_ROW])
    result = run_post_close(date(2026, 5, 16), paths=paths, apply=False)
    assert isinstance(result, PostCloseResult)
    assert result.symbols_path is not None
    assert result.symbols_path.is_file()
    body = result.symbols_path.read_text(encoding="utf-8")
    assert "RVNL" in body
    assert result.summary_path is None
    assert result.trades_evaluated == 0


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_closes_time_stop_and_writes_summary(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(
        date(2026, 5, 16), paths=paths, apply=True, cash=100_000.0
    )
    assert isinstance(result, PostCloseResult)
    assert result.quotes_capture_ts == _dt(2026, 5, 16, 16, 1)
    assert result.bars_built == 1
    assert result.trades_evaluated == 1
    assert result.trades_closed == 1   # day 24+1=25 → TIME exit
    # paper_trade is now closed
    with get_conn(paths.db_path) as file_conn:
        closed = file_conn.execute(
            "SELECT exit_reason FROM paper_trades WHERE ts_exit IS NOT NULL"
        ).fetchone()
    assert closed["exit_reason"] == "TIME"
    # portfolio snapshot row written
    with get_conn(paths.db_path) as file_conn:
        snap = file_conn.execute(
            "SELECT date, cash, equity FROM portfolio_snapshots WHERE date = ?",
            ("2026-05-16",),
        ).fetchone()
    assert snap is not None
    assert snap["cash"] == 100_000.0
    assert result.equity == snap["equity"]
    # markdown written
    assert result.summary_path is not None
    body = result.summary_path.read_text(encoding="utf-8")
    assert "## Post-close summary" in body
    assert "16:01:23" in body
    assert "RVNL" in body
    assert "EXIT_TIME" in body
    assert "Portfolio snapshot" in body
    assert "₹" in body  # equity formatted


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_aborts_when_quotes_missing(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    with pytest.raises(PostCloseAborted) as exc:
        run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert "/kite-quotes-snapshot" in str(exc.value)


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_idempotent_on_rerun(paths) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
        _seed_open_trade_at_day_24(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    r1 = run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert r1.trades_closed == 1
    # Re-run: trade already closed, snapshot UPSERT overwrites
    r2 = run_post_close(date(2026, 5, 16), paths=paths, apply=True)
    assert r2.trades_evaluated == 0
    assert r2.trades_closed == 0
    # portfolio snapshot still has exactly one row for as_of
    with get_conn(paths.db_path) as file_conn:
        n = file_conn.execute(
            "SELECT COUNT(*) FROM portfolio_snapshots WHERE date = ?",
            ("2026-05-16",),
        ).fetchone()[0]
    assert n == 1


@freeze_time("2026-05-16T16:01:23")
def test_run_post_close_apply_no_open_trades_still_writes_summary(paths) -> None:
    """Quiet day: no open trades, no matured predictions. Summary still written."""
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as file_conn:
        run_migrations(file_conn)
    _write_quotes(paths, date(2026, 5, 16), "1601", [_QUOTE_ROW_RVNL_TIME])
    result = run_post_close(
        date(2026, 5, 16), paths=paths, apply=True, cash=100_000.0
    )
    assert result.bars_built == 1
    assert result.trades_evaluated == 0
    assert result.trades_closed == 0
    assert result.predictions_matured == 0
    body = result.summary_path.read_text(encoding="utf-8")
    assert "0 open trades evaluated" in body
    assert "_(none today)_" in body  # matured predictions section
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_post_close.py -v`
Expected: FAIL — `ModuleNotFoundError: trading.jobs.post_close`.

- [ ] **Step 3: Write the implementation**

Create `src/trading/jobs/post_close.py`:

```python
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


def _main(  # pragma: no cover — manual entry
    date_str: str,
    apply: bool = False,
    cash: float = 100_000.0,
) -> None:
    """`python -m trading.jobs.post_close <YYYY-MM-DD> [--apply] [--cash N]` entry."""
    try:
        result = run_post_close(
            date.fromisoformat(date_str), apply=apply, cash=cash
        )
    except PostCloseAborted as e:
        print(f"Post-close aborted: {e}")
        raise SystemExit(2) from e
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
```

Update `src/trading/jobs/__init__.py`:

```python
"""Top-level jobs package — orchestrators that wire phases together."""

from trading.jobs.mid_day import MidDayAborted, MidDayResult, run_mid_day
from trading.jobs.post_close import (
    PostCloseAborted,
    PostCloseResult,
    run_post_close,
)
from trading.jobs.pre_open import PreOpenResult, run_pre_open

__all__ = [
    "MidDayAborted",
    "MidDayResult",
    "PostCloseAborted",
    "PostCloseResult",
    "PreOpenResult",
    "run_mid_day",
    "run_post_close",
    "run_pre_open",
]
```

- [ ] **Step 4: Run tests + lint + types**

```bash
cd D:/Projects/Trading
uv run pytest tests/test_jobs_post_close.py -v
```

Expected: 5 passing.

```bash
cd D:/Projects/Trading
uv run ruff check src/trading/jobs/post_close.py tests/test_jobs_post_close.py
uv run mypy src/trading/jobs/post_close.py
```

Expected: both clean.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/post_close.py src/trading/jobs/__init__.py tests/test_jobs_post_close.py
git commit -m "feat(jobs): post_close orchestrator + summary markdown (14.B.1)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: CLI `trading post-close`

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_post_close_cli_prepare_writes_symbol_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["post-close", "--date", "2026-05-16"]
    )
    assert result.exit_code == 0, result.stdout
    out_path = tmp_path / "data" / "raw" / "2026-05-16" / "_quote_symbols.txt"
    assert out_path.is_file()
    assert "/kite-quotes-snapshot" in result.stdout


def test_post_close_cli_apply_aborts_when_quotes_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["post-close", "--date", "2026-05-16", "--apply"]
    )
    assert result.exit_code == 2, result.stdout
    assert "/kite-quotes-snapshot" in result.stdout


def test_post_close_cli_apply_happy_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Stub run_post_close to verify exit-code + summary-line."""
    from datetime import date as _d, datetime as _dt
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)

    fake_summary = tmp_path / "data" / "research" / "2026-05-16" / "post_close_summary.md"
    fake_summary.parent.mkdir(parents=True, exist_ok=True)
    fake_summary.write_text("stub", encoding="utf-8")

    from trading.jobs import post_close as pc_mod
    fake_result = pc_mod.PostCloseResult(
        as_of=_d(2026, 5, 16),
        quotes_capture_ts=_dt(2026, 5, 16, 16, 1),
        bars_built=11, trades_evaluated=2, trades_closed=1,
        trades_held=1, trades_skipped=0, predictions_matured=2,
        equity=527341.0, drawdown_pct=-1.2,
        summary_path=fake_summary, symbols_path=None, warnings=[],
    )
    monkeypatch.setattr(
        "trading.cli.run_post_close", lambda *a, **kw: fake_result
    )
    result = runner.invoke(
        app, ["post-close", "--date", "2026-05-16", "--apply"]
    )
    assert result.exit_code == 0, result.stdout
    assert "evaluated" in result.stdout or "trades_evaluated" in result.stdout
    assert "post_close_summary.md" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v -k post_close`
Expected: FAIL — no such subcommand.

- [ ] **Step 3: Add the CLI command**

In `src/trading/cli.py`, add the import (next to the `mid_day` import):

```python
from trading.jobs.post_close import PostCloseAborted, run_post_close
```

After the `mid_day_cmd` function and before `if __name__ == "__main__":`, add:

```python
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
    cash: Annotated[
        float,
        typer.Option(help="Paper-cash balance for portfolio snapshot."),
    ] = 100_000.0,
) -> None:
    """Phase 14.B — end-of-day MTM + reconcile + summary."""
    as_of = date.fromisoformat(date_str)
    try:
        result = run_post_close(as_of, apply=apply, cash=cash)
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
    table.add_row("equity", f"Rs {result.equity:,.0f}" if result.equity else "—")
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
```

- [ ] **Step 4: Run tests**

```bash
cd D:/Projects/Trading
uv run pytest tests/test_cli.py -v -k post_close
```

Expected: 3 passing.

Also: `uv run trading post-close --help` should list the command.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading post-close [--apply] [--cash N] subcommand (14.B.2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: `briefing.py` opportunistic include for `post_close_summary.md`

**Files:**
- Modify: `src/trading/llm/briefing.py`
- Modify: `tests/test_llm_briefing.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_briefing.py`:

```python
def test_compile_brief_includes_post_close_summary_after_mid_day(
    tmp_path: Path,
) -> None:
    """Both mid_day_update.md and post_close_summary.md are opportunistically
    included; ordering must be mid_day FIRST, post_close SECOND."""
    date_dir = tmp_path / "2026-05-16"
    date_dir.mkdir()
    _write_part(
        date_dir, "_context.md",
        "# Trading context bundle — 2026-05-16  (mode: pre_open)\n"
        "\n## Today's candidates\n\n### RVNL — passes 9/10 rules\n",
    )
    _write_part(date_dir, "macro_brief.md", "Regime: NEUTRAL.\n")
    _write_part(date_dir, "candidates/RVNL.md", "# RVNL — Conviction: HIGH\n")
    _write_part(
        date_dir, "mid_day_update.md",
        "## Mid-day update — captured 2026-05-16T12:32:14\n\nMID-DAY-CONTENT\n",
    )
    _write_part(
        date_dir, "post_close_summary.md",
        "## Post-close summary — captured 2026-05-16T16:01:23\n\nPOST-CLOSE-CONTENT\n",
    )
    out = compile_brief(date_dir, mode="pre_open")
    body = out.read_text(encoding="utf-8")
    assert "MID-DAY-CONTENT" in body
    assert "POST-CLOSE-CONTENT" in body
    # Order: mid-day comes BEFORE post-close
    assert body.index("MID-DAY-CONTENT") < body.index("POST-CLOSE-CONTENT")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_llm_briefing.py -v -k post_close_summary`
Expected: FAIL — `POST-CLOSE-CONTENT` not in body (briefing doesn't read the file yet).

- [ ] **Step 3: Implement the include**

In `src/trading/llm/briefing.py`, find the existing mid_day_update include (added in Phase 14.A.5):

```python
    # Optional mid-day update (Phase 14.A): included when present, regardless of mode.
    mid_day_path = date_dir / "mid_day_update.md"
    if mid_day_path.is_file():
        sections.append("")
        sections.append(mid_day_path.read_text(encoding="utf-8").strip())
```

Add the post_close_summary include immediately after it:

```python
    # Optional post-close summary (Phase 14.B): same pattern, regardless of mode.
    post_close_summary_path = date_dir / "post_close_summary.md"
    if post_close_summary_path.is_file():
        sections.append("")
        sections.append(
            post_close_summary_path.read_text(encoding="utf-8").strip()
        )
```

- [ ] **Step 4: Run all briefing tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_llm_briefing.py -v`
Expected: all passing (existing 10 + 1 new = 11).

```bash
cd D:/Projects/Trading
git add src/trading/llm/briefing.py tests/test_llm_briefing.py
git commit -m "feat(llm): compile_brief picks up post_close_summary.md too (14.B.3)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: `scripts/post_close.bat` launcher

**Files:**
- Create: `scripts/post_close.bat`

- [ ] **Step 1: Create the launcher**

Create `scripts/post_close.bat`:

```bat
@echo off
REM Phase 14.B two-step launcher.
REM Usage: post_close.bat YYYY-MM-DD prepare
REM        post_close.bat YYYY-MM-DD apply
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: post_close.bat YYYY-MM-DD {prepare^|apply} & exit /b 2)
if "%~2"=="apply" (
  uv run python -m trading.jobs.post_close %1 --apply
) else (
  uv run python -m trading.jobs.post_close %1
)
```

- [ ] **Step 2: Smoke-test the `__main__` invocation**

```bash
cd D:/Projects/Trading
uv run python -m trading.jobs.post_close 2026-05-16 2>&1 | tail -5
```

Expected: writes `data/raw/2026-05-16/_quote_symbols.txt`, prints
"wrote …" + "Now run /kite-quotes-snapshot skill, then re-run with
--apply".

- [ ] **Step 3: Commit**

```bash
cd D:/Projects/Trading
git add scripts/post_close.bat
git commit -m "feat(jobs): post_close.bat Windows launcher (14.B.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Real-data smoke + PROGRESS.md + commit + push

**Files:**
- Modify: `PROGRESS.md`
- Mutates: `data/raw/2026-05-16/`, `data/research/2026-05-16/`, `data/app.db` (all gitignored)

- [ ] **Step 1: Capture before-state**

```bash
cd D:/Projects/Trading
uv run python -c "
import sqlite3
c = sqlite3.connect('data/app.db'); c.row_factory = sqlite3.Row
print('open paper-trades:', c.execute('select count(*) from paper_trades where ts_exit is null').fetchone()[0])
print('portfolio_snapshots for 2026-05-16:', c.execute(\"select count(*) from portfolio_snapshots where date = '2026-05-16'\").fetchone()[0])
"
```

Record the counts.

- [ ] **Step 2: Run prepare**

```bash
cd D:/Projects/Trading
uv run trading post-close --date 2026-05-16 2>&1 | tail -5
```

Expected: writes `_quote_symbols.txt`, prints next-step instruction.

- [ ] **Step 3: Refresh quotes via /kite-quotes-snapshot skill**

In Claude Code: `/kite-quotes-snapshot`

Skill (assistant): probes `mcp__kite__get_profile` (auth check), reads
`data/raw/2026-05-16/_quote_symbols.txt`, calls
`mcp__kite__get_quotes` for each symbol with the appropriate
`NSE:` / `BSE:` prefix, writes `data/raw/2026-05-16/quotes_HHMM.json`,
updates `_meta.json` with `quotes_at`.

If MCP auth fails, the skill prompts the user to run
`mcp__kite__login`, then re-invoke `/kite-quotes-snapshot`.

Verify:

```bash
cd D:/Projects/Trading && ls -la data/raw/2026-05-16/quotes_*.json
cat data/raw/2026-05-16/_meta.json
```

- [ ] **Step 4: Run apply**

```bash
cd D:/Projects/Trading
uv run trading post-close --date 2026-05-16 --apply 2>&1 | tail -25
```

Expected: Rich table with quotes_captured_at, bars_built,
trades_evaluated/closed/held/skipped, predictions_matured, equity,
drawdown_pct. "wrote post_close_summary.md" line.

- [ ] **Step 5: Inspect the summary markdown**

```bash
cd D:/Projects/Trading && cat data/research/2026-05-16/post_close_summary.md
```

Expected: well-formed markdown with capture timestamp + Final MTM
table + Portfolio snapshot section + Matured predictions table (or
"_(none today)_") + summary line.

- [ ] **Step 6: Verify portfolio_snapshots row**

```bash
cd D:/Projects/Trading
uv run python -c "
import sqlite3
c = sqlite3.connect('data/app.db'); c.row_factory = sqlite3.Row
r = c.execute(\"select * from portfolio_snapshots where date = '2026-05-16'\").fetchone()
print(dict(r) if r else 'no snapshot')
"
```

Expected: row with date, cash, equity, drawdown_pct fields populated.

- [ ] **Step 7: Run full verification**

```bash
cd D:/Projects/Trading
uv run ruff check . && uv run mypy src/ && uv run pytest -q
```

Expected: clean. Test count: ~503 passed (493 + 10 new).

- [ ] **Step 8: Update PROGRESS.md**

In `PROGRESS.md`, in the status snapshot table, add:

```
| 14.B | post_close MVP | `[x]` |
```

immediately after the existing 14.A row.

After the Phase 14.A body block, before `## Phase 14 — mid_day +
post_close jobs`, insert:

```markdown
## Phase 14.B — post_close MVP

> Spec at [`docs/superpowers/specs/2026-05-16-phase-14-b-post-close-design.md`](docs/superpowers/specs/2026-05-16-phase-14-b-post-close-design.md).
> Plan at [`docs/superpowers/plans/2026-05-16-phase-14-b-post-close.md`](docs/superpowers/plans/2026-05-16-phase-14-b-post-close.md).
> Reuses 14.A `/kite-quotes-snapshot` skill, `paper.mtm.mtm_open_trades`,
> and `paper.reconcile.reconcile_day` unchanged.

- [x] 14.B.1 `src/trading/jobs/post_close.py`: `PostCloseAborted` +
       `PostCloseResult` + `run_post_close(prepare/apply)` orchestrator;
       `_render_post_close_summary` markdown builder; reuses
       `gather_quote_symbols` + `_quotes_to_bars` from `mid_day`. Calls
       `paper.mtm.mtm_open_trades` for final MTM and
       `paper.reconcile.reconcile_day` for matured predictions +
       portfolio snapshot. 5 new tests including TIME-stop closure +
       idempotent re-run + quiet-day case.
- [x] 14.B.2 `src/trading/cli.py`: `trading post-close --date YYYY-MM-DD
       [--apply] [--cash N]` subcommand with Rich summary table +
       remediation on abort. 3 new tests.
- [x] 14.B.3 `src/trading/llm/briefing.py`: opportunistic include for
       `post_close_summary.md` after `mid_day_update.md` (additive
       across modes). 1 new test verifying ordering.
- [x] 14.B.4 `scripts/post_close.bat`: two-step Windows launcher
       (prepare/apply).
- [x] 14.B.5 Real-data smoke: `trading post-close` prepare → MCP
       `get_quotes` for symbols → `trading post-close --apply` runs
       end-to-end. <fill in counts from Step 4 above>. Suite
       **N passed** (was 493 + ~10 new), 1 skipped (live), ruff +
       mypy clean. Commit `feat(jobs): post_close MVP (Phase 14.B)`
       pushed to origin/main.
```

- [ ] **Step 9: Commit + push**

```bash
cd D:/Projects/Trading
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(jobs): post_close MVP (Phase 14.B)

End-of-day MTM + reconcile + summary markdown wired end-to-end.
Two-phase Python (prepare / apply) reuses 14.A's /kite-quotes-snapshot
skill and Phase 11's paper.mtm + paper.reconcile unchanged. New thin
orchestrator (jobs/post_close.py) and numbers-only summary renderer.

trading post-close --date X (prepare) writes _quote_symbols.txt.
/kite-quotes-snapshot via MCP writes quotes_HHMM.json (closing OHLC).
trading post-close --date X --apply runs final MTM (closes any
TIME-stopped trades), reconcile_day (matured predictions +
portfolio_snapshots row), writes post_close_summary.md.

compile_brief picks up post_close_summary.md opportunistically after
mid_day_update.md (additive across all modes); the existing prose
post_close_recap.md from /analyst skill remains required for
mode=post_close.

Spec: docs/superpowers/specs/2026-05-16-phase-14-b-post-close-design.md
Plan: docs/superpowers/plans/2026-05-16-phase-14-b-post-close.md

Tests: <count> passed, 1 skipped (live), ruff + mypy clean.

Real-data smoke (2026-05-16): <fill in counts from Step 4>.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push origin main
```

Expected: push succeeds. Phase 14.B is shipped. Phase 14.C (pre_open_iep)
will get its own brainstorm → spec → plan cycle next.

---

## Self-review notes

- **Spec coverage:** spec §3.1 post_close.py → Task 1. §3.2 jobs/__init__.py → Task 1. §3.3 CLI → Task 2. §3.4 briefing → Task 3. §3.5 .bat → Task 4. §4 markdown shape → Task 1's `_render_post_close_summary`. §5 error handling → tests in Tasks 1, 2. §6 testing matrix → mapped 1:1. §7 sub-task breakdown → mapped to Tasks 1-5. ✓
- **Type consistency:** `PostCloseAborted`, `PostCloseResult`, `run_post_close` defined in Task 1, imported in Tasks 2, 5. `gather_quote_symbols` and `_quotes_to_bars` imported from `trading.jobs.mid_day` (already exist). `reconcile_day` returns `ReconcileResult` with `.snapshot` (PortfolioSnapshot) and `.prediction_updates` (list[PredictionUpdate]) — matched in `_render_post_close_summary`. ✓
- **Placeholder scan:** every code block is concrete; commit messages written out (one judgement-call placeholder: smoke counts in Task 5 Step 8/9 are filled in only after Step 4 actually runs). ✓
- **TDD discipline:** every task is failing-test → impl → run-tests → commit. ✓

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-16-phase-14-b-post-close.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

**Which approach?**
