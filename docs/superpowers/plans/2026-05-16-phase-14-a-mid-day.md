# Phase 14.A — mid_day MVP: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire mid-day MTM end-to-end: Python pre-flight writes a symbol list, a new `/kite-quotes-snapshot` skill fetches intraday quotes via MCP and writes them to disk, then Python reads them and runs `paper.mtm.mtm_open_trades` to close hits / ratchet trails / append a `mid_day_update.md` to the daily brief.

**Architecture:** Two-phase Python (`prepare` / `apply`) wrapping a Claude Code skill in the middle. Same file-handshake pattern as Phase 12 (analyst) and Phase 13.5 (kite-snapshot). All Kite I/O via MCP; SDK fallback unwired. Reuses `paper.mtm.mtm_open_trades` and `data.kite.Quote` unchanged.

**Tech Stack:** Python 3.11 · sqlite3 · `typer` (CLI) · `pytest` · existing project modules (Phases 1–13.5). Skill side: Claude Code MCP tools (`mcp__kite__get_quotes`, `mcp__kite__get_profile`).

**Spec:** [`docs/superpowers/specs/2026-05-16-phase-14-a-mid-day-design.md`](../specs/2026-05-16-phase-14-a-mid-day-design.md)

---

## File structure

| Path | Created/Modified | Responsibility |
|------|------------------|----------------|
| `src/trading/data/quotes_snapshot.py` | Create | `read_latest_quotes(paths, as_of)` + `QuoteSnapshotMissingError` + `QuoteSnapshotStaleError`. Pure JSON-to-Quote reader. |
| `tests/test_quotes_snapshot.py` | Create | Happy / missing / stale / multi-snapshot / empty-list tests |
| `.claude/skills/kite-quotes-snapshot/SKILL.md` | Create | Reads `_quote_symbols.txt`, writes `quotes_HHMM.json`, updates `_meta.quotes_at` |
| `src/trading/jobs/mid_day.py` | Create | `MidDayResult`, `MidDayAborted`, `gather_quote_symbols`, `_quotes_to_bars`, `_render_mid_day_update`, `run_mid_day(apply=False/True)`, `_main` typer entry |
| `tests/test_jobs_mid_day.py` | Create | gather + bars + prepare + apply + abort + idempotency tests |
| `src/trading/cli.py` | Modify | New `trading mid-day --date YYYY-MM-DD [--apply]` subcommand |
| `tests/test_cli.py` | Modify | prepare-mode + apply-mode + abort-on-missing tests |
| `src/trading/llm/briefing.py` | Modify | Extend `Mode` to include `"mid_day"`; treat `mid_day_update.md` as optional; insert it after Candidates section |
| `src/trading/llm/context.py` | Modify | Same `Mode` literal extension |
| `tests/test_llm_briefing.py` | Modify | `compile_brief(mode="mid_day")` appends update when present + still raises on missing required parts |
| `scripts/mid_day.bat` | Create | Two-step Windows launcher (prepare/apply) |
| `PROGRESS.md` | Modify | Mark 14.A done; insert sub-task block; update pointers |

---

## Task 1: `quotes_snapshot.py` + reader + typed errors

**Files:**
- Create: `src/trading/data/quotes_snapshot.py`
- Create: `tests/test_quotes_snapshot.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_quotes_snapshot.py`:

```python
"""Tests for trading.data.quotes_snapshot — JSON readers + typed errors."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from trading.config import get_paths
from trading.data.kite import Quote
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_QUOTE_ROW = {
    "instrument_token": 2977281,
    "last_price": 395.25,
    "volume": 8123456,
    "open": 396.30, "high": 397.10, "low": 393.80, "close": 396.30,
    "bid": 395.20, "ask": 395.30,
    "oi": None,
    "upper_circuit_limit": 435.93, "lower_circuit_limit": 356.67,
    "tradingsymbol": "NTPC",
}


def _seed_quotes(paths, as_of: date, hhmm: str, rows: list) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(json.dumps(rows), encoding="utf-8")
    return target


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_happy_path(paths) -> None:
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert "NTPC" in quotes
    assert isinstance(quotes["NTPC"], Quote)
    assert quotes["NTPC"].last_price == 395.25
    assert capture_ts == datetime(2026, 5, 16, 12, 32)


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_picks_newest_when_multiple(paths) -> None:
    older = dict(_QUOTE_ROW); older["last_price"] = 390.0
    _seed_quotes(paths, date(2026, 5, 16), "1100", [older])
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    quotes, capture_ts = read_latest_quotes(paths, date(2026, 5, 16))
    assert quotes["NTPC"].last_price == 395.25
    assert capture_ts == datetime(2026, 5, 16, 12, 32)


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_missing_raises(paths) -> None:
    with pytest.raises(QuoteSnapshotMissingError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    assert "/kite-quotes-snapshot" in str(exc.value)


@freeze_time("2026-05-16T15:30:00")
def test_read_latest_quotes_stale_raises(paths) -> None:
    """Newest snapshot is from 12:32; default max_age is 30 min, now is 15:30."""
    _seed_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW])
    with pytest.raises(QuoteSnapshotStaleError) as exc:
        read_latest_quotes(paths, date(2026, 5, 16))
    msg = str(exc.value)
    assert "stale" in msg.lower()
    assert "12:32" in msg or "1232" in msg


@freeze_time("2026-05-16T12:33:00")
def test_read_latest_quotes_empty_list_returns_empty_dict(paths) -> None:
    _seed_quotes(paths, date(2026, 5, 16), "1232", [])
    quotes, _ = read_latest_quotes(paths, date(2026, 5, 16))
    assert quotes == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_quotes_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: trading.data.quotes_snapshot`.

