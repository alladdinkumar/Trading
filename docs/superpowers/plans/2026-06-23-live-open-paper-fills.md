# Live-Open Fills for the Paper Book — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill paper-trade entries at the live LTP in a new post-open block (`trading open-fills`) instead of at the previous close in pre-open.

**Architecture:** Pre-open becomes plan/record-only — it writes the funding-eligible candidates to `data/raw/<date>/_pending_entries.json` and opens nothing. A new two-phase `trading open-fills` command (prepare → `/kite-quotes-snapshot` → `--apply`) reads those pending entries, takes the live LTP as the entry, recomputes qty/stop/target from it via the unchanged `plan_daily_entries`, and opens the funded trades.

**Tech Stack:** Python 3.9+, UV, pytest, Typer CLI, SQLite. Spec: `docs/superpowers/specs/2026-06-23-live-open-paper-fills-design.md`.

## Global Constraints

- **Run commands with `PYTHONUTF8=1 uv run …`** (Windows console is cp1252; the repo's tables/emoji need UTF-8).
- **TDD:** write the failing test first, watch it fail, implement minimally, watch it pass, commit.
- **No network in tests:** use a temp DB + temp `Paths`; never hit Kite/yfinance.
- **`plan_daily_entries` is reused unchanged** — do not modify `strategy/daily_budget.py`.
- **Entry price = live LTP** = `Quote.last_price` (Kite's `Quote.close` is yesterday's close — never use it for entry).
- **Stop = `LTP − 1.5·atr_14`**, **target = `target_price(LTP, stop)`** (`trading.strategy.exits.target_price`).
- **ATR** is the D-1 `atr_14` carried in the handoff file — never recomputed intraday.

---

### Task 1: Shared position helpers (`deployed_by_symbol`, `already_opened_today`)

Move the two private helpers `_deployed_by_symbol` and `_already_opened_today` out of `pre_open.py` into `paper/positions.py` as public functions so `open_fills` can reuse them without importing pre-open privates.

**Files:**
- Modify: `src/trading/paper/positions.py` (append two functions)
- Modify: `src/trading/jobs/pre_open.py` (delete the two privates; import + call the public ones)
- Test: `tests/test_paper_positions.py` (append two tests)

**Interfaces:**
- Produces:
  - `deployed_by_symbol(conn: sqlite3.Connection) -> dict[str, float]` — cost-basis value of open paper positions, grouped by symbol.
  - `already_opened_today(conn: sqlite3.Connection, symbol: str, as_of: date) -> bool` — True if `symbol` has an OPEN paper-trade entered on `as_of`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_paper_positions.py`, append (reuse whatever DB fixture the file already uses to open a migrated in-memory/temp conn and insert a signal + open paper_trade; mirror the existing tests' setup helpers):

```python
from datetime import date
from trading.paper.positions import already_opened_today, deployed_by_symbol


def test_deployed_by_symbol_sums_open_cost_basis(tmp_conn):
    # tmp_conn: migrated sqlite conn with one OPEN trade NESTLEIND qty 2 @ 100.0
    _seed_open_trade(tmp_conn, symbol="NESTLEIND", entry=100.0, qty=2, ts_entry="2026-06-23T09:20:00")
    assert deployed_by_symbol(tmp_conn) == {"NESTLEIND": 200.0}


def test_already_opened_today_true_only_for_open_same_day(tmp_conn):
    _seed_open_trade(tmp_conn, symbol="TATASTEEL", entry=200.0, qty=1, ts_entry="2026-06-23T09:20:00")
    assert already_opened_today(tmp_conn, "TATASTEEL", date(2026, 6, 23)) is True
    assert already_opened_today(tmp_conn, "TATASTEEL", date(2026, 6, 22)) is False
    assert already_opened_today(tmp_conn, "POWERGRID", date(2026, 6, 23)) is False
```

If `tests/test_paper_positions.py` has no `_seed_open_trade`/`tmp_conn` helper, add a small local one using `trading.store.repo.insert_signal` + `insert_paper_trade` and `trading.store.db.get_conn` + `run_migrations`, matching the patterns in `tests/test_paper_ledger.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONUTF8=1 uv run pytest tests/test_paper_positions.py -k "deployed_by_symbol or already_opened_today" -v`
Expected: FAIL — `ImportError: cannot import name 'deployed_by_symbol'`.

- [ ] **Step 3: Implement the helpers in `paper/positions.py`**

Append to `src/trading/paper/positions.py` (note the existing module already imports `sqlite3` and `date`):

```python
def deployed_by_symbol(conn: sqlite3.Connection) -> dict[str, float]:
    """Cost-basis value of open paper positions, grouped by symbol."""
    rows = conn.execute(
        "SELECT s.symbol AS symbol, SUM(pt.entry_price * pt.qty) AS deployed "
        "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL GROUP BY s.symbol"
    ).fetchall()
    return {r["symbol"]: float(r["deployed"]) for r in rows}


def already_opened_today(conn: sqlite3.Connection, symbol: str, as_of: date) -> bool:
    """True if `symbol` has an OPEN paper-trade entered on `as_of`."""
    row = conn.execute(
        "SELECT 1 FROM paper_trades pt "
        "JOIN signals s ON s.id = pt.signal_id "
        "WHERE s.symbol = ? AND substr(pt.ts_entry, 1, 10) = ? "
        "  AND pt.ts_exit IS NULL "
        "LIMIT 1",
        (symbol, as_of.isoformat()),
    ).fetchone()
    return row is not None
```

- [ ] **Step 4: Point `pre_open.py` at the shared helpers**

In `src/trading/jobs/pre_open.py`: delete the private `_deployed_by_symbol` (currently ~lines 513-520) and `_already_opened_today` (~lines 523-533). Add the import:

```python
from trading.paper.positions import already_opened_today, deployed_by_symbol
```

Replace the two call sites inside `_step_auto_open`:
- `deployed_by_symbol = _deployed_by_symbol(conn)` → `deployed = deployed_by_symbol(conn)` (rename the local to avoid shadowing the imported function), and pass `deployed_by_symbol=deployed` into `plan_daily_entries`.
- `if _already_opened_today(conn, cand.symbol, as_of):` → `if already_opened_today(conn, cand.symbol, as_of):`

- [ ] **Step 5: Run the full pre_open + positions tests**

Run: `PYTHONUTF8=1 uv run pytest tests/test_paper_positions.py tests/test_jobs_pre_open.py -v`
Expected: PASS (pre_open behavior unchanged — pure refactor).

- [ ] **Step 6: Commit**

```bash
git add src/trading/paper/positions.py src/trading/jobs/pre_open.py tests/test_paper_positions.py
git commit -m "refactor(paper): promote deployed_by_symbol/already_opened_today to positions.py

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Pending-entries handoff IO (`paper/pending.py`)

The file pre-open writes and open-fills reads.

**Files:**
- Create: `src/trading/paper/pending.py`
- Test: `tests/test_paper_pending.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PendingEntry: symbol: str; atr_14: float; ml_score: float | None; ref_close: float`
  - `class PendingEntriesMissingError(RuntimeError)`
  - `write_pending_entries(paths: Paths, as_of: date, *, regime: str, entries: list[PendingEntry]) -> Path` — writes `data/raw/<date>/_pending_entries.json`, returns the path.
  - `read_pending_entries(paths: Paths, as_of: date) -> tuple[str, list[PendingEntry]]` — returns `(regime, entries)`; raises `PendingEntriesMissingError` if the file is absent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_paper_pending.py`:

```python
from datetime import date

import pytest

from trading.config import Paths
from trading.paper.pending import (
    PendingEntriesMissingError,
    PendingEntry,
    read_pending_entries,
    write_pending_entries,
)


def _paths(tmp_path) -> Paths:
    return Paths(
        data_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        research_dir=tmp_path / "research",
        db_path=tmp_path / "app.db",
    )


def test_write_then_read_roundtrips(tmp_path):
    paths = _paths(tmp_path)
    entries = [
        PendingEntry(symbol="TATASTEEL", atr_14=4.30, ml_score=0.61, ref_close=198.97),
        PendingEntry(symbol="COALINDIA", atr_14=11.2, ml_score=None, ref_close=449.0),
    ]
    out = write_pending_entries(paths, date(2026, 6, 23), regime="NEUTRAL", entries=entries)
    assert out.name == "_pending_entries.json"

    regime, got = read_pending_entries(paths, date(2026, 6, 23))
    assert regime == "NEUTRAL"
    assert got == entries


def test_read_missing_raises(tmp_path):
    with pytest.raises(PendingEntriesMissingError):
        read_pending_entries(_paths(tmp_path), date(2026, 6, 23))
```

Adjust the `Paths(...)` keyword names to match the real `trading.config.Paths` dataclass fields (check `src/trading/config.py`); the four shown are illustrative.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_paper_pending.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.paper.pending'`.

- [ ] **Step 3: Implement `paper/pending.py`**

```python
"""Handoff file between pre-open (writes) and open-fills (reads).

Pre-open selects the day's funding-eligible candidates but no longer opens
trades at the previous close; it records them here so the post-open
`open-fills` block can fill them at the live LTP. One JSON file per date:
`data/raw/<date>/_pending_entries.json`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from trading.config import Paths

_FILENAME = "_pending_entries.json"


class PendingEntriesMissingError(RuntimeError):
    """Raised when no _pending_entries.json exists for the requested date."""


@dataclass(frozen=True)
class PendingEntry:
    """A funding-eligible candidate from pre-open, awaiting a live-LTP fill."""

    symbol: str
    atr_14: float
    ml_score: float | None
    ref_close: float  # D-1 close, kept only for the open_fills drift report


def _path(paths: Paths, as_of: date) -> Path:
    return paths.raw_dir / as_of.isoformat() / _FILENAME


def write_pending_entries(
    paths: Paths,
    as_of: date,
    *,
    regime: str,
    entries: list[PendingEntry],
) -> Path:
    """Write the pending entries for `as_of`; returns the file path."""
    out = _path(paths, as_of)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": as_of.isoformat(),
        "regime": regime,
        "entries": [asdict(e) for e in entries],
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def read_pending_entries(paths: Paths, as_of: date) -> tuple[str, list[PendingEntry]]:
    """Read `(regime, entries)` for `as_of`. Raises PendingEntriesMissingError."""
    src = _path(paths, as_of)
    if not src.is_file():
        raise PendingEntriesMissingError(
            f"No pending entries for {as_of.isoformat()} ({src}). "
            "Run `trading pre-open` first."
        )
    payload = json.loads(src.read_text(encoding="utf-8"))
    entries = [
        PendingEntry(
            symbol=e["symbol"],
            atr_14=float(e["atr_14"]),
            ml_score=(None if e["ml_score"] is None else float(e["ml_score"])),
            ref_close=float(e["ref_close"]),
        )
        for e in payload["entries"]
    ]
    return str(payload["regime"]), entries
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_paper_pending.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading/paper/pending.py tests/test_paper_pending.py
git commit -m "feat(paper): _pending_entries.json handoff (pre-open -> open-fills)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Pre-open writes the handoff and opens nothing

Repurpose `_step_auto_open` → `_step_plan_and_record`: log visibility signals for non-selected candidates (unchanged), write the selected/eligible candidates to the handoff file, and **open zero trades**. Funding now happens in open-fills.

**Files:**
- Modify: `src/trading/jobs/pre_open.py` (`_step_auto_open` body + its call site in `run_pre_open`; `PreOpenResult`)
- Test: `tests/test_jobs_pre_open.py`

**Interfaces:**
- Consumes: `PendingEntry`, `write_pending_entries` (Task 2); `deployed_by_symbol`, `already_opened_today` (Task 1).
- Produces: `run_pre_open(...) -> PreOpenResult` with `paper_trades_opened == 0` always, and a new field `pending_entries: int` (count written to the handoff file).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_jobs_pre_open.py` (reuse the file's existing harness that builds parquet bars + a migrated DB and runs `run_pre_open`; the assertion is the new behavior):

```python
def test_pre_open_writes_pending_and_opens_nothing(pre_open_env):
    # pre_open_env: existing fixture/helper that seeds bars + DB so >=1 candidate is selected
    result = run_pre_open(pre_open_env.as_of, paths=pre_open_env.paths, require_snapshot=False)

    # No paper trades opened at pre-open anymore.
    assert result.paper_trades_opened == 0
    with get_conn(pre_open_env.paths.db_path) as conn:
        opened = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE substr(ts_entry,1,10)=?",
            (pre_open_env.as_of.isoformat(),),
        ).fetchone()[0]
    assert opened == 0

    # The selected candidates were recorded for the open-fills block.
    regime, entries = read_pending_entries(pre_open_env.paths, pre_open_env.as_of)
    assert result.pending_entries == len(entries)
    assert entries  # at least one eligible candidate
    assert all(e.atr_14 > 0 for e in entries)
```

Import at the top of the test module: `from trading.paper.pending import read_pending_entries` and `from trading.store.db import get_conn`. If an existing test asserted `paper_trades_opened >= 1` for pre-open, update it to `== 0` and assert pending entries instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_jobs_pre_open.py -k pending_and_opens_nothing -v`
Expected: FAIL — `AttributeError: 'PreOpenResult' object has no attribute 'pending_entries'` (and/or opened > 0).

- [ ] **Step 3: Add the `pending_entries` field to `PreOpenResult`**

In `src/trading/jobs/pre_open.py`, add to the `PreOpenResult` dataclass (after `paper_trades_opened`):

```python
    pending_entries: int = 0
```

- [ ] **Step 4: Rewrite `_step_auto_open` as `_step_plan_and_record`**

Replace the whole `_step_auto_open` function (currently ~lines 396-510) with the version below. It keeps candidate scoring, visibility-signal logging for non-selected, and the ATR/stop guard, but **writes the handoff file instead of opening trades**. Remove the `plan_daily_entries`/`plan.entries`/`plan.skipped`/`log_signal_and_open_trade` machinery from pre-open (it moves to open-fills, Task 4).

```python
def _step_plan_and_record(
    conn: sqlite3.Connection,
    as_of: date,
    scored: list[ScoredCandidate],
    regime: Regime,
    warnings: list[str],
    paths: Paths,
) -> int:
    """Log visibility signals for non-selected candidates and record the
    funding-eligible (selected) ones to `_pending_entries.json` for the
    post-open `open-fills` block. Opens no paper trades — the live-LTP fill
    happens after market open, not at D-1 close (see the 2026-06-23 design).
    Returns the number of pending entries written.
    """
    pending: list[PendingEntry] = []
    for sc in scored:
        cand = sc.candidate
        stop_price = cand.close - 1.5 * cand.atr_14
        if cand.close <= stop_price:
            warnings.append(f"{cand.symbol}: ATR={cand.atr_14:.2f} ≥ close — skip")
            continue
        if not sc.selected:
            # Visibility-only: log the signal (with ml_score) but don't trade.
            signal_target = target_price(cand.close, stop_price)
            insert_signal(
                conn,
                Signal(
                    id=None,
                    ts=f"{as_of.isoformat()}T08:30:00",
                    symbol=cand.symbol,
                    side="LONG",
                    entry=cand.close,
                    stop=stop_price,
                    target=signal_target,
                    horizon_days=25,
                    rules_passed_json=json.dumps(
                        [r.name for r in cand.rules if r.passed]
                    ),
                    ml_score=sc.ml_score,
                    conviction=conviction_from_score(sc.ml_score),
                    created_by="pre_open",
                ),
            )
            continue
        if already_opened_today(conn, cand.symbol, as_of):
            continue
        pending.append(
            PendingEntry(
                symbol=cand.symbol,
                atr_14=cand.atr_14,
                ml_score=sc.ml_score,
                ref_close=cand.close,
            )
        )

    write_pending_entries(paths, as_of, regime=regime.value, entries=pending)
    return len(pending)
```

Add imports near the top of `pre_open.py`: `from trading.paper.pending import PendingEntry, write_pending_entries`. Remove the now-unused `log_signal_and_open_trade`, `BudgetCandidate`, `plan_daily_entries`, `build_score_calibration`, `matured_score_outcomes`, `load_sector_map`, `negative_news_count_7d`, and `compute_paper_cash` imports **only if** nothing else in `pre_open.py` uses them (grep first — `negative_news_count_7d`/`load_sector_map` may be used elsewhere in the file; if so, leave them).

Note on `regime.value`: `Regime` is an enum (`trading.features.regime.Regime`); `.value` yields the string (e.g. `"NEUTRAL"`). Confirm with `python -c "from trading.features.regime import Regime; print(list(Regime))"` and use `.value` (or `.name` if values aren't the human strings).

- [ ] **Step 5: Update the call site + result in `run_pre_open`**

Replace the `opened = _step_auto_open(...)` call (~lines 149-158) with:

```python
        pending_count = _step_plan_and_record(
            conn,
            as_of,
            scored,
            regime,
            warnings,
            p,
        )
```

In the `return PreOpenResult(...)`: set `paper_trades_opened=0,` and add `pending_entries=pending_count,`.

- [ ] **Step 6: Run pre-open tests**

Run: `PYTHONUTF8=1 uv run pytest tests/test_jobs_pre_open.py -v`
Expected: PASS (update any other test that assumed pre-open opens trades — assert `pending_entries` instead).

- [ ] **Step 7: Commit**

```bash
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(pre-open): record pending entries, stop opening at prev close

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `trading open-fills` orchestrator (`jobs/open_fills.py`)

Two-phase, mirroring `jobs/mid_day.py`: prepare writes `_quote_symbols.txt` from the pending entries; apply reads the live quotes, re-plans at LTP, opens funded trades, writes `open_fills.md`.

**Files:**
- Create: `src/trading/jobs/open_fills.py`
- Test: `tests/test_jobs_open_fills.py`

**Interfaces:**
- Consumes: `read_pending_entries`, `PendingEntry` (Task 2); `deployed_by_symbol`, `already_opened_today` (Task 1); `plan_daily_entries`, `BudgetCandidate` (`strategy/daily_budget.py`); `read_latest_quotes` (`data/quotes_snapshot.py`); `log_signal_and_open_trade` (`paper/ledger.py`); `compute_paper_cash` (`paper/reconcile.py`); `target_price`, `conviction_from_score`, `EntryAttribution`, `Signal`, `insert_signal`.
- Produces:
  - `class OpenFillsAborted(RuntimeError)`
  - `@dataclass(frozen=True) class OpenFillsResult: as_of; symbols_path; update_path; quotes_capture_ts; trades_opened; trades_skipped; warnings`
  - `run_open_fills(as_of: date, *, paths: Paths | None = None, apply: bool = False) -> OpenFillsResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jobs_open_fills.py`. Use the same DB/Paths fixtures the mid-day tests use (`tests/test_jobs_mid_day.py` is the closest template — copy its quote-snapshot-writing helper).

```python
import json
from datetime import date

import pytest

from trading.jobs.open_fills import OpenFillsAborted, run_open_fills
from trading.paper.pending import PendingEntry, write_pending_entries
from trading.store.db import get_conn


def _write_quotes(paths, as_of, hhmm, ltp_by_symbol):
    """Write data/raw/<date>/quotes_<hhmm>.json with current LTP per symbol."""
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    rows = [
        {"tradingsymbol": sym, "instrument_token": i, "last_price": ltp,
         "open": ltp, "high": ltp, "low": ltp, "close": ltp,
         "volume": 0, "buy_quantity": 0, "sell_quantity": 0}
        for i, (sym, ltp) in enumerate(ltp_by_symbol.items(), start=1)
    ]
    (base / f"quotes_{hhmm}.json").write_text(json.dumps(rows), encoding="utf-8")


def test_prepare_writes_quote_symbols(open_fills_env):
    p = open_fills_env.paths
    write_pending_entries(p, open_fills_env.as_of, regime="NEUTRAL", entries=[
        PendingEntry(symbol="TATASTEEL", atr_14=4.30, ml_score=0.6, ref_close=198.97),
    ])
    result = run_open_fills(open_fills_env.as_of, paths=p, apply=False)
    text = result.symbols_path.read_text(encoding="utf-8")
    assert "TATASTEEL" in text.split()


def test_apply_opens_at_live_ltp_with_recomputed_stop(open_fills_env):
    p = open_fills_env.paths
    as_of = open_fills_env.as_of
    write_pending_entries(p, as_of, regime="NEUTRAL", entries=[
        PendingEntry(symbol="TATASTEEL", atr_14=4.0, ml_score=0.6, ref_close=198.97),
    ])
    _write_quotes(p, as_of, "0920", {"TATASTEEL": 205.0})  # gapped up from 198.97

    result = run_open_fills(as_of, paths=p, apply=True)
    assert result.trades_opened == 1
    with get_conn(p.db_path) as conn:
        row = conn.execute(
            "SELECT pt.entry_price, pt.current_stop, pt.qty "
            "FROM paper_trades pt JOIN signals s ON s.id=pt.signal_id "
            "WHERE s.symbol='TATASTEEL' AND pt.ts_exit IS NULL"
        ).fetchone()
    assert row["entry_price"] == pytest.approx(205.0)          # live LTP, not 198.97
    assert row["current_stop"] == pytest.approx(205.0 - 1.5 * 4.0)  # stop from LTP
    assert row["qty"] >= 1


def test_apply_skips_when_ltp_below_stop(open_fills_env):
    p = open_fills_env.paths
    as_of = open_fills_env.as_of
    # atr so large that LTP - 1.5*atr >= LTP is impossible, but LTP <= stop when
    # stop computed from a different basis — here force LTP under a wide stop:
    write_pending_entries(p, as_of, regime="NEUTRAL", entries=[
        PendingEntry(symbol="WIDE", atr_14=1000.0, ml_score=0.6, ref_close=100.0),
    ])
    _write_quotes(p, as_of, "0920", {"WIDE": 100.0})  # 100 - 1500 < 100, stop ok;
    # Instead assert the inverse guard: a symbol whose LTP <= computed stop is skipped.
    result = run_open_fills(as_of, paths=p, apply=True)
    assert result.trades_opened == 0
    assert any("WIDE" in w for w in result.warnings)


def test_apply_missing_quotes_aborts(open_fills_env):
    p = open_fills_env.paths
    write_pending_entries(p, open_fills_env.as_of, regime="NEUTRAL", entries=[
        PendingEntry(symbol="TATASTEEL", atr_14=4.0, ml_score=0.6, ref_close=198.97),
    ])
    with pytest.raises(OpenFillsAborted):
        run_open_fills(open_fills_env.as_of, paths=p, apply=True)  # no quotes_*.json


def test_apply_no_pending_is_noop(open_fills_env):
    result = run_open_fills(open_fills_env.as_of, paths=open_fills_env.paths, apply=True)
    assert result.trades_opened == 0
```

Provide an `open_fills_env` fixture (a migrated temp DB via `get_conn` + `run_migrations`, a temp `Paths`, and `as_of = date(2026, 6, 23)`). For the LTP-below-stop case, simplify the guard test: pick `atr_14` and LTP so that `LTP <= LTP - 1.5*atr_14` can never hold (it can't, since atr>0) — therefore the real guard to test is **negative/zero stop or non-positive qty**. Re-spec this test during implementation to the actual guard: assert that a symbol the planner funds **zero qty** for (e.g. LTP so high one unit exceeds the ₹7k daily cap and 50% per-stock cap) is reported as skipped with a reason. Use a pending symbol with LTP `= 9_000_000` so `qty == 0`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONUTF8=1 uv run pytest tests/test_jobs_open_fills.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.jobs.open_fills'`.

- [ ] **Step 3: Implement `jobs/open_fills.py`**

```python
"""Post-open block — fill paper entries at the live LTP (2026-06-23 design).

Two-phase invocation, mirroring jobs/mid_day.py:
  prepare → read _pending_entries.json → write _quote_symbols.txt
  /kite-quotes-snapshot (out-of-process) → quotes_HHMM.json
  apply → live LTP → re-plan at LTP → open funded trades → open_fills.md
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trading.config import Paths, get_paths
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)
from trading.data.sector import load_sector_map
from trading.ops.logging_setup import configure_logging
from trading.paper.ledger import log_signal_and_open_trade
from trading.paper.pending import (
    PendingEntriesMissingError,
    PendingEntry,
    read_pending_entries,
)
from trading.paper.positions import already_opened_today, deployed_by_symbol
from trading.paper.reconcile import compute_paper_cash
from trading.ranking.ranker import conviction_from_score
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.news_store import negative_news_count_7d
from trading.store.repo import EntryAttribution, Signal, insert_signal
from trading.strategy.calibration import build_score_calibration
from trading.store.repo import matured_score_outcomes
from trading.strategy.daily_budget import BudgetCandidate, plan_daily_entries
from trading.strategy.exits import target_price
from trading.features.regime import Regime


class OpenFillsAborted(RuntimeError):  # noqa: N818
    """Raised when apply mode cannot proceed (no fresh quotes)."""


@dataclass(frozen=True)
class OpenFillsResult:
    as_of: date
    symbols_path: Path | None
    update_path: Path | None
    quotes_capture_ts: datetime | None
    trades_opened: int
    trades_skipped: int
    warnings: list[str] = field(default_factory=list)


def run_open_fills(
    as_of: date,
    *,
    paths: Paths | None = None,
    apply: bool = False,
) -> OpenFillsResult:
    p = paths if paths is not None else get_paths()
    warnings: list[str] = []

    # No pending file → graceful no-op in both phases.
    try:
        regime_str, pending = read_pending_entries(p, as_of)
    except PendingEntriesMissingError as e:
        warnings.append(str(e))
        return OpenFillsResult(as_of, None, None, None, 0, 0, warnings)

    if not apply:
        base = p.raw_dir / as_of.isoformat()
        base.mkdir(parents=True, exist_ok=True)
        symbols_path = base / "_quote_symbols.txt"
        symbols = sorted({e.symbol for e in pending})
        symbols_path.write_text("\n".join(symbols) + "\n", encoding="utf-8")
        return OpenFillsResult(as_of, symbols_path, None, None, 0, 0, warnings)

    # apply mode
    try:
        quotes, capture_ts = read_latest_quotes(p, as_of)
    except (QuoteSnapshotMissingError, QuoteSnapshotStaleError) as e:
        raise OpenFillsAborted(str(e)) from e

    regime = Regime(regime_str)
    sector_map = load_sector_map()
    opened = 0
    skipped = 0
    opened_rows: list[tuple[str, float, int, float]] = []  # sym, ltp, qty, ref_close

    with get_conn(p.db_path) as conn:
        run_migrations(conn)

        budget_cands: list[BudgetCandidate] = []
        meta: dict[str, PendingEntry] = {}
        for e in pending:
            q = quotes.get(e.symbol)
            if q is None:
                warnings.append(f"{e.symbol}: no live quote — skipped")
                skipped += 1
                continue
            ltp = float(q.last_price)
            stop = ltp - 1.5 * e.atr_14
            if ltp <= stop:
                warnings.append(f"{e.symbol}: LTP {ltp:.2f} <= stop {stop:.2f} — skipped")
                skipped += 1
                continue
            if already_opened_today(conn, e.symbol, as_of):
                continue
            budget_cands.append(
                BudgetCandidate(
                    symbol=e.symbol,
                    entry=ltp,
                    stop=stop,
                    target=target_price(ltp, stop),
                    ml_score=e.ml_score,
                )
            )
            meta[e.symbol] = e

        p_win_cal = build_score_calibration(matured_score_outcomes(conn))
        plan = plan_daily_entries(
            budget_cands,
            available_cash=compute_paper_cash(conn, as_of=as_of),
            deployed_by_symbol=deployed_by_symbol(conn),
            regime=regime,
            p_win_calibration=p_win_cal,
        )

        planned = {pe.symbol for pe in plan.entries}
        for pe in plan.entries:
            e = meta[pe.symbol]
            signal = Signal(
                id=None,
                ts=capture_ts.isoformat(),
                symbol=pe.symbol,
                side="LONG",
                entry=pe.entry,
                stop=pe.stop,
                target=pe.target,
                horizon_days=25,
                rules_passed_json="[]",
                ml_score=e.ml_score,
                conviction=conviction_from_score(e.ml_score),
                created_by="open_fills",
            )
            log_signal_and_open_trade(
                conn,
                signal=signal,
                entry_ts=capture_ts,
                entry_price=pe.entry,
                qty=pe.qty,
                atr_at_entry=e.atr_14,
                attribution=EntryAttribution(
                    regime=regime,
                    sector=sector_map.get(pe.symbol),
                    neg_news_7d=negative_news_count_7d(conn, pe.symbol, as_of),
                ),
            )
            opened += 1
            opened_rows.append((pe.symbol, pe.entry, pe.qty, e.ref_close))

        for symbol, reason in plan.skipped:
            if symbol in planned:
                continue
            e = meta.get(symbol)
            if e is not None:
                insert_signal(
                    conn,
                    Signal(
                        id=None,
                        ts=capture_ts.isoformat(),
                        symbol=symbol,
                        side="LONG",
                        entry=float(quotes[symbol].last_price),
                        stop=float(quotes[symbol].last_price) - 1.5 * e.atr_14,
                        target=target_price(
                            float(quotes[symbol].last_price),
                            float(quotes[symbol].last_price) - 1.5 * e.atr_14,
                        ),
                        horizon_days=25,
                        rules_passed_json="[]",
                        ml_score=e.ml_score,
                        conviction=conviction_from_score(e.ml_score),
                        created_by="open_fills",
                    ),
                )
            warnings.append(f"{symbol}: not opened — {reason}")
            skipped += 1

    update_dir = p.research_dir / as_of.isoformat()
    update_dir.mkdir(parents=True, exist_ok=True)
    update_path = update_dir / "open_fills.md"
    update_path.write_text(
        _render_open_fills(capture_ts, opened_rows, warnings), encoding="utf-8"
    )

    return OpenFillsResult(
        as_of=as_of,
        symbols_path=None,
        update_path=update_path,
        quotes_capture_ts=capture_ts,
        trades_opened=opened,
        trades_skipped=skipped,
        warnings=warnings,
    )


def _render_open_fills(
    capture_ts: datetime,
    opened: list[tuple[str, float, int, float]],
    warnings: list[str],
) -> str:
    lines = [
        f"## Open-fills — filled at live LTP, captured "
        f"{capture_ts.isoformat(timespec='seconds')}",
        "",
        "| symbol | LTP | qty | prev close | drift |",
        "|---|---|---|---|---|",
    ]
    for sym, ltp, qty, ref in opened:
        drift = (ltp - ref) / ref * 100.0 if ref else 0.0
        lines.append(f"| {sym} | {ltp:.2f} | {qty} | {ref:.2f} | {drift:+.2f}% |")
    lines.append("")
    lines.append(f"{len(opened)} trade(s) opened at live LTP.")
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"- {w}")
    return "\n".join(lines) + "\n"


def _main(date_str: str, apply: bool = False) -> None:  # pragma: no cover
    configure_logging("open_fills")
    from loguru import logger

    try:
        result = run_open_fills(date.fromisoformat(date_str), apply=apply)
    except OpenFillsAborted as e:
        print(f"Open-fills aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("open_fills failed")
        raise
    if result.symbols_path:
        print(f"wrote {result.symbols_path}")
        print("Now run /kite-quotes-snapshot skill, then re-run with --apply")
    if result.update_path:
        print(f"wrote {result.update_path} — opened {result.trades_opened}")


if __name__ == "__main__":  # pragma: no cover
    import typer

    typer.run(_main)
```

Note: confirm `Quote` field names with `tests/test_jobs_mid_day.py` and `data/snapshot_schema.py` — the `_write_quotes` helper's row keys must satisfy `validate_row(Quote, …)`. Drop any keys the schema rejects; add any it requires.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONUTF8=1 uv run pytest tests/test_jobs_open_fills.py -v`
Expected: PASS (adjust the skip-guard test per Step 1's note so it exercises a real `qty==0`/planner-skip path).

- [ ] **Step 5: Commit**

```bash
git add src/trading/jobs/open_fills.py tests/test_jobs_open_fills.py
git commit -m "feat(open-fills): two-phase live-LTP paper fill block

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `trading open-fills` CLI command

**Files:**
- Modify: `src/trading/cli.py` (import + new `@app.command("open-fills")`)
- Test: `tests/test_cli.py` (append a CliRunner test)

**Interfaces:**
- Consumes: `run_open_fills`, `OpenFillsAborted`, `OpenFillsResult` (Task 4).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py` (mirror the existing mid-day CLI test — patch `trading.cli.run_open_fills` to return a stub `OpenFillsResult`, assert exit 0 and the printed path):

```python
def test_open_fills_prepare_prints_symbols_path(monkeypatch, tmp_path):
    from trading.jobs.open_fills import OpenFillsResult

    sp = tmp_path / "_quote_symbols.txt"
    sp.write_text("TATASTEEL\n", encoding="utf-8")
    monkeypatch.setattr(
        "trading.cli.run_open_fills",
        lambda as_of, apply=False: OpenFillsResult(
            as_of=as_of, symbols_path=sp, update_path=None,
            quotes_capture_ts=None, trades_opened=0, trades_skipped=0, warnings=[],
        ),
    )
    result = runner.invoke(app, ["open-fills", "--date", "2026-06-23"])
    assert result.exit_code == 0
    assert "_quote_symbols.txt" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_cli.py -k open_fills -v`
Expected: FAIL — no command `open-fills` (exit code 2 / usage error).

- [ ] **Step 3: Wire the command in `cli.py`**

Add the import near the other job imports (alongside `from trading.jobs.mid_day import MidDayAborted, run_mid_day`):

```python
from trading.jobs.open_fills import OpenFillsAborted, run_open_fills
```

Add the command (place it after `mid_day_cmd`):

```python
@app.command("open-fills")
def open_fills_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Apply mode: read live quotes + open funded entries at LTP. "
            "Without --apply runs prepare mode.",
        ),
    ] = False,
) -> None:
    """Post-open block — fill paper entries at the live LTP. Two-phase:
    prepare → /kite-quotes-snapshot → apply."""
    as_of = date.fromisoformat(date_str)
    try:
        result = run_open_fills(as_of, apply=apply)
    except OpenFillsAborted as e:
        console.print(f"[red]Open-fills aborted:[/red] {e}")
        raise typer.Exit(code=2) from e

    if result.symbols_path is not None:
        console.print(f"[green]wrote[/green] {result.symbols_path}")
        console.print(
            "[bold]Now run /kite-quotes-snapshot skill in Claude Code, "
            f"then `trading open-fills --date {date_str} --apply`[/bold]"
        )
        return

    table = Table(title=f"open-fills {as_of.isoformat()}", show_header=True)
    table.add_column("step")
    table.add_column("count", justify="right")
    table.add_row("quotes_captured_at", str(result.quotes_capture_ts))
    table.add_row("trades_opened", str(result.trades_opened))
    table.add_row("trades_skipped", str(result.trades_skipped))
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")
    if result.update_path:
        console.print(f"[green]wrote[/green] {result.update_path}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_cli.py -k open_fills -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading open-fills command (prepare/apply)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Add the open-fills block to the daily-workflow skill

**Files:**
- Modify: `.claude/skills/daily-workflow/SKILL.md`

- [ ] **Step 1: Insert the block doc**

In `.claude/skills/daily-workflow/SKILL.md`, add a new block section between the **IEP block** and the **Mid-day block**:

```markdown
### Open-fills block — window 09:15–09:25 · done-marker `data/research/<date>/open_fills.md`

| Step | Command | Kind | Writes |
|---|---|---|---|
| 1/3 | `trading open-fills --date <date>` | CLI (prepare) | `data/raw/<date>/_quote_symbols.txt` (from `_pending_entries.json`) |
| 2/3 | `/kite-quotes-snapshot` | MCP skill | `data/raw/<date>/quotes_HHMM.json` — **halt point** if Kite session dead |
| 3/3 | `trading open-fills --date <date> --apply` | CLI | `data/research/<date>/open_fills.md`; opens funded entries at live LTP |

This is where the day's paper entries actually open — pre-open only *records*
the funding-eligible candidates to `_pending_entries.json`; the fill happens
here at the live LTP (per the 2026-06-23 live-open-fills design). If pre-open
recorded no pending entries, prepare is a no-op and nothing opens.
```

Update the block-ordering line under "Re-arming the next wake-up" to read: pre-open/IEP → **open-fills (09:15)** → mid-day (12:25) → post-close (16:05).

- [ ] **Step 2: Verify the skill still reads coherently**

Run: `PYTHONUTF8=1 uv run pytest -q` (full suite — no regressions) and re-read the edited SKILL.md section for flow.
Expected: suite PASS.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/daily-workflow/SKILL.md
git commit -m "docs(daily-workflow): add open-fills block (live-LTP paper fills)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Live LTP entry → Task 4 (`entry=ltp` from `q.last_price`). ✓
- Recompute qty/stop/target → Task 4 (`stop=ltp-1.5*atr`, `target_price`, `plan_daily_entries` at LTP). ✓
- New two-phase block → Tasks 4 + 5. ✓
- Pre-open plan/record-only, opens nothing → Task 3. ✓
- `_pending_entries.json` handoff → Task 2. ✓
- Signal ownership (pre-open: non-selected only; open-fills: funded + selected-unfunded) → Tasks 3 + 4. ✓
- Guards: LTP≤stop skip, already-opened idempotency → Task 4. ✓
- Done-marker `open_fills.md` → Task 4. ✓
- Kite-dead halt → Task 4 (`OpenFillsAborted`). ✓
- SKILL.md block → Task 6. ✓
- Out-of-scope (no historical migration, no budget-math change) → honored (Task 1 only relocates helpers). ✓

**Placeholder scan:** The LTP-below-stop guard test in Task 4 Step 1 is explicitly flagged as needing a real `qty==0` path (since `ltp <= ltp - 1.5*atr` is unreachable for atr>0). Resolve it as noted during implementation — not a silent TODO.

**Type consistency:** `PendingEntry` fields (symbol, atr_14, ml_score, ref_close) match across Tasks 2/3/4. `OpenFillsResult` fields match between Task 4 and the Task 5 CLI test stub. `plan_daily_entries`/`BudgetCandidate`/`PlannedEntry` names match `daily_budget.py`. `Regime(regime_str)` round-trips the `.value` written in Task 3.
