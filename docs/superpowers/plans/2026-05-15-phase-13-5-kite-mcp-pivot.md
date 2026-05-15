# Phase 13.5 — Kite-via-MCP Pivot: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MCP the only production path to Kite data; keep the SDK wrapper as an unwired manual fallback.

**Architecture:** New `data/kite_snapshot.py` reads JSON files written by a `/kite-snapshot` Claude Code skill (which calls `mcp__kite__*`). Pre_open and the `portfolio` CLI consume those JSONs; on missing/stale snapshot they hard-halt with a remediation message. SDK wrapper at `src/trading/data/kite.py` stays in the repo, wired only into two new `kite-emergency-*` CLI commands.

**Tech Stack:** Python 3.11 · sqlite3 · `typer` (CLI) · `pytest` · existing project modules. Skill side: Claude Code MCP tools (`mcp__kite__*`).

**Spec:** [`docs/superpowers/specs/2026-05-15-phase-13-5-kite-mcp-pivot-design.md`](../specs/2026-05-15-phase-13-5-kite-mcp-pivot-design.md)

---

## File structure

| Path | Created/Modified | Responsibility |
|------|------------------|----------------|
| `src/trading/data/kite_snapshot.py` | Create | `KiteSnapshotMissingError`, `KiteSnapshotStaleError`, `read_holdings/gtts/positions` — pure JSON-to-dataclass readers; `_meta.json` validation |
| `tests/test_kite_snapshot.py` | Create | Per-reader happy path + missing + stale + empty-list |
| `.claude/skills/kite-snapshot/SKILL.md` | Create | Claude Code skill: probe `mcp__kite__get_profile`, call `get_holdings`/`get_gtts`, write JSON files atomically |
| `src/trading/jobs/pre_open.py` | Modify | Drop `skip_kite` arg + Kite-SDK imports; add `PreOpenAborted`; rewrite `_step_portfolio` to use `kite_snapshot.read_holdings` |
| `src/trading/cli.py` | Modify | `pre-open`: drop `--skip-kite`, catch `PreOpenAborted` → exit 2; `portfolio`: use `kite_snapshot`; rename `kite-login` → `kite-emergency-login`; add `kite-emergency-snapshot` |
| `tests/test_jobs_pre_open.py` | Modify | Replace 3 `_step_portfolio` tests; drop `--skip-kite` from CLI test; add missing-snapshot test |
| `tests/test_cli.py` | Modify | Update `portfolio` CLI test to seed JSONs; rename `kite-login` test; add `kite-emergency-snapshot` test; drop `--skip-kite` from pre-open CLI test |
| `tests/conftest.py` | Modify | Add `seed_kite_snapshot(paths, as_of, **lists)` helper to keep snapshot-fixture boilerplate out of tests |
| `PROGRESS.md` | Modify | Mark 13.5 done; insert sub-task block; update pointers |

---

## Task 1: `kite_snapshot.py` skeleton + `read_holdings`

**Files:**
- Create: `src/trading/data/kite_snapshot.py`
- Create: `tests/test_kite_snapshot.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add the snapshot-seed fixture helper to conftest**

In `tests/conftest.py`, append at the bottom:

```python
import json as _kite_json
from datetime import datetime as _kite_dt


def seed_kite_snapshot(
    paths,
    as_of,
    *,
    holdings=None,
    gtts=None,
    positions=None,
    snapshot_at=None,
    source="mcp",
):
    """Test helper: write data/raw/<as_of>/{resource}.json + _meta.json.

    Each `*_lists` parameter is a list of dicts (or None to skip the
    file). Used by kite_snapshot tests + pre_open / portfolio CLI tests.
    """
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    if holdings is not None:
        (base / "holdings.json").write_text(
            _kite_json.dumps(holdings), encoding="utf-8"
        )
    if gtts is not None:
        (base / "gtts.json").write_text(
            _kite_json.dumps(gtts), encoding="utf-8"
        )
    if positions is not None:
        (base / "positions.json").write_text(
            _kite_json.dumps(positions), encoding="utf-8"
        )
    meta_ts = (
        snapshot_at.isoformat()
        if snapshot_at is not None
        else _kite_dt.combine(as_of, _kite_dt.min.time()).isoformat()
    )
    (base / "_meta.json").write_text(
        _kite_json.dumps({
            "snapshot_at": meta_ts, "source": source, "skill_version": "1",
        }),
        encoding="utf-8",
    )
    return base
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_kite_snapshot.py`:

```python
"""Tests for trading.data.kite_snapshot — JSON readers + typed errors."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.data.kite import Holding
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_holdings,
)
from tests.conftest import seed_kite_snapshot


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


_HOLDING_ROW = {
    "tradingsymbol": "RVNL", "exchange": "NSE", "isin": "INE415G01027",
    "quantity": 32, "average_price": 305.0, "last_price": 329.6,
    "close_price": 327.1, "pnl": 787.2, "day_change": 2.5,
    "day_change_percentage": 0.76,
}


def test_read_holdings_happy_path(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[_HOLDING_ROW])
    out = read_holdings(paths, date(2026, 5, 15))
    assert len(out) == 1
    assert isinstance(out[0], Holding)
    assert out[0].tradingsymbol == "RVNL"
    assert out[0].quantity == 32
    assert out[0].pnl == 787.2


def test_read_holdings_returns_empty_list(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[])
    assert read_holdings(paths, date(2026, 5, 15)) == []


def test_read_holdings_missing_raises(paths) -> None:
    with pytest.raises(KiteSnapshotMissingError) as exc:
        read_holdings(paths, date(2026, 5, 15))
    msg = str(exc.value)
    assert "holdings.json" in msg
    assert "/kite-snapshot" in msg


def test_read_holdings_stale_raises(paths) -> None:
    # Snapshot taken yesterday but caller asks for today
    yesterday = datetime(2026, 5, 14, 16, 30)
    seed_kite_snapshot(
        paths, date(2026, 5, 15),
        holdings=[_HOLDING_ROW],
        snapshot_at=yesterday,
    )
    with pytest.raises(KiteSnapshotStaleError) as exc:
        read_holdings(paths, date(2026, 5, 15))
    assert "2026-05-14" in str(exc.value)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_kite_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: trading.data.kite_snapshot`.

- [ ] **Step 4: Write the implementation**

Create `src/trading/data/kite_snapshot.py`:

```python
"""Phase 13.5 — readers for Kite snapshots written by /kite-snapshot skill.