- [ ] **Step 3: Write the implementation**

Create `src/trading/data/quotes_snapshot.py`:

```python
"""Phase 14.A — readers for intraday quote snapshots written by /kite-quotes-snapshot.

Production code reads quotes through this module, never the SDK. The
filename's HHMM component is the single source of truth for capture
time (`_meta.quotes_at` is informational only). Staleness is measured
against real-time `datetime.now()` — a snapshot is "stale" if more
than `max_age_minutes` of wall-clock time has passed since capture.
Each row's `tradingsymbol` field is popped before splatting into
`Quote(**row)` since the dataclass uses `instrument_token` and we
key the dict by symbol externally.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from trading.config import Paths
from trading.data.kite import Quote

_FILENAME_RE = re.compile(r"^quotes_(\d{4})\.json$")


class QuoteSnapshotMissingError(RuntimeError):
    """Raised when no quotes_*.json file exists for the requested date."""


class QuoteSnapshotStaleError(RuntimeError):
    """Raised when the newest quotes_*.json is older than max_age_minutes."""


def read_latest_quotes(
    paths: Paths,
    as_of: date,
    *,
    max_age_minutes: int = 30,
) -> tuple[dict[str, Quote], datetime]:
    """Find the most recent quotes_HHMM.json for `as_of`, parse → dict[symbol, Quote].

    Returns (quotes_by_symbol, capture_ts). Raises:
      - KiteSnapshotMissingError: no quotes_*.json present for the date
      - KiteSnapshotStaleError: newest exists but capture > max_age_minutes ago
    """
    date_dir = paths.raw_dir / as_of.isoformat()
    if not date_dir.is_dir():
        raise QuoteSnapshotMissingError(
            f"No quotes for {as_of.isoformat()} (directory absent: {date_dir}). "
            "Run /kite-quotes-snapshot skill in Claude Code first."
        )
    candidates: list[tuple[str, Path]] = []
    for f in date_dir.iterdir():
        m = _FILENAME_RE.match(f.name)
        if m:
            candidates.append((m.group(1), f))
    if not candidates:
        raise QuoteSnapshotMissingError(
            f"No quotes_HHMM.json files in {date_dir}. "
            "Run /kite-quotes-snapshot skill in Claude Code first."
        )
    candidates.sort(key=lambda x: x[0])  # ascending HHMM
    hhmm, target = candidates[-1]
    capture_ts = datetime(
        as_of.year, as_of.month, as_of.day,
        int(hhmm[:2]), int(hhmm[2:]),
    )
    age = datetime.now() - capture_ts
    if age > timedelta(minutes=max_age_minutes):
        raise QuoteSnapshotStaleError(
            f"Newest quotes snapshot is stale: captured at {hhmm[:2]}:{hhmm[2:]} "
            f"({int(age.total_seconds() // 60)} min ago, max {max_age_minutes}). "
            "Re-run /kite-quotes-snapshot skill."
        )
    rows: list[dict] = json.loads(target.read_text(encoding="utf-8"))
    quotes: dict[str, Quote] = {}
    for row in rows:
        sym = row.pop("tradingsymbol")
        quotes[sym] = Quote(**row)
    return quotes, capture_ts
```

- [ ] **Step 4: Run tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_quotes_snapshot.py -v`
Expected: 5 passing.

```bash
cd D:/Projects/Trading
git add src/trading/data/quotes_snapshot.py tests/test_quotes_snapshot.py
git commit -m "feat(data): quotes_snapshot.read_latest_quotes + typed errors (14.A.1)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: `jobs/mid_day.py` skeleton + `gather_quote_symbols` + prepare mode

**Files:**
- Create: `src/trading/jobs/mid_day.py`
- Create: `tests/test_jobs_mid_day.py`
- Modify: `src/trading/jobs/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jobs_mid_day.py`:

```python
"""Tests for trading.jobs.mid_day — orchestrator + helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from tests.conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.jobs.mid_day import (
    MidDayAborted,
    MidDayResult,
    gather_quote_symbols,
    run_mid_day,
)
from trading.store.migrations import run_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_HOLDING_ROW = {
    "tradingsymbol": "COALINDIA", "exchange": "NSE", "isin": "INE522F01014",
    "quantity": 69, "average_price": 463.68, "last_price": 462.2,
    "close_price": 454.05, "pnl": -102.25, "day_change": 8.15,
    "day_change_percentage": 1.79,
}


def _seed_open_trade(conn: sqlite3.Connection, symbol: str = "RVNL") -> None:
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-05-15T08:30:00", symbol, 305.0, 290.0, 360.0),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, "
        "current_stop, atr_at_entry) VALUES (?, ?, ?, ?, ?, ?)",
        (sig_id, "2026-05-15T08:30:00", 305.0, 32, 295.0, 8.4),
    )
    conn.commit()


def test_gather_quote_symbols_unions_paper_signals_holdings(
    conn, paths
) -> None:
    _seed_open_trade(conn, "RVNL")
    conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-05-16T08:30:00", "NTPC", 395.0, 380.0, 470.0),
    )
    conn.commit()
    seed_kite_snapshot(paths, date(2026, 5, 16), holdings=[_HOLDING_ROW])
    out = gather_quote_symbols(conn, paths, date(2026, 5, 16))
    assert out == sorted({"RVNL", "NTPC", "COALINDIA"})


def test_gather_quote_symbols_degrades_when_holdings_missing(
    conn, paths
) -> None:
    _seed_open_trade(conn, "RVNL")
    out = gather_quote_symbols(conn, paths, date(2026, 5, 16))
    assert out == ["RVNL"]


def test_run_mid_day_prepare_writes_symbol_file(conn, paths) -> None:
    _seed_open_trade(conn, "RVNL")
    seed_kite_snapshot(paths, date(2026, 5, 16), holdings=[_HOLDING_ROW])
    result = run_mid_day(date(2026, 5, 16), paths=paths, apply=False)
    assert isinstance(result, MidDayResult)
    assert result.symbols_path is not None
    assert result.symbols_path.is_file()
    body = result.symbols_path.read_text(encoding="utf-8")
    assert body.split("\n") == ["COALINDIA", "RVNL", ""]  # sorted, trailing newline
    assert result.update_path is None  # apply mode didn't run
    assert result.trades_evaluated == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_mid_day.py -v`
Expected: FAIL — `ModuleNotFoundError: trading.jobs.mid_day`.

- [ ] **Step 3: Write the skeleton + helpers**

Create `src/trading/jobs/mid_day.py`:

```python
"""Phase 14.A — mid_day MVP orchestrator.

Two-phase invocation:
  prepare → write data/raw/<as_of>/_quote_symbols.txt
  /kite-quotes-snapshot skill (out-of-process) → write quotes_HHMM.json
  apply → read quotes → mtm_open_trades → write mid_day_update.md
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trading.config import Paths, get_paths
from trading.data.kite_snapshot import KiteSnapshotMissingError, read_holdings
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.repo import list_signals_by_date


class MidDayAborted(RuntimeError):  # noqa: N818 — "Aborted" is a state
    """Raised when run_mid_day cannot proceed (analogue of PreOpenAborted)."""


@dataclass(frozen=True)
class MidDayResult:
    as_of: date
    quotes_capture_ts: datetime | None
    bars_built: int
    trades_evaluated: int
    trades_closed: int
    trades_held: int
    trades_skipped: int
    update_path: Path | None
    symbols_path: Path | None
    warnings: list[str] = field(default_factory=list)


def gather_quote_symbols(
    conn: sqlite3.Connection, paths: Paths, as_of: date
) -> list[str]:
    """Sorted, deduped union of: open paper-trade symbols ∪ today's
    signals ∪ holdings.json symbols. Holdings degrade silently if
    snapshot is absent.
    """
    symbols: set[str] = set()
    rows = conn.execute(
        "SELECT DISTINCT s.symbol FROM paper_trades pt "
        "JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL"
    ).fetchall()
    for r in rows:
        symbols.add(r["symbol"])
    for sig in list_signals_by_date(conn, as_of.isoformat()):
        symbols.add(sig.symbol)
    try:
        for h in read_holdings(paths, as_of):
            symbols.add(h.tradingsymbol)
    except KiteSnapshotMissingError:
        pass  # holdings optional
    return sorted(symbols)


def run_mid_day(
    as_of: date,
    *,
    paths: Paths | None = None,
    apply: bool = False,
) -> MidDayResult:
    """Orchestrate mid_day. apply=False → prepare mode (writes symbol file).
    apply=True → reads quotes + mtm + writes markdown (Task 3 wires this).
    """
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
            return MidDayResult(
                as_of=as_of,
                quotes_capture_ts=None,
                bars_built=0,
                trades_evaluated=0,
                trades_closed=0,
                trades_held=0,
                trades_skipped=0,
                update_path=None,
                symbols_path=symbols_path,
                warnings=warnings,
            )

        # apply mode wired in Task 3
        raise NotImplementedError("apply mode wired in Task 3")
```

Modify `src/trading/jobs/__init__.py` to also export the new symbols:

```python
"""Top-level jobs package — orchestrators that wire phases together."""

from trading.jobs.mid_day import MidDayAborted, MidDayResult, run_mid_day
from trading.jobs.pre_open import PreOpenResult, run_pre_open

__all__ = [
    "MidDayAborted",
    "MidDayResult",
    "PreOpenResult",
    "run_mid_day",
    "run_pre_open",
]
```

- [ ] **Step 4: Run tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_mid_day.py -v`
Expected: 3 passing.

```bash
cd D:/Projects/Trading
git add src/trading/jobs/mid_day.py src/trading/jobs/__init__.py tests/test_jobs_mid_day.py
git commit -m "feat(jobs): mid_day skeleton + gather_quote_symbols + prepare mode (14.A.3a)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: `_quotes_to_bars` + `run_mid_day(apply)` + markdown

**Files:**
- Modify: `src/trading/jobs/mid_day.py`
- Modify: `tests/test_jobs_mid_day.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs_mid_day.py`:

```python
import json as _j
from datetime import datetime as _dt

from freezegun import freeze_time

from trading.data.kite import Quote
from trading.jobs.mid_day import _quotes_to_bars
from trading.strategy.exits import Bar


_QUOTE_ROW_RVNL = {
    "instrument_token": 2445313,
    "last_price": 280.0,           # below current_stop=295 → triggers EXIT_STOP
    "volume": 100,
    "open": 305.0, "high": 305.5, "low": 280.0, "close": 305.0,
    "bid": 279.9, "ask": 280.1, "oi": None,
    "upper_circuit_limit": None, "lower_circuit_limit": None,
    "tradingsymbol": "RVNL",
}


def _write_quotes(paths, as_of: date, hhmm: str, rows: list) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(_j.dumps(rows), encoding="utf-8")
    return target


def test_quotes_to_bars_uses_last_price_as_close() -> None:
    q = Quote(
        instrument_token=1, last_price=395.25, volume=8123456,
        open=396.30, high=397.10, low=393.80, close=396.30,
        bid=395.20, ask=395.30, oi=None,
        upper_circuit_limit=None, lower_circuit_limit=None,
    )
    bars = _quotes_to_bars({"NTPC": q})
    assert bars["NTPC"] == Bar(
        open=396.30, high=397.10, low=393.80, close=395.25,
    )


@freeze_time("2026-05-16T12:33:00")
def test_run_mid_day_apply_closes_stop_hit_and_writes_markdown(
    conn, paths
) -> None:
    _seed_open_trade(conn, "RVNL")
    _write_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW_RVNL])
    result = run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert isinstance(result, MidDayResult)
    assert result.quotes_capture_ts == _dt(2026, 5, 16, 12, 32)
    assert result.bars_built == 1
    assert result.trades_evaluated == 1
    assert result.trades_closed == 1
    assert result.trades_held == 0
    # paper_trade is now closed
    closed = conn.execute(
        "SELECT exit_reason, exit_price FROM paper_trades WHERE ts_exit IS NOT NULL"
    ).fetchone()
    assert closed["exit_reason"] == "STOP"
    assert closed["exit_price"] is not None
    # markdown written
    assert result.update_path is not None
    body = result.update_path.read_text(encoding="utf-8")
    assert "## Mid-day update" in body
    assert "RVNL" in body
    assert "EXIT_STOP" in body


@freeze_time("2026-05-16T12:33:00")
def test_run_mid_day_apply_aborts_when_quotes_missing(conn, paths) -> None:
    _seed_open_trade(conn, "RVNL")
    with pytest.raises(MidDayAborted) as exc:
        run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert "/kite-quotes-snapshot" in str(exc.value)


@freeze_time("2026-05-16T12:33:00")
def test_run_mid_day_apply_idempotent_on_rerun(conn, paths) -> None:
    _seed_open_trade(conn, "RVNL")
    _write_quotes(paths, date(2026, 5, 16), "1232", [_QUOTE_ROW_RVNL])
    r1 = run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert r1.trades_closed == 1
    # Re-run: trade already closed → mtm_open_trades sees no open trades
    r2 = run_mid_day(date(2026, 5, 16), paths=paths, apply=True)
    assert r2.trades_evaluated == 0
    assert r2.trades_closed == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_mid_day.py -v`
Expected: 4 failures (skeleton raises NotImplementedError on apply, _quotes_to_bars not exported).

- [ ] **Step 3: Implement apply mode + helpers**

In `src/trading/jobs/mid_day.py`, add the imports near the top:

```python
from trading.data.kite import Quote
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)
from trading.paper.mtm import MtmResult, mtm_open_trades
from trading.strategy.exits import Bar
```

Add helpers (above `run_mid_day`):

```python
def _quotes_to_bars(quotes: dict[str, Quote]) -> dict[str, Bar]:
    """Translate Quote → Bar. close = last_price (current LTP), NOT
    quote.close (which is yesterday's close in Kite's convention).
    """
    return {
        sym: Bar(
            open=q.open, high=q.high, low=q.low, close=q.last_price,
        )
        for sym, q in quotes.items()
    }


def _render_mid_day_update(
    capture_ts: datetime, results: list[MtmResult]
) -> str:
    closed = [r for r in results if r.action.startswith("EXIT_")]
    held = [r for r in results if r.action == "HOLD"]
    skipped = [r for r in results if r.action == "SKIP"]

    lines = [
        f"## Mid-day update — captured {capture_ts.isoformat(timespec='seconds')}",
        "",
        "| symbol | action | exit price | reason | new stop |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        ep = f"{r.exit_price:.2f}" if r.exit_price is not None else "—"
        ns = f"{r.new_stop:.2f}" if r.new_stop is not None else "—"
        lines.append(
            f"| {r.symbol} | {r.action} | {ep} | {r.reason or '—'} | {ns} |"
        )
    lines.append("")
    lines.append(
        f"{len(results)} open trades evaluated; "
        f"{len(closed)} closed (EXIT_STOP/TARGET/TIME); "
        f"{len(held)} held; "
        f"{len(skipped)} skipped (no quote)."
    )
    return "\n".join(lines) + "\n"
```

Replace the `apply` branch in `run_mid_day`:

```python
        # apply mode
        try:
            quotes, capture_ts = read_latest_quotes(p, as_of)
        except (QuoteSnapshotMissingError, QuoteSnapshotStaleError) as e:
            raise MidDayAborted(str(e)) from e

        bars = _quotes_to_bars(quotes)
        mtm_results = mtm_open_trades(conn, bars, as_of=capture_ts)

        closed = sum(1 for r in mtm_results if r.action.startswith("EXIT_"))
        held = sum(1 for r in mtm_results if r.action == "HOLD")
        skipped = sum(1 for r in mtm_results if r.action == "SKIP")

        update_dir = p.research_dir / as_of.isoformat()
        update_dir.mkdir(parents=True, exist_ok=True)
        update_path = update_dir / "mid_day_update.md"
        update_path.write_text(
            _render_mid_day_update(capture_ts, mtm_results),
            encoding="utf-8",
        )

        return MidDayResult(
            as_of=as_of,
            quotes_capture_ts=capture_ts,
            bars_built=len(bars),
            trades_evaluated=len(mtm_results),
            trades_closed=closed,
            trades_held=held,
            trades_skipped=skipped,
            update_path=update_path,
            symbols_path=None,
            warnings=warnings,
        )
```