Production code reads Kite data through these readers, never the SDK.
Each reader opens `data/raw/<as_of>/<resource>.json` and parses rows
into the existing dataclasses from `trading.data.kite`. Missing files
raise `KiteSnapshotMissingError`; out-of-date snapshots raise
`KiteSnapshotStaleError`. The skill (or `kite-emergency-snapshot` CLI
fallback) writes both the resource JSONs and `_meta.json`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from trading.config import Paths
from trading.data.kite import GttOrder, Holding, Position


class KiteSnapshotMissingError(RuntimeError):
    """Raised when an expected snapshot JSON is absent for `as_of`."""


class KiteSnapshotStaleError(RuntimeError):
    """Raised when _meta.snapshot_at's date doesn't match `as_of`."""


def _validate_meta(date_dir: Path, as_of: date) -> None:
    meta_path = date_dir / "_meta.json"
    if not meta_path.is_file():
        raise KiteSnapshotMissingError(
            f"No Kite snapshot _meta.json at {meta_path}. "
            "Run /kite-snapshot skill in Claude Code first."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    snapshot_at = meta.get("snapshot_at", "")
    snap_date = snapshot_at[:10]
    if snap_date != as_of.isoformat():
        raise KiteSnapshotStaleError(
            f"Snapshot at {date_dir} is for {snap_date}, requested {as_of.isoformat()}. "
            "Re-run /kite-snapshot skill to refresh."
        )


def _read_resource(
    paths: Paths,
    as_of: date,
    filename: str,
) -> list[dict]:
    date_dir = paths.raw_dir / as_of.isoformat()
    target = date_dir / filename
    if not target.is_file():
        raise KiteSnapshotMissingError(
            f"No Kite {filename} at {target}. "
            "Run /kite-snapshot skill in Claude Code first."
        )
    _validate_meta(date_dir, as_of)
    return json.loads(target.read_text(encoding="utf-8"))


def read_holdings(paths: Paths, as_of: date) -> list[Holding]:
    """Read `holdings.json` for `as_of` → list of Holding dataclasses."""
    rows = _read_resource(paths, as_of, "holdings.json")
    return [Holding(**row) for row in rows]


def read_gtts(paths: Paths, as_of: date) -> list[GttOrder]:
    """Read `gtts.json` for `as_of` → list of GttOrder dataclasses."""
    rows = _read_resource(paths, as_of, "gtts.json")
    return [GttOrder(**row) for row in rows]


def read_positions(paths: Paths, as_of: date) -> list[Position]:
    """Read `positions.json` for `as_of` → list of Position dataclasses."""
    rows = _read_resource(paths, as_of, "positions.json")
    return [Position(**row) for row in rows]
```

- [ ] **Step 5: Run tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_kite_snapshot.py tests/test_kite.py -v`
Expected: 4 new tests pass; existing 15 kite-wrapper tests still pass.

```bash
cd D:/Projects/Trading
git add src/trading/data/kite_snapshot.py tests/test_kite_snapshot.py tests/conftest.py
git commit -m "feat(data): kite_snapshot.read_holdings + typed errors (13.5.1a)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Add `read_gtts` and `read_positions` test coverage

**Files:**
- Modify: `tests/test_kite_snapshot.py`

The implementations were written in Task 1 (since the helper `_read_resource` is shared). This task just exercises them.

- [ ] **Step 1: Append tests**

Append to `tests/test_kite_snapshot.py`:

```python
from trading.data.kite import GttOrder, Position
from trading.data.kite_snapshot import read_gtts, read_positions


_GTT_ROW = {
    "id": 12345, "type": "single", "status": "active",
    "tradingsymbol": "RVNL", "exchange": "NSE",
    "trigger_values": [350.0], "last_price": 329.6,
    "created_at": "2026-05-10T10:00:00",
    "orders": [{"transaction_type": "SELL", "quantity": 32, "price": 350.0}],
}

_POSITION_ROW = {
    "tradingsymbol": "NTPC", "exchange": "NSE", "product": "CNC",
    "quantity": 10, "average_price": 303.0, "last_price": 305.5, "pnl": 25.0,
}


def test_read_gtts_happy_path(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), gtts=[_GTT_ROW])
    out = read_gtts(paths, date(2026, 5, 15))
    assert len(out) == 1
    assert isinstance(out[0], GttOrder)
    assert out[0].id == 12345
    assert out[0].trigger_values == [350.0]
    assert out[0].orders[0]["transaction_type"] == "SELL"