Also add `_main` typer entry at the bottom (mirrors pre_open.py):

```python
def _main(  # pragma: no cover — manual entry
    date_str: str,
    apply: bool = False,
) -> None:
    """`python -m trading.jobs.mid_day <YYYY-MM-DD> [--apply]` entry."""
    try:
        result = run_mid_day(date.fromisoformat(date_str), apply=apply)
    except MidDayAborted as e:
        print(f"Mid-day aborted: {e}")
        raise SystemExit(2) from e
    if result.symbols_path:
        print(f"wrote {result.symbols_path}")
        print("Now run /kite-quotes-snapshot skill, then re-run with --apply")
    if result.update_path:
        print(f"wrote {result.update_path}")
        print(
            f"trades evaluated={result.trades_evaluated} "
            f"closed={result.trades_closed} held={result.trades_held} "
            f"skipped={result.trades_skipped}"
        )


if __name__ == "__main__":  # pragma: no cover
    import typer
    typer.run(_main)
```

- [ ] **Step 4: Run tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_mid_day.py -v`
Expected: 7 passing.

```bash
cd D:/Projects/Trading
git add src/trading/jobs/mid_day.py tests/test_jobs_mid_day.py
git commit -m "feat(jobs): mid_day apply mode + _quotes_to_bars + markdown (14.A.3b)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: CLI `trading mid-day`

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_mid_day_cli_prepare_writes_symbol_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["mid-day", "--date", "2026-05-16"]
    )
    assert result.exit_code == 0, result.stdout
    out_path = tmp_path / "data" / "raw" / "2026-05-16" / "_quote_symbols.txt"
    assert out_path.is_file()
    assert "/kite-quotes-snapshot" in result.stdout


def test_mid_day_cli_apply_aborts_when_quotes_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["mid-day", "--date", "2026-05-16", "--apply"]
    )
    assert result.exit_code == 2, result.stdout
    assert "/kite-quotes-snapshot" in result.stdout


def test_mid_day_cli_apply_happy_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Stub run_mid_day to avoid full mtm setup; verify exit-code + summary line."""
    from datetime import date as _d, datetime as _dt
    from pathlib import Path as _P
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)

    fake_update = tmp_path / "data" / "research" / "2026-05-16" / "mid_day_update.md"
    fake_update.parent.mkdir(parents=True, exist_ok=True)
    fake_update.write_text("stub", encoding="utf-8")

    from trading.jobs import mid_day as mid_day_mod
    fake_result = mid_day_mod.MidDayResult(
        as_of=_d(2026, 5, 16),
        quotes_capture_ts=_dt(2026, 5, 16, 12, 32),
        bars_built=3, trades_evaluated=2, trades_closed=1,
        trades_held=1, trades_skipped=0,
        update_path=fake_update, symbols_path=None, warnings=[],
    )
    monkeypatch.setattr(
        "trading.cli.run_mid_day", lambda *a, **kw: fake_result
    )
    result = runner.invoke(
        app, ["mid-day", "--date", "2026-05-16", "--apply"]
    )
    assert result.exit_code == 0, result.stdout
    assert "trades_evaluated" in result.stdout or "evaluated" in result.stdout
    assert "mid_day_update.md" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v -k mid_day`
Expected: FAIL — no such subcommand.

- [ ] **Step 3: Add the CLI command**

In `src/trading/cli.py`, add the import near the existing `from trading.jobs.pre_open` line:

```python
from trading.jobs.mid_day import MidDayAborted, run_mid_day
```

After the `pre_open_cmd` function (and before `if __name__ == "__main__":`), add:

```python
@app.command("mid-day")
def mid_day_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    apply: Annotated[
        bool,
        typer.Option("--apply",
                     help="Apply mode: read quotes + run MTM. Without --apply runs prepare mode."),
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
```

- [ ] **Step 4: Run tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v -k mid_day`
Expected: 3 passing.

Sanity check: `cd D:/Projects/Trading && uv run trading mid-day --help`

```bash
cd D:/Projects/Trading
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading mid-day [--apply] subcommand (14.A.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Briefing extension for `mode="mid_day"`

**Files:**
- Modify: `src/trading/llm/context.py`
- Modify: `src/trading/llm/briefing.py`
- Modify: `tests/test_llm_briefing.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_llm_briefing.py`:

```python
def test_compile_brief_mid_day_appends_update_when_present(
    tmp_path: Path,
) -> None:
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
        "## Mid-day update — captured 2026-05-16T12:32:14\n\n"
        "| symbol | action | ... |\n",
    )
    out = compile_brief(date_dir, mode="mid_day")
    body = out.read_text(encoding="utf-8")
    assert "## Mid-day update" in body
    assert "12:32:14" in body


def test_compile_brief_mid_day_skips_update_when_absent(
    tmp_path: Path,
) -> None:
    date_dir = tmp_path / "2026-05-16"
    date_dir.mkdir()
    _write_part(
        date_dir, "_context.md",
        "# Trading context bundle — 2026-05-16  (mode: pre_open)\n"
        "\n## Today's candidates\n\n### RVNL — passes 9/10 rules\n",
    )
    _write_part(date_dir, "macro_brief.md", "Regime: NEUTRAL.\n")
    _write_part(date_dir, "candidates/RVNL.md", "# RVNL — Conviction: HIGH\n")
    # NO mid_day_update.md
    out = compile_brief(date_dir, mode="mid_day")
    body = out.read_text(encoding="utf-8")
    assert "Mid-day update" not in body  # absent — section not added
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_llm_briefing.py -v -k mid_day`
Expected: FAIL — `mode="mid_day"` rejected by `Mode` literal.

- [ ] **Step 3: Extend `Mode` and `compile_brief`**

In `src/trading/llm/context.py`, change:

```python
Mode = Literal["pre_open", "post_close"]
```

to:

```python
Mode = Literal["pre_open", "mid_day", "post_close"]
```

In `src/trading/llm/briefing.py`, locate `compile_brief`. Find the
section after the candidates loop and before the post_close branch.
Insert the mid_day branch there:

```python
    for sym in symbols:
        body = (date_dir / "candidates" / f"{sym}.md").read_text(encoding="utf-8")
        sections.append("")
        sections.append(body.strip())

    # NEW: Optional mid-day update section
    mid_day_path = date_dir / "mid_day_update.md"
    if mid_day_path.is_file():
        sections.append("")
        sections.append(mid_day_path.read_text(encoding="utf-8").strip())

    if mode == "post_close":
        sections.append("")
        sections.append("## Post-close recap")
        ...
```

(`mid_day_update.md` is optional regardless of mode — adding it
unconditionally means pre_open and post_close briefs also include it
when present, which is the desired behaviour. The new mode literal is
just for type-correctness when callers explicitly want mid_day.)

- [ ] **Step 4: Run all briefing tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_llm_briefing.py -v`
Expected: all passing (existing 8 + 2 new = 10).

```bash
cd D:/Projects/Trading
git add src/trading/llm/context.py src/trading/llm/briefing.py tests/test_llm_briefing.py
git commit -m "feat(llm): compile_brief picks up mid_day_update.md when present (14.A.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: `/kite-quotes-snapshot` skill

**Files:**
- Create: `.claude/skills/kite-quotes-snapshot/SKILL.md`

No Python tests — this is a Claude Code skill. The `quotes_snapshot.py`
readers in Task 1 already validate the file contract.

- [ ] **Step 1: Create the skill directory + SKILL.md**

```bash
cd D:/Projects/Trading && mkdir -p .claude/skills/kite-quotes-snapshot
```

Then create `.claude/skills/kite-quotes-snapshot/SKILL.md`:

```markdown
---
name: kite-quotes-snapshot
description: Use when invoked at /kite-quotes-snapshot or when the user asks to fetch fresh intraday quotes from Kite via MCP for a downstream Python job (mid_day MTM). Reads the symbol list from data/raw/YYYY-MM-DD/_quote_symbols.txt (written by `trading mid-day` prepare mode), calls mcp__kite__get_quotes, writes data/raw/YYYY-MM-DD/quotes_HHMM.json, updates _meta.quotes_at.
---

# /kite-quotes-snapshot — fetch intraday quotes via MCP

Python's `trading mid-day --apply` reads `data/raw/<date>/quotes_HHMM.json`
to drive paper-trade MTM. Your job is to refresh that file by calling
`mcp__kite__get_quotes` for whatever symbols Python pre-flighted.

## Inputs

The user may pass a date. If absent, use today in `Asia/Kolkata`.

## Pre-flight check

Read `data/raw/<date>/_quote_symbols.txt` (one ticker per line). If the
file is absent, halt without writing any quotes file. Print:

> No `_quote_symbols.txt` found for <date>. Run `trading mid-day --date
> <date>` (without --apply) first to generate the symbol list, then
> re-invoke /kite-quotes-snapshot.

## Auth probe

Call `mcp__kite__get_profile`. If it raises an auth error (401 / not
logged in), DO NOT write any files. Print:

> Kite MCP is not authenticated. Please run `mcp__kite__login` to
> complete the browser handshake, then re-invoke /kite-quotes-snapshot.

Then halt.

## Fetch quotes

Build the instrument list. Kite MCP `get_quotes` accepts identifiers
like `"NSE:RVNL"`, so qualify each ticker with its exchange. For
holdings + paper-trades the exchange is in the underlying data; for
the simple MVP, default to `NSE:` and let the user fix any BSE
mismatches manually if they appear.

Call `mcp__kite__get_quotes(instruments=["NSE:RVNL", "NSE:NTPC", ...])`.

## Output schema

Write `data/raw/<date>/quotes_HHMM.json` (HHMM = current local time
`%H%M`). Use atomic `.tmp` + rename. The file is a flat JSON list of
dicts; each row's field names match `data.kite.Quote` plus a
top-level `tradingsymbol`:

```json
[
  {
    "instrument_token": 2977281,
    "last_price": 395.25,
    "volume": 8123456,
    "open": 396.30, "high": 397.10, "low": 393.80, "close": 396.30,
    "bid": 395.20, "ask": 395.30,
    "oi": null,
    "upper_circuit_limit": 435.93, "lower_circuit_limit": 356.67,
    "tradingsymbol": "NTPC"
  }
]
```

The MCP response shape may differ — map fields explicitly. If a field
is missing from the MCP response, set it to `null` rather than dropping
the row. Use `null` for fields that don't apply (e.g. `oi` for
non-derivatives).

## Update `_meta.json`

Read `data/raw/<date>/_meta.json` if present (the morning
`/kite-snapshot` skill will have created it). Merge:

```json
{
  "quotes_at": "<current ISO timestamp>"
}
```

into the existing object and write back atomically. Preserve
`snapshot_at`, `source`, `skill_version` if they exist. If `_meta.json`
is absent (no morning snapshot today), create one with `source: "mcp"`.

## After writing

Print a one-line summary (`quotes: 12 rows captured at HH:MM`) and the
next-step suggestion:

> Quotes snapshotted. Now run `trading mid-day --date YYYY-MM-DD --apply`.

## Failure modes

- MCP auth error → halt without writing files (see Auth probe).
- `mcp__kite__get_quotes` returns empty dict → write `quotes_HHMM.json: []` +
  update `_meta`. mid_day will SKIP every open trade.
- MCP returns unfamiliar shape → ask user before guessing. Do not write
  a partial / wrong file.
```

- [ ] **Step 2: Verify discovery**

```bash
cd D:/Projects/Trading && ls .claude/skills/kite-quotes-snapshot/
```

Expected: `SKILL.md` listed. Skill becomes available next session.

- [ ] **Step 3: Commit**

```bash
cd D:/Projects/Trading
git add .claude/skills/kite-quotes-snapshot/SKILL.md
git commit -m "feat(skill): /kite-quotes-snapshot fetches intraday quotes via MCP (14.A.2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: `scripts/mid_day.bat`

**Files:**
- Create: `scripts/mid_day.bat`

- [ ] **Step 1: Create the launcher**

```bash
cd D:/Projects/Trading && ls scripts/
```

Then create `scripts/mid_day.bat`:

```bat
@echo off
REM Phase 14.A two-step launcher.
REM Usage: mid_day.bat YYYY-MM-DD prepare
REM        mid_day.bat YYYY-MM-DD apply
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: mid_day.bat YYYY-MM-DD {prepare^|apply} & exit /b 2)
if "%~2"=="apply" (
  uv run python -m trading.jobs.mid_day %1 --apply
) else (
  uv run python -m trading.jobs.mid_day %1
)
```

- [ ] **Step 2: Smoke-test the `__main__` invocation**

```bash
cd D:/Projects/Trading
uv run python -m trading.jobs.mid_day 2026-05-16 2>&1 | tail -5
```

Expected: writes `data/raw/2026-05-16/_quote_symbols.txt`, prints
"wrote …" + "Now run /kite-quotes-snapshot skill, then re-run with
--apply".

- [ ] **Step 3: Commit**

```bash
cd D:/Projects/Trading
git add scripts/mid_day.bat
git commit -m "feat(jobs): mid_day.bat Windows launcher (14.A.6)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Real-data smoke + PROGRESS.md + commit + push

**Files:**
- Modify: `PROGRESS.md`
- Mutates: `data/raw/2026-05-16/` (gitignored)

- [ ] **Step 1: Verify Kite MCP auth + run /kite-snapshot for fresh holdings**

```bash
cd D:/Projects/Trading
uv run python -c "import sqlite3; c=sqlite3.connect('data/app.db'); print('open paper-trades:', c.execute('select count(*) from paper_trades where ts_exit is null').fetchone()[0])"
```

If paper-trades exist, mid-day MTM will actually evaluate them. If not,
the smoke still proves the pipeline by gathering the holdings + signals
symbol set and writing the markdown with empty results.

If Kite MCP isn't already authenticated this session, invoke
`mcp__kite__login`, complete the browser flow, and re-invoke
`/kite-snapshot` to refresh `data/raw/2026-05-16/holdings.json`.

- [ ] **Step 2: Run prepare**

```bash
cd D:/Projects/Trading
uv run trading mid-day --date 2026-05-16 2>&1 | tail -5
```

Expected: writes `_quote_symbols.txt`, prints next-step instruction.

```bash
cat data/raw/2026-05-16/_quote_symbols.txt
```

Expected: holdings symbols + any open paper-trade symbols + today's
signals, sorted.

- [ ] **Step 3: Invoke /kite-quotes-snapshot skill in Claude Code**

User asks the assistant: `/kite-quotes-snapshot`

The assistant (this skill) reads `_quote_symbols.txt`, calls
`mcp__kite__get_quotes` for the listed instruments, writes
`data/raw/2026-05-16/quotes_HHMM.json` + updates `_meta.json`.

Verify:

```bash
cd D:/Projects/Trading && ls -la data/raw/2026-05-16/quotes_*.json
cat data/raw/2026-05-16/_meta.json
```

- [ ] **Step 4: Run apply**

```bash
cd D:/Projects/Trading
uv run trading mid-day --date 2026-05-16 --apply 2>&1 | tail -20
```

Expected: Rich table with bars_built / trades_evaluated / closed / held
counts, "wrote mid_day_update.md" line.

- [ ] **Step 5: Inspect the update markdown**

```bash
cd D:/Projects/Trading && cat data/research/2026-05-16/mid_day_update.md
```

Expected: well-formed markdown with capture timestamp + per-trade table
+ summary line. If no open paper-trades exist, the table is empty and
the summary line says "0 open trades evaluated".

- [ ] **Step 6: Update PROGRESS.md**

In `PROGRESS.md`, in the status snapshot table, change the existing
Phase 14 row and add 14.A:

```
| 14 | mid_day + post_close jobs | `[~]` |
| 14.A | mid_day MVP | `[x]` |
```

(Phase 14 stays `[~]` until 14.B + 14.C also ship.)

After the existing Phase 13.5 body block, before the existing Phase 14
block, insert:

```markdown
## Phase 14.A — mid_day MVP

> Spec at [`docs/superpowers/specs/2026-05-16-phase-14-a-mid-day-design.md`](docs/superpowers/specs/2026-05-16-phase-14-a-mid-day-design.md).
> Plan at [`docs/superpowers/plans/2026-05-16-phase-14-a-mid-day.md`](docs/superpowers/plans/2026-05-16-phase-14-a-mid-day.md).

- [x] 14.A.1 `src/trading/data/quotes_snapshot.py`: `read_latest_quotes`
       + `QuoteSnapshotMissingError` / `StaleError`. Reads newest
       `quotes_HHMM.json` from `data/raw/<as_of>/`. Filename HHMM is
       single source of truth for capture time; staleness checked
       against wall-clock `datetime.now()`. 5 new tests.
- [x] 14.A.2 `.claude/skills/kite-quotes-snapshot/SKILL.md`: reads
       `_quote_symbols.txt`, calls `mcp__kite__get_quotes`, writes
       `quotes_HHMM.json` atomically, updates `_meta.quotes_at`.
- [x] 14.A.3 `src/trading/jobs/mid_day.py`: `gather_quote_symbols`
       (paper-trades ∪ signals ∪ holdings); `_quotes_to_bars`
       (close=last_price, NOT yesterday's close); `run_mid_day` two-mode
       orchestrator; `_render_mid_day_update` markdown builder;
       `MidDayAborted` + `MidDayResult`. 7 new tests including
       end-to-end EXIT_STOP closure + idempotency.
- [x] 14.A.4 `src/trading/cli.py`: `trading mid-day --date YYYY-MM-DD
       [--apply]` subcommand with Rich summary table + remediation on
       abort. 3 new tests.
- [x] 14.A.5 `src/trading/llm/briefing.py` + `context.py`: `Mode`
       extended to include `"mid_day"`; `compile_brief` includes
       `mid_day_update.md` after candidates section when present
       (regardless of mode — additive). 2 new tests.
- [x] 14.A.6 `scripts/mid_day.bat`: two-step Windows launcher
       (prepare/apply).
- [x] 14.A.7 Real-data smoke: `trading mid-day` (prepare) →
       `/kite-quotes-snapshot` (MCP) → `trading mid-day --apply`
       end-to-end. <fill in counts from Step 4 above>. Suite
       **N passed** (was 474 + ~17 new), 1 skipped (live), ruff +
       mypy clean. Commit `feat(jobs): mid_day MVP (Phase 14.A)`
       pushed to origin/main.
```

- [ ] **Step 7: Run full verification**

```bash
cd D:/Projects/Trading
uv run ruff check . && uv run mypy src/ && uv run pytest -q
```

Expected: clean. Test count: ~491 passed (474 + ~17 new), 1 skipped (live).

- [ ] **Step 8: Commit + push**

```bash
cd D:/Projects/Trading
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(jobs): mid_day MVP (Phase 14.A)

Wires intraday MTM end-to-end. Two-phase Python (`prepare` / `apply`)
wrapping a Claude Code skill in the middle: trading mid-day prepare
writes the symbol list, /kite-quotes-snapshot fetches via MCP and
writes data/raw/<date>/quotes_HHMM.json, trading mid-day --apply
reads the quotes, runs paper.mtm.mtm_open_trades, writes
mid_day_update.md.

Reuses paper.mtm + data.kite.Quote unchanged. Hard-halts (exit 2)
on missing or stale snapshot — no silent skips. Same file-handshake
pattern as Phase 12 (analyst) and Phase 13.5 (kite-snapshot).

mid_day_update.md is optional in compile_brief regardless of mode
(additive); the new "mid_day" Mode literal is for type-correct
callers that want to declare intent.

Spec: docs/superpowers/specs/2026-05-16-phase-14-a-mid-day-design.md
Plan: docs/superpowers/plans/2026-05-16-phase-14-a-mid-day.md

Tests: <count> passed, 1 skipped (live), ruff + mypy clean.

Real-data smoke (2026-05-16):
  trading mid-day prepare → wrote N symbols.
  /kite-quotes-snapshot via MCP → wrote quotes_HHMM.json.
  trading mid-day --apply → bars_built=N, evaluated=N,
    closed=N, held=N. mid_day_update.md written.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push origin main
```

Expected: push succeeds; Phase 14.A is shipped. Phase 14.B (post_close)
and 14.C (pre_open_iep) will get their own brainstorm → spec → plan
→ implementation cycles after this.

---

## Self-review notes

- **Spec coverage:** spec §3.1 quotes_snapshot → Task 1. §3.2 mid_day.py → Tasks 2+3. §3.3 skill → Task 6. §3.4 CLI → Task 4. §3.5 briefing extension → Task 5. §3.6 .bat launcher → Task 7. §4 file contract enforced by Task 1's reader + Task 6 skill. §5 Quote→Bar conversion → Task 3 (`_quotes_to_bars`). §6 markdown shape → Task 3 (`_render_mid_day_update`). §7 error handling → tests in Tasks 1, 3, 4. §8 testing matrix → mapped 1:1. §9 sub-task breakdown → mapped to Tasks 1-8. ✓
- **Type consistency:** `Bar` has `(open, high, low, close)` only — no `date` or `volume` (verified). `Quote` field names match the on-disk schema (verified). `MidDayResult` has the `trades_skipped` field that's set in apply mode (added during writing — earlier draft missed it). `Mode` literal extended in `context.py` (where it's defined), re-imported via `briefing.py`. `MidDayAborted` defined in `mid_day.py`, used in CLI. ✓
- **Placeholder scan:** every code block is concrete; commit messages written out (one judgement-call placeholder: smoke counts in Task 8 Step 6 are filled in only after Step 4 actually runs). ✓
- **TDD discipline:** every task is failing-test → impl → run-tests → commit. ✓

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-16-phase-14-a-mid-day.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

**Which approach?**