def test_read_gtts_missing_raises(paths) -> None:
    with pytest.raises(KiteSnapshotMissingError):
        read_gtts(paths, date(2026, 5, 15))


def test_read_positions_happy_path(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), positions=[_POSITION_ROW])
    out = read_positions(paths, date(2026, 5, 15))
    assert len(out) == 1
    assert isinstance(out[0], Position)
    assert out[0].tradingsymbol == "NTPC"
    assert out[0].pnl == 25.0
```

- [ ] **Step 2: Run + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_kite_snapshot.py -v`
Expected: 7 passing.

```bash
cd D:/Projects/Trading
git add tests/test_kite_snapshot.py
git commit -m "test(data): kite_snapshot read_gtts + read_positions coverage (13.5.1b)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Create the `/kite-snapshot` skill

**Files:**
- Create: `.claude/skills/kite-snapshot/SKILL.md`

No Python tests — this is a Claude Code skill. The `kite_snapshot.py` readers in Task 1 already validate the file contract that the skill produces.

- [ ] **Step 1: Write `SKILL.md`**

```bash
cd D:/Projects/Trading && mkdir -p .claude/skills/kite-snapshot
```

Then create `.claude/skills/kite-snapshot/SKILL.md`:

```markdown
---
name: kite-snapshot
description: Use when invoked at /kite-snapshot or when the user asks to refresh Kite holdings/GTTs/positions from MCP for a downstream Python job (pre_open, portfolio CLI). Fetches via mcp__kite__* tools, writes data/raw/YYYY-MM-DD/{holdings,gtts,positions}.json + _meta.json so trading.data.kite_snapshot.read_* can consume them.
---

# /kite-snapshot — fetch Kite data via MCP and write to disk

You are the Kite data ingest layer. Python jobs (pre_open, `trading portfolio`)
need today's holdings / GTTs / positions but cannot call MCP themselves. Your
job is to fetch via `mcp__kite__*` and write JSON files to
`data/raw/YYYY-MM-DD/`.

## Inputs

The user may pass a date. If absent, use today in `Asia/Kolkata`.

The user may pass a list of resources to snapshot (default: holdings + GTTs).
Positions are only needed by Phase 14 mid_day / post_close jobs.

## Auth probe

Always start by calling `mcp__kite__get_profile`. If it raises an auth error
(401 / not logged in), DO NOT write any files. Print:

> Kite MCP is not authenticated. Please run `mcp__kite__login` to complete
> the browser handshake, then re-invoke `/kite-snapshot`.

Then halt. The user will run `mcp__kite__login` (which opens a browser) and
re-invoke this skill.

## Resource fetch + write

For each requested resource:

| Resource | MCP tool | Output file |
|----------|----------|-------------|
| holdings | `mcp__kite__get_holdings` | `data/raw/<date>/holdings.json` |
| gtts | `mcp__kite__get_gtts` | `data/raw/<date>/gtts.json` |
| positions | `mcp__kite__get_positions` | `data/raw/<date>/positions.json` |

For each: call the MCP tool, map the response to the on-disk schema (see
below), and write to a `.tmp` file then rename to the final filename
(atomic replace).

## On-disk schema

Each file is a flat JSON list of dicts, one per record. Field names match the
dataclasses in `src/trading/data/kite.py` exactly so Python can do
`Holding(**row)` without a mapping layer.

### `holdings.json` row shape

```json
{
  "tradingsymbol": "RVNL", "exchange": "NSE", "isin": "INE415G01027",
  "quantity": 32, "average_price": 305.0, "last_price": 329.6,
  "close_price": 327.1, "pnl": 787.2, "day_change": 2.5,
  "day_change_percentage": 0.76
}
```

### `gtts.json` row shape

```json
{
  "id": 12345, "type": "single", "status": "active",
  "tradingsymbol": "RVNL", "exchange": "NSE",
  "trigger_values": [350.0], "last_price": 329.6,
  "created_at": "2026-05-10T10:00:00",
  "orders": [{"transaction_type": "SELL", "quantity": 32, "price": 350.0}]
}
```

### `positions.json` row shape

```json
{
  "tradingsymbol": "NTPC", "exchange": "NSE", "product": "CNC",
  "quantity": 10, "average_price": 303.0, "last_price": 305.5, "pnl": 25.0
}
```

If MCP returns an unfamiliar shape, map what you can (use the dataclass field
names) and ask the user about anything you don't know how to map. Do not
silently drop fields the dataclass requires.

## `_meta.json` (always written last)

After all requested resources are written, write `_meta.json`:

```json
{
  "snapshot_at": "<current ISO timestamp>",
  "source": "mcp",
  "skill_version": "1"
}
```

## After writing

Print a one-line summary per resource (`holdings: 12 rows`, `gtts: 3 rows`,
etc.) and the next-step suggestion:

> Snapshot ready. Now run `trading pre-open --date YYYY-MM-DD` (or
> `trading portfolio --date YYYY-MM-DD`).

## Failure modes

- MCP auth error → halt without writing files (see Auth probe).
- MCP tool returns empty list → write `[]` to the file. Empty is valid.
- MCP tool returns unexpected shape (missing required field) → ask the user
  before guessing. Do not write a partial / wrong file.
- Resource MCP tool errors mid-flight (network blip) → write the resources
  that did succeed + `_meta.json`, then surface which one failed and tell the
  user to re-invoke.
```

- [ ] **Step 2: Verify discovery**

Manual check:

```bash
cd D:/Projects/Trading && ls .claude/skills/kite-snapshot/
```

Expected: `SKILL.md` is listed. The skill becomes available next time the
session starts (or when /skills are reloaded).

- [ ] **Step 3: Commit**

```bash
cd D:/Projects/Trading
git add .claude/skills/kite-snapshot/SKILL.md
git commit -m "feat(skill): /kite-snapshot fetches Kite via MCP, writes JSONs (13.5.2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Rewrite `_step_portfolio` + `PreOpenAborted`

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_jobs_pre_open.py`, replace the existing
`test_step_portfolio_returns_empty_when_skip_kite`,
`test_step_portfolio_returns_empty_when_no_token`, and
`test_step_portfolio_degrades_on_kite_auth_error` tests with these:

```python
from trading.data.kite_snapshot import KiteSnapshotMissingError
from trading.jobs.pre_open import PreOpenAborted
from tests.conftest import seed_kite_snapshot


_PRE_OPEN_HOLDING = {
    "tradingsymbol": "RVNL", "exchange": "NSE", "isin": "INE415G01027",
    "quantity": 32, "average_price": 305.0, "last_price": 329.6,
    "close_price": 327.1, "pnl": 787.2, "day_change": 2.5,
    "day_change_percentage": 0.76,
}


def test_step_portfolio_reads_snapshot_and_scores(paths) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[_PRE_OPEN_HOLDING])
    warnings: list[str] = []
    out = _step_portfolio(paths, _settings(), warnings, as_of=date(2026, 5, 15))
    assert len(out) == 1
    assert out[0].symbol == "RVNL"
    # No graceful-degradation warning expected on a happy path
    assert warnings == []


def test_step_portfolio_raises_pre_open_aborted_when_snapshot_missing(
    paths,
) -> None:
    warnings: list[str] = []
    with pytest.raises(PreOpenAborted) as exc:
        _step_portfolio(paths, _settings(), warnings, as_of=date(2026, 5, 15))
    assert "/kite-snapshot" in str(exc.value)
```

Also DROP the existing `test_run_pre_open_returns_result_with_bundle_path`'s
reference to `skip_kite=True` — replace it with a `seed_kite_snapshot(...)`
call so the integration tests have a real snapshot file.

```python
def test_run_pre_open_returns_result_with_bundle_path(
    paths, monkeypatch
) -> None:
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[])
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_macro",
        lambda conn, as_of, warnings: (False, "NEUTRAL"),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_news",
        lambda conn, as_of, warnings: (0, 0),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_scan",
        lambda paths, as_of, warnings: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_auto_open",
        lambda conn, as_of, passing, regime, capital, risk_pct, warnings: 0,
    )
    result = run_pre_open(date(2026, 5, 15), paths=paths, skip_news=True)
    assert isinstance(result, PreOpenResult)
    assert result.bundle_path.is_file()
```

And UPDATE the integration test (`test_run_pre_open_full_happy_path_integration`)
to also seed an empty holdings snapshot and drop `skip_kite=True`:

```python
    write_ohlcv(_all_pass_frame(), "TESTSYM", paths)
    seed_kite_snapshot(paths, date(2026, 5, 15), holdings=[])  # NEW
    # ... rest unchanged, except remove `skip_kite=True` from both
    # run_pre_open() calls
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v -k portfolio`
Expected: FAIL — `_step_portfolio` still has `skip_kite` keyword arg + `make_client`
imports + no `PreOpenAborted`.

- [ ] **Step 3: Write the implementation**

In `src/trading/jobs/pre_open.py`:

Replace these imports:

```python
from trading.data.kite import KiteAuthError, get_holdings, make_client
```

with:

```python
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_holdings,
)
```

Add a new exception near the top of the module (after imports, before
`PreOpenResult`):

```python
class PreOpenAborted(RuntimeError):
    """Raised when run_pre_open cannot proceed because a prerequisite is missing.

    Currently raised when the Kite snapshot for `as_of` is missing or stale —
    `/kite-snapshot` skill must run first. CLI catches this and exits 2 with
    the remediation message.
    """
```

Replace `_step_portfolio` entirely:

```python
def _step_portfolio(
    paths: Paths,
    settings: Settings,
    warnings: list[str],
    *,
    as_of: date,
) -> list[HealthScore]:
    """Score each holding from today's Kite snapshot.

    Reads `data/raw/<as_of>/holdings.json` (written by the /kite-snapshot
    skill). If missing or stale → `PreOpenAborted` with remediation hint.
    `settings` is unused here today; kept on the signature for symmetry
    with other steps and forward-compat with future per-account config.
    """
    try:
        holdings = read_holdings(paths, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError) as e:
        raise PreOpenAborted(str(e)) from e

    results: list[HealthScore] = []
    for h in holdings:
        try:
            history = read_ohlcv(h.tradingsymbol, paths)
        except FileNotFoundError:
            warnings.append(f"no parquet for holding {h.tradingsymbol} — skipped")
            continue
        ctx = HoldingContext(
            symbol=h.tradingsymbol,
            qty=h.quantity,
            avg_price=h.average_price,
            last_price=h.last_price,
            technicals=technicals_from_history(history),
            fundamentals=FundamentalsSnapshot(),
            sentiment=SentimentSnapshot(),
        )
        results.append(score_holding(ctx))
    return results
```

In `run_pre_open`'s signature, drop `skip_kite: bool = False`. Update the
call site at the body:

```python
        holdings = _step_portfolio(p, s, warnings, as_of=as_of)
```

(Drop the `skip_kite=skip_kite` kwarg.)

Drop the unused `KiteAuthError`, `get_holdings`, `make_client` symbols
nowhere referenced now.

- [ ] **Step 4: Run pre_open tests**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: all passing — the original 12 minus 3 deleted plus 2 new = 11.

- [ ] **Step 5: Update CLI for `PreOpenAborted` exit handling**

In `src/trading/cli.py`, locate the `pre-open` command. Drop the
`skip_kite: Annotated[bool, ...]` parameter. Wrap the `run_pre_open` call
in a try/except:

```python
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
            as_of, skip_news=skip_news,
            capital_per_trade=capital, risk_pct=risk_pct,
        )
    except PreOpenAborted as e:
        console.print(f"[red]Pre-open aborted:[/red] {e}")
        raise typer.Exit(code=2) from e
    # ... existing Rich table + next-step instruction unchanged
```

Add the import at the top of `cli.py`:

```python
from trading.jobs.pre_open import PreOpenAborted, run_pre_open
```

(Replacing the existing `from trading.jobs.pre_open import run_pre_open`.)

Update the existing pre-open CLI test in `tests/test_cli.py` —
`test_pre_open_cli_writes_bundle_and_prints_next_step` — drop the
`"--skip-kite"` argument from the `runner.invoke(...)` call, and add a
`seed_kite_snapshot(..., holdings=[])` call so the snapshot exists. Add a
new test:

```python
def test_pre_open_cli_aborts_when_kite_snapshot_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    from trading.jobs import pre_open as po
    monkeypatch.setattr(po, "_step_macro",
                        lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr(po, "_step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr(po, "_step_scan", lambda p, d, w: [])
    monkeypatch.setattr(po, "_step_auto_open", lambda *a, **kw: 0)
    # NO seed_kite_snapshot call → portfolio step will raise
    result = runner.invoke(
        app,
        ["pre-open", "--date", "2026-05-15", "--skip-news"],
    )
    assert result.exit_code == 2, result.stdout
    assert "/kite-snapshot" in result.stdout
```

- [ ] **Step 6: Run all tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py tests/test_cli.py -v 2>&1 | tail -20`
Expected: all passing.

```bash
cd D:/Projects/Trading
git add src/trading/jobs/pre_open.py src/trading/cli.py tests/test_jobs_pre_open.py tests/test_cli.py
git commit -m "feat(jobs): _step_portfolio reads kite_snapshot; PreOpenAborted on missing (13.5.3)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Rewire `trading portfolio` CLI

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Find the existing portfolio test**

Run: `cd D:/Projects/Trading && grep -n "portfolio_cmd\|trading portfolio\|portfolio.*runner" tests/test_cli.py`

If a `test_portfolio_*` test exists, find it. If not, we'll add one.

- [ ] **Step 2: Write the failing test**

Replace any existing `test_portfolio_*` test (or add one if absent) with:

```python
def test_portfolio_cli_reads_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    from datetime import date as _d

    from tests.conftest import seed_kite_snapshot
    seed_kite_snapshot(
        tmp_path_paths := tmp_path,
        _d(2026, 5, 15),
        holdings=[],
        gtts=[],
    )
    # Override the paths fixture used by the CLI by setting env above.
    result = runner.invoke(
        app, ["portfolio", "--date", "2026-05-15"]
    )
    assert result.exit_code == 0, result.stdout
    assert "0 holding" in result.stdout or "Loaded 0" in result.stdout


def test_portfolio_cli_aborts_when_snapshot_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["portfolio", "--date", "2026-05-15"]
    )
    assert result.exit_code == 2, result.stdout
    assert "/kite-snapshot" in result.stdout
```

The first test seeds an empty snapshot (no holdings, no GTTs) so the CLI
can run cleanly without calling Kite.

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v -k portfolio`
Expected: FAIL — `portfolio` CLI still calls `make_client(settings.kite_api_key)`.

- [ ] **Step 4: Rewrite the `portfolio` command**

In `src/trading/cli.py`, find the `portfolio_cmd` function and replace its
body. Remove the `make_client` / `get_holdings` / `get_gtts` calls. Add a
required `--date` option. The new shape:

```python
@app.command("portfolio")
def portfolio_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD for the snapshot to read.")],
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
        holdings_list = read_holdings(paths, as_of)
        gtts_list = read_gtts(paths, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e

    console.print(
        f"[bold]Loaded {len(holdings_list)} holding(s), "
        f"{len(gtts_list)} GTT(s) from snapshot.[/bold]"
    )

    # ... rest of the existing portfolio logic (enrich + score + render)
    # is unchanged; only the data source changed.
```

Add the imports:

```python
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_gtts,
    read_holdings,
)
```

Drop the now-unused imports (`make_client`, `get_holdings`, `get_gtts`,
`KiteAuthError`) — only if no other CLI command uses them. The `kite-login`
(soon `kite-emergency-login`) and `kite-emergency-snapshot` commands DO
use them, so keep them.

- [ ] **Step 5: Run tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v 2>&1 | tail -15`
Expected: all passing (portfolio happy path + abort + the existing CLI tests).

```bash
cd D:/Projects/Trading
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading portfolio reads kite_snapshot (13.5.4)

Adds required --date option; drops SDK calls. KiteSnapshotMissingError
exits 2 with the same /kite-snapshot remediation as pre-open.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Rename `kite-login` + add `kite-emergency-snapshot`

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_kite_emergency_login_present_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert "kite-emergency-login" in result.stdout
    assert "kite-login" not in result.stdout  # renamed away


def test_kite_emergency_snapshot_writes_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("KITE_API_KEY", "fake")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "fake")

    fake_holding = MagicMock(
        tradingsymbol="RVNL", exchange="NSE", isin="INE415G01027",
        quantity=32, average_price=305.0, last_price=329.6,
        close_price=327.1, pnl=787.2, day_change=2.5,
        day_change_percentage=0.76,
    )
    fake_gtt = MagicMock(
        id=1, type="single", status="active", tradingsymbol="RVNL",
        exchange="NSE", trigger_values=[350.0], last_price=329.6,
        created_at="2026-05-10T10:00:00",
        orders=[{"transaction_type": "SELL", "quantity": 32, "price": 350.0}],
    )
    with patch("trading.cli.make_client", return_value=MagicMock()), \
         patch("trading.cli.get_holdings", return_value=[fake_holding]), \
         patch("trading.cli.get_gtts", return_value=[fake_gtt]):
        result = runner.invoke(
            app, ["kite-emergency-snapshot", "--date", "2026-05-15"]
        )
    assert result.exit_code == 0, result.stdout
    base = tmp_path / "data" / "raw" / "2026-05-15"
    assert (base / "holdings.json").is_file()
    assert (base / "gtts.json").is_file()
    assert (base / "_meta.json").is_file()
    import json as _j
    meta = _j.loads((base / "_meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "sdk-fallback"
```

Add the `MagicMock`, `patch` imports if not already present at the top of
`test_cli.py`:

```python
from unittest.mock import MagicMock, patch
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v -k "kite_emergency"`
Expected: FAIL — neither command exists yet.

- [ ] **Step 3: Rename `kite-login` and add `kite-emergency-snapshot`**

In `src/trading/cli.py`, rename the existing `@app.command("kite-login")` to:

```python
@app.command("kite-emergency-login")
def kite_emergency_login(
    request_token: Annotated[
        str | None,
        typer.Option("--request-token", "-t",
                     help="Skip the interactive prompt and pass the request_token directly."),
    ] = None,
) -> None:
    """FALLBACK: interactive Kite Connect SDK login.

    Production paths use the /kite-snapshot skill (MCP). Use this command
    only when MCP is broken and you need to populate KITE_ACCESS_TOKEN
    in .env so `kite-emergency-snapshot` can run.
    """
    # ... body unchanged
```

Update the body's error message from `"Run \`trading kite-login\` first."` to
`"Run \`trading kite-emergency-login\` first."`. Search for any other
references to `kite-login` in the codebase and update.

Add the new `kite-emergency-snapshot` command directly below it:

```python
import json as _kes_json
from datetime import datetime as _kes_dt
from dataclasses import asdict as _kes_asdict


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

    def _dump(rows, name: str) -> None:
        tmp = base / f"{name}.tmp"
        final = base / f"{name}"
        tmp.write_text(
            _kes_json.dumps([_kes_asdict(r) for r in rows]), encoding="utf-8"
        )
        tmp.replace(final)

    _dump(holdings_list, "holdings.json")
    _dump(gtts_list, "gtts.json")

    meta = {
        "snapshot_at": _kes_dt.now().isoformat(),
        "source": "sdk-fallback",
        "skill_version": "1",
    }
    meta_tmp = base / "_meta.tmp"
    meta_tmp.write_text(_kes_json.dumps(meta), encoding="utf-8")
    meta_tmp.replace(base / "_meta.json")

    console.print(
        f"[green]Wrote {len(holdings_list)} holdings + {len(gtts_list)} GTTs "
        f"to {base} (sdk-fallback).[/green]"
    )
```

- [ ] **Step 4: Run tests + commit**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v 2>&1 | tail -15`
Expected: all passing.

```bash
cd D:/Projects/Trading
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): rename kite-login → kite-emergency-login + add kite-emergency-snapshot (13.5.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Real-data smoke + PROGRESS.md + commit + push

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Run /kite-snapshot skill in this session**

The `/kite-snapshot` skill is now available. From the user's terminal in
Claude Code:

```
/kite-snapshot
```

(I, the assistant, execute the skill: probe `mcp__kite__get_profile`, fetch
holdings + GTTs, write `data/raw/YYYY-MM-DD/holdings.json` etc.)

If MCP auth fails, the skill prompts the user to run `mcp__kite__login`,
the user does so, then re-invokes `/kite-snapshot`.

- [ ] **Step 2: Verify the snapshot files exist**

```bash
cd D:/Projects/Trading
ls -la data/raw/2026-05-15/
cat data/raw/2026-05-15/_meta.json
```

Expected: `holdings.json`, `gtts.json`, `_meta.json` present; meta shows
`"source": "mcp"`.

- [ ] **Step 3: Run `trading pre-open` end-to-end**

```bash
cd D:/Projects/Trading
uv run trading pre-open --date 2026-05-15
```

Expected: Rich table shows `holdings_scored: <N>` (non-zero if user has
holdings); no warnings about Kite auth or skipped portfolio. Bundle
written to `data/research/2026-05-15/_context.md`.

- [ ] **Step 4: Verify portfolio CLI**

```bash
cd D:/Projects/Trading
uv run trading portfolio --date 2026-05-15
```

Expected: prints "Loaded N holdings, M GTTs from snapshot.", proceeds with
health scoring + GTT viability projection.

- [ ] **Step 5: Update PROGRESS.md**

Edit `PROGRESS.md`. In the status snapshot table, add:

```
| 13.5 | Kite MCP pivot | `[x]` |
```

between the Phase 13 row and the Phase 14 row.

After the Phase 13 body block, before Phase 14, insert:

```markdown
## Phase 13.5 — Kite MCP pivot

> Reverses Phase 13's `--skip-kite` design. Production paths read Kite data
> from JSON files written by a `/kite-snapshot` Claude Code skill. SDK
> wrapper at `src/trading/data/kite.py` is kept as a manual fallback,
> wired only into `kite-emergency-*` CLI commands.
> Spec at [`docs/superpowers/specs/2026-05-15-phase-13-5-kite-mcp-pivot-design.md`](docs/superpowers/specs/2026-05-15-phase-13-5-kite-mcp-pivot-design.md).
> Plan at [`docs/superpowers/plans/2026-05-15-phase-13-5-kite-mcp-pivot.md`](docs/superpowers/plans/2026-05-15-phase-13-5-kite-mcp-pivot.md).

- [x] 13.5.1 `src/trading/data/kite_snapshot.py`: `read_holdings/gtts/positions`
       readers + `KiteSnapshotMissingError` / `KiteSnapshotStaleError`. Reads
       `data/raw/<as_of>/<resource>.json` + `_meta.json` validation
       (date-equality). 7 new tests in `test_kite_snapshot.py`.
- [x] 13.5.2 `.claude/skills/kite-snapshot/SKILL.md`: project-level skill.
       Probes `mcp__kite__get_profile`; on 401 prompts user to run
       `mcp__kite__login` (no partial writes). Calls `mcp__kite__get_holdings`
       / `mcp__kite__get_gtts`; writes JSONs atomically + `_meta.json` with
       `source: "mcp"`.
- [x] 13.5.3 `src/trading/jobs/pre_open.py`: `_step_portfolio` reads from
       `kite_snapshot.read_holdings`. Drops `skip_kite` arg. New
       `PreOpenAborted` exception bubbles to CLI which exits 2 with
       remediation message. 2 new step tests; 1 new CLI abort test.
- [x] 13.5.4 `trading portfolio` CLI: drops SDK calls, reads from
       `kite_snapshot`. New required `--date` option. `KiteSnapshotMissingError`
       exits 2 with same remediation as pre-open. 2 new tests.
- [x] 13.5.5 Renamed `kite-login` → `kite-emergency-login`. Added
       `kite-emergency-snapshot --date` CLI: writes the same JSON contract
       as the skill but tags `_meta.source: "sdk-fallback"`. 2 new tests.
- [x] 13.5.6 Real-data smoke: `/kite-snapshot` writes JSONs from MCP;
       `trading pre-open` reads them; `holdings_scored` non-zero. PROGRESS
       updated; commit `feat(data): Phase 13.5 Kite MCP pivot` pushed to
       origin/main.
```

Update the pointers (currently say Phase 14):

```
**Currently working on:** _Phase 14 — mid_day + post_close jobs_
**Next up:** _Phase 12.6 — Sector data (deferred)_
```

(No change — the pointers were already correct.)

- [ ] **Step 6: Run full verification**

```bash
cd D:/Projects/Trading
uv run ruff check . && uv run mypy src/ && uv run pytest -q
```

Expected: clean. Test count: ~478 passed (463 + 7 kite_snapshot + 2 pre_open
portfolio + 2 portfolio CLI + 2 emergency CLI + 1 pre-open abort - 3 deleted
- 1 renamed = roughly +10 net).

- [ ] **Step 7: Commit + push**

```bash
cd D:/Projects/Trading
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(data): Phase 13.5 Kite MCP pivot

Production code now reads Kite data from JSON files written by the
/kite-snapshot Claude Code skill (which calls mcp__kite__*). The
kiteconnect SDK wrapper at src/trading/data/kite.py is kept as a
manual fallback, wired only into kite-emergency-{login,snapshot}
CLI commands.

Pre-open and trading portfolio now hard-halt with exit code 2 if
data/raw/<date>/holdings.json is missing or stale, with a clear
"Run /kite-snapshot first" message. The --skip-kite flag is gone.

Spec: docs/superpowers/specs/2026-05-15-phase-13-5-kite-mcp-pivot-design.md
Plan: docs/superpowers/plans/2026-05-15-phase-13-5-kite-mcp-pivot.md

Real-data smoke confirmed: /kite-snapshot wrote N holdings, pre-open
read them and scored health for each.

Tests: <count> passed, 1 skipped (live), ruff + mypy clean.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push origin main
```

Expected: push succeeds; the pivot is shipped.

---

## Self-review notes

- **Spec coverage:** §3.1 readers → Tasks 1+2. §3.2 skill → Task 3. §3.3 pre_open → Task 4. §3.4 CLI changes (pre-open / portfolio / kite-login rename / kite-emergency-snapshot) → Tasks 4+5+6. §4 file contract → enforced by Task 1's `_validate_meta` and the skill's atomic write rules. §5 error handling → tests in Tasks 1+4+5. §6 testing matrix → mapped 1:1. §7 PROGRESS.md placement → Task 7. ✓
- **Type consistency:** `KiteSnapshotMissingError` / `KiteSnapshotStaleError` defined in Task 1, imported in Tasks 4+5. `PreOpenAborted` defined in Task 4, used in Tasks 4+(test in 4). `seed_kite_snapshot(paths, as_of, *, holdings, gtts, positions, ...)` signature defined in Task 1, used in Tasks 4+5. `_step_portfolio` no longer accepts `skip_kite`; new keyword-only `as_of` added — call sites updated in Task 4. ✓
- **Placeholder scan:** every code block is concrete; commit messages written out (one judgement-call placeholder: smoke result counts in Task 7 are filled in only after Step 3 actually runs). ✓
- **Atomic write contract:** Task 6's `kite-emergency-snapshot` uses `tmp` + `replace`. The skill (Task 3) is instructed to do the same. Both writers therefore satisfy the spec's atomicity guarantee. ✓

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-15-phase-13-5-kite-mcp-pivot.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

**Which approach?**
