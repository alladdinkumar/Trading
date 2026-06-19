# Paper Portfolio + Funds Tracking + Journal Deviation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the paper-trading side a Kite-portfolio-style dashboard (per-symbol holdings + summary), separately-tracked funds (initial ₹1L + top-ups), and bought/target/deviation columns in the Paper Journal — all additive, retaining existing data.

**Architecture:** Three pure, DB-driven units on top of the existing paper layer — `paper/funds.py` (deposits ledger), `paper/positions.py` (per-symbol aggregation + summary from open trades and snapshot marks), `paper/journal.py` (trading-day date helpers). A new `cash_ledger` SQLite table (migration v5) holds top-ups; `compute_paper_cash` adds the ledger sum to its seed. CLI gains a `funds` sub-app; the Streamlit UI renames `1_Portfolio.py`→`1_Kite_Portfolio.py`, adds `4_Paper_Portfolio.py`, and extends the Journal + `ui/data.py` loaders.

**Tech Stack:** Python 3.11, SQLite (`sqlite3`), Typer + Rich (CLI), Streamlit + pandas (UI), numpy (trading-day math), pytest. Package manager UV (`uv run pytest` / `uv run ruff` / `uv run mypy`).

## Global Constraints

- Every change is **additive** — no existing table altered, no backfill; with an empty `cash_ledger` and no new rows, cash math and the equity curve are byte-identical to today.
- The initial ₹1,00,000 stays the `INITIAL_CAPITAL = 100_000.0` seed constant in `src/trading/paper/reconcile.py` — it is **not** a ledger row.
- Price source is the **latest DB snapshot mark (offline)** — never a live/network call. Pages are captioned "as of last close".
- Funds are **deposits only** (no withdrawals/negative entries); `add_funds` rejects `amount <= 0`.
- Deviation is **trading-day based** (`numpy.busday_offset` / `numpy.busday_count`), consistent with `mtm._days_held`.
- TDD throughout (red → green → refactor). Existing suite (950 passed) stays green; `uv run ruff check .` and `uv run mypy src/` clean.
- Page naming copy: real broker view = **"Kite Portfolio"**; paper view = **"Paper Portfolio"**.
- Do NOT stage or commit pre-existing untracked files (`.mcp.json`, `CLAUDE.md`, `Research/`, `data/README.md`, `data/mutual_funds_holdings.md`, `docs/daily-workflow.md`) or the uncommitted F-005 note. After each task's commit, run the F-005 leak check — `git diff HEAD~1` piped through `grep -c` for the two F-005 sentinel phrases (the "real-money" and "suspended-indefinitely" wording from the FINDINGS note) — and confirm it counts `0`.
- Commit after each task; push to `origin/main` at the end of the phase.

---

### Task 1: Migration v5 — `cash_ledger` table

**Files:**
- Modify: `src/trading/store/migrations.py` (bump `CURRENT_VERSION`, add `SCHEMA_V5`, add branch in `run_migrations`)
- Test: `tests/test_migrations.py` (append; create if absent)

**Interfaces:**
- Consumes: existing `run_migrations(conn) -> int`, `CURRENT_VERSION`.
- Produces: a `cash_ledger` table with columns `id` (PK AUTOINCREMENT), `date` (TEXT), `amount` (REAL), `note` (TEXT NULL), `created_at` (TEXT); index `idx_cash_ledger_date`. `run_migrations` now returns `5`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_migrations.py` (if the file does not exist, create it with the imports shown):

```python
from __future__ import annotations

import sqlite3

from trading.store.migrations import CURRENT_VERSION, run_migrations


def test_migration_creates_cash_ledger_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    version = run_migrations(conn)
    assert version == CURRENT_VERSION
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(cash_ledger)").fetchall()
    }
    assert cols == {"id", "date", "amount", "note", "created_at"}


def test_cash_ledger_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    # Re-running must be a no-op (no duplicate-table error).
    assert run_migrations(conn) == CURRENT_VERSION
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrations.py::test_migration_creates_cash_ledger_table -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: cash_ledger` (PRAGMA returns empty / assert fails), or `CURRENT_VERSION` still 4.

- [ ] **Step 3: Add the schema constant**

In `src/trading/store/migrations.py`, change the version constant near the top:

```python
CURRENT_VERSION = 5
```

After the `SCHEMA_V4 = """ ... """` block, add:

```python
# v5: paper-trading funds ledger. Records capital top-ups on top of the
# INITIAL_CAPITAL seed (which stays a constant, not a row). Additive — no
# existing table is touched, so DBs at v4 need no data backfill.
SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS cash_ledger (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT NOT NULL,
  amount     REAL NOT NULL,
  note       TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cash_ledger_date ON cash_ledger(date);
"""
```

- [ ] **Step 4: Wire the migration branch**

In `run_migrations`, after the `if current < 4:` block and before `return CURRENT_VERSION`, add:

```python
    if current < 5:
        conn.executescript(SCHEMA_V5)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (5, datetime.now(UTC).isoformat()),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add src/trading/store/migrations.py tests/test_migrations.py
git commit -m "feat(paper): add cash_ledger table (migration v5)"
```

---

### Task 2: Funds ledger module (`paper/funds.py`)

**Files:**
- Create: `src/trading/paper/funds.py`
- Test: `tests/test_paper_funds.py`

**Interfaces:**
- Consumes: a migrated `sqlite3.Connection` (with `cash_ledger`).
- Produces:
  - `@dataclass(frozen=True) FundsDeposit(id: int, date: str, amount: float, note: str | None, created_at: str)`
  - `add_funds(conn, *, amount: float, date: str, note: str | None = None) -> FundsDeposit` — raises `ValueError` if `amount <= 0`.
  - `list_funds(conn) -> list[FundsDeposit]` — ordered by `date`, then `id`.
  - `total_funds_added(conn, *, as_of: date) -> float` — `SUM(amount) WHERE date <= as_of`; `0.0` if none.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paper_funds.py`:

```python
"""Tests for trading.paper.funds — the capital top-ups ledger."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from trading.paper.funds import FundsDeposit, add_funds, list_funds, total_funds_added
from trading.store.migrations import run_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


def test_add_funds_returns_deposit_and_persists(conn: sqlite3.Connection) -> None:
    dep = add_funds(conn, amount=50_000.0, date="2026-06-19", note="June top-up")
    assert isinstance(dep, FundsDeposit)
    assert dep.id == 1
    assert dep.amount == 50_000.0
    assert dep.date == "2026-06-19"
    assert dep.note == "June top-up"
    assert dep.created_at  # non-empty ISO timestamp
    rows = conn.execute("SELECT amount FROM cash_ledger").fetchall()
    assert [r["amount"] for r in rows] == [50_000.0]


def test_add_funds_rejects_non_positive(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        add_funds(conn, amount=0.0, date="2026-06-19")
    with pytest.raises(ValueError):
        add_funds(conn, amount=-100.0, date="2026-06-19")


def test_list_funds_orders_by_date_then_id(conn: sqlite3.Connection) -> None:
    add_funds(conn, amount=10.0, date="2026-06-19")
    add_funds(conn, amount=20.0, date="2026-06-17")
    add_funds(conn, amount=30.0, date="2026-06-19")
    got = [(d.date, d.amount) for d in list_funds(conn)]
    assert got == [("2026-06-17", 20.0), ("2026-06-19", 10.0), ("2026-06-19", 30.0)]


def test_total_funds_added_filters_by_as_of(conn: sqlite3.Connection) -> None:
    add_funds(conn, amount=10_000.0, date="2026-06-10")
    add_funds(conn, amount=25_000.0, date="2026-06-20")
    assert total_funds_added(conn, as_of=date(2026, 6, 15)) == 10_000.0
    assert total_funds_added(conn, as_of=date(2026, 6, 30)) == 35_000.0


def test_total_funds_added_zero_when_empty(conn: sqlite3.Connection) -> None:
    assert total_funds_added(conn, as_of=date(2026, 6, 19)) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paper_funds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.paper.funds'`.

- [ ] **Step 3: Implement the module**

Create `src/trading/paper/funds.py`:

```python
"""Paper-trading funds ledger — capital top-ups on top of INITIAL_CAPITAL.

The initial paper capital stays a constant seed (see
`trading.paper.reconcile.INITIAL_CAPITAL`); this ledger records *additional*
deposits the user makes over time. `compute_paper_cash` adds the running sum
to its seed, so a top-up raises available cash without disturbing the existing
trade-derived cash math (an empty ledger sums to 0.0).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class FundsDeposit:
    """One row in `cash_ledger` — a single capital top-up."""

    id: int
    date: str
    amount: float
    note: str | None
    created_at: str


def add_funds(
    conn: sqlite3.Connection,
    *,
    amount: float,
    date: str,
    note: str | None = None,
) -> FundsDeposit:
    """Record a capital top-up. Rejects `amount <= 0` (deposits only).

    `date` is the caller's responsibility (CLI passes today or `--date`);
    `created_at` is stamped here with the wall clock.
    """
    if amount <= 0:
        raise ValueError(f"funds amount must be positive, got {amount!r}")
    created_at = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO cash_ledger (date, amount, note, created_at) VALUES (?, ?, ?, ?)",
        (date, amount, note, created_at),
    )
    conn.commit()
    return FundsDeposit(
        id=int(cur.lastrowid),
        date=date,
        amount=amount,
        note=note,
        created_at=created_at,
    )


def list_funds(conn: sqlite3.Connection) -> list[FundsDeposit]:
    """All deposits ordered by date, then insertion id."""
    rows = conn.execute(
        "SELECT id, date, amount, note, created_at FROM cash_ledger ORDER BY date, id"
    ).fetchall()
    return [
        FundsDeposit(
            id=int(r["id"]),
            date=str(r["date"]),
            amount=float(r["amount"]),
            note=r["note"],
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


def total_funds_added(conn: sqlite3.Connection, *, as_of: date) -> float:
    """Sum of top-ups with `date <= as_of` (0.0 if none).

    Date-filtered so re-running an older `as_of` reproduces the balance as it
    stood that day — mirrors `compute_paper_cash`.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0.0) AS total FROM cash_ledger WHERE date <= ?",
        (as_of.isoformat(),),
    ).fetchone()
    return float(row["total"]) if row is not None else 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paper_funds.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/trading/paper/funds.py tests/test_paper_funds.py
git commit -m "feat(paper): funds ledger — add/list/total deposits"
```

---

### Task 3: Wire funds into `compute_paper_cash`

**Files:**
- Modify: `src/trading/paper/reconcile.py` (`compute_paper_cash` seed)
- Test: `tests/test_paper_reconcile.py` (append)

**Interfaces:**
- Consumes: `trading.paper.funds.total_funds_added`.
- Produces: `compute_paper_cash` seed becomes `initial_capital + total_funds_added(conn, as_of=as_of)`. Signature unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_paper_reconcile.py` (imports `add_funds` and reuses the file's existing `conn` fixture):

```python
def test_paper_cash_rises_by_top_up(conn: sqlite3.Connection) -> None:
    from trading.paper.funds import add_funds
    from trading.paper.reconcile import compute_paper_cash

    base = compute_paper_cash(conn, as_of=date(2026, 6, 19))
    add_funds(conn, amount=40_000.0, date="2026-06-19")
    after = compute_paper_cash(conn, as_of=date(2026, 6, 19))
    assert after == pytest.approx(base + 40_000.0)


def test_paper_cash_excludes_future_top_ups(conn: sqlite3.Connection) -> None:
    from trading.paper.funds import add_funds
    from trading.paper.reconcile import compute_paper_cash

    add_funds(conn, amount=40_000.0, date="2026-06-25")
    # A top-up dated after as_of must not count yet.
    assert compute_paper_cash(conn, as_of=date(2026, 6, 19)) == pytest.approx(100_000.0)


def test_paper_cash_unchanged_with_empty_ledger(conn: sqlite3.Connection) -> None:
    from trading.paper.reconcile import compute_paper_cash

    # Regression guard: no ledger rows ⇒ byte-identical to the seed.
    assert compute_paper_cash(conn, as_of=date(2026, 6, 19)) == pytest.approx(100_000.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paper_reconcile.py::test_paper_cash_rises_by_top_up -v`
Expected: FAIL — `after` equals `base` (top-up not yet counted), `assert` fails.

- [ ] **Step 3: Implement the wiring**

In `src/trading/paper/reconcile.py`, add the import near the existing `from trading.paper.ledger import ...`:

```python
from trading.paper.funds import total_funds_added
```

In `compute_paper_cash`, change the seed line:

```python
    as_of_iso = as_of.isoformat()
    cash = initial_capital + total_funds_added(conn, as_of=as_of)
```

(Everything else in the function is unchanged.) Update the docstring's first paragraph to note the seed:

```python
    """Paper-cash balance derived from the trade ledger as of `as_of`.

    The seed is `initial_capital` plus any capital top-ups recorded in
    `cash_ledger` with `date <= as_of` (see `trading.paper.funds`); an empty
    ledger contributes 0.0, so behaviour is unchanged from before funds
    tracking existed.
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paper_reconcile.py -v`
Expected: PASS (new tests + all pre-existing reconcile tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/trading/paper/reconcile.py tests/test_paper_reconcile.py
git commit -m "feat(paper): seed paper cash with funds-ledger top-ups"
```

---

### Task 4: Journal schedule helpers (`paper/journal.py`)

**Files:**
- Create: `src/trading/paper/journal.py`
- Test: `tests/test_paper_journal.py`

**Interfaces:**
- Produces:
  - `expected_target_date(entry_iso: str, horizon_days: int) -> date` — bought date + `horizon_days` trading days (`np.busday_offset`, `roll="forward"`).
  - `deviation_label(target: date, *, exit_iso: str | None, as_of: date) -> str` — signed trading-day deviation. Closed: `"-Nd early"` / `"+Nd late"` / `"on time"`. Open: `"Nd left"` / `"+Nd overdue"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paper_journal.py`:

```python
"""Tests for trading.paper.journal — bought/target/deviation date helpers."""

from __future__ import annotations

from datetime import date

from trading.paper.journal import deviation_label, expected_target_date


def test_expected_target_date_adds_trading_days() -> None:
    # 2026-06-01 is a Monday; +5 trading days → Monday 2026-06-08.
    assert expected_target_date("2026-06-01T09:20:00", 5) == date(2026, 6, 8)


def test_expected_target_date_parses_date_only_string() -> None:
    assert expected_target_date("2026-06-01", 5) == date(2026, 6, 8)


def test_deviation_label_closed_early() -> None:
    target = date(2026, 6, 8)
    # Exit Friday 2026-06-05, two trading days before target.
    assert deviation_label(target, exit_iso="2026-06-05T15:30:00", as_of=date(2026, 6, 19)) == "-2d early"


def test_deviation_label_closed_late() -> None:
    target = date(2026, 6, 8)
    # Exit Wednesday 2026-06-10, two trading days after target.
    assert deviation_label(target, exit_iso="2026-06-10T15:30:00", as_of=date(2026, 6, 19)) == "+2d late"


def test_deviation_label_closed_on_time() -> None:
    target = date(2026, 6, 8)
    assert deviation_label(target, exit_iso="2026-06-08T15:30:00", as_of=date(2026, 6, 19)) == "on time"


def test_deviation_label_open_remaining() -> None:
    target = date(2026, 6, 19)
    # as_of Monday 2026-06-15, four trading days before target.
    assert deviation_label(target, exit_iso=None, as_of=date(2026, 6, 15)) == "4d left"


def test_deviation_label_open_overdue() -> None:
    target = date(2026, 6, 8)
    # as_of Wednesday 2026-06-10, two trading days past target.
    assert deviation_label(target, exit_iso=None, as_of=date(2026, 6, 10)) == "+2d overdue"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paper_journal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.paper.journal'`.

- [ ] **Step 3: Implement the module**

Create `src/trading/paper/journal.py`:

```python
"""Paper Journal schedule helpers — bought / expected-target / deviation.

Pure, trading-day-based date math (`numpy.busday_offset` / `busday_count`),
consistent with the engine's time-stop and `mtm._days_held`. No DB or network.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np


def _as_date(iso: str) -> date:
    """Parse the date part of an ISO date or full timestamp."""
    return datetime.fromisoformat(iso).date()


def expected_target_date(entry_iso: str, horizon_days: int) -> date:
    """Bought date + `horizon_days` trading days (rolled forward off weekends)."""
    entry = _as_date(entry_iso)
    shifted = np.busday_offset(
        np.datetime64(entry, "D"), horizon_days, roll="forward"
    )
    return shifted.astype("datetime64[D]").astype(date)


def deviation_label(target: date, *, exit_iso: str | None, as_of: date) -> str:
    """Signed trading-day deviation from the expected target date.

    Closed (`exit_iso` set): "-Nd early" / "+Nd late" / "on time".
    Open  (`exit_iso` None): "Nd left" (on/before target) / "+Nd overdue".
    """
    t = np.datetime64(target, "D")
    if exit_iso is not None:
        exit_d = np.datetime64(_as_date(exit_iso), "D")
        if exit_d < t:
            return f"-{int(np.busday_count(exit_d, t))}d early"
        if exit_d > t:
            return f"+{int(np.busday_count(t, exit_d))}d late"
        return "on time"
    a = np.datetime64(as_of, "D")
    if a > t:
        return f"+{int(np.busday_count(t, a))}d overdue"
    return f"{int(np.busday_count(a, t))}d left"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paper_journal.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/trading/paper/journal.py tests/test_paper_journal.py
git commit -m "feat(paper): journal schedule helpers (target date + deviation)"
```

---

### Task 5: Positions view (`paper/positions.py`)

**Files:**
- Create: `src/trading/paper/positions.py`
- Test: `tests/test_paper_positions.py`

**Interfaces:**
- Consumes: `compute_paper_cash`, `INITIAL_CAPITAL` from `reconcile`; `total_funds_added` from `funds`; `paper_trades` / `signals` / `portfolio_snapshots` tables.
- Produces:
  - `@dataclass(frozen=True) Position(symbol, qty: int, avg, invested, ltp, current_value, pnl, pnl_pct, today_pnl: float)`
  - `@dataclass(frozen=True) PortfolioSummary(invested, current_value, total_pnl, total_pnl_pct, today_pnl, cash, funds_added, account_value: float, as_of_mark: str | None)`
  - `compute_positions(conn, *, as_of: date) -> list[Position]` — sorted by `current_value` desc.
  - `compute_summary(conn, *, as_of: date, initial_capital: float = INITIAL_CAPITAL) -> PortfolioSummary`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paper_positions.py`:

```python
"""Tests for trading.paper.positions — per-symbol holdings + summary."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from trading.paper.ledger import log_signal_and_open_trade
from trading.paper.positions import compute_positions, compute_summary
from trading.store.migrations import run_migrations
from trading.store.repo import Signal


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _open(conn: sqlite3.Connection, symbol: str, entry: float, qty: int, ts: str) -> None:
    sig = Signal(
        id=None, ts=ts, symbol=symbol, side="LONG",
        entry=entry, stop=entry * 0.9, target=entry * 1.2,
        horizon_days=15, created_by="auto",
    )
    log_signal_and_open_trade(
        conn, signal=sig, entry_ts=ts, entry_price=entry, qty=qty, atr_at_entry=2.0
    )


def _snapshot(conn: sqlite3.Connection, d: str, holdings: dict[str, dict[str, float]]) -> None:
    conn.execute(
        "INSERT INTO portfolio_snapshots(date, cash, holdings_json, equity) VALUES (?, ?, ?, ?)",
        (d, 100000.0, json.dumps(holdings), 100000.0),
    )
    conn.commit()


def test_multi_lot_avg_and_invested(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _open(conn, "ACME", entry=120.0, qty=10, ts="2026-06-02T09:20:00")
    pos = {p.symbol: p for p in compute_positions(conn, as_of=date(2026, 6, 3))}
    acme = pos["ACME"]
    assert acme.qty == 20
    assert acme.invested == pytest.approx(2200.0)  # 100*10 + 120*10
    assert acme.avg == pytest.approx(110.0)


def test_ltp_from_latest_snapshot(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    # Latest snapshot marks ACME at 130 (value=1300 over qty 10).
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1300.0}})
    acme = compute_positions(conn, as_of=date(2026, 6, 3))[0]
    assert acme.ltp == pytest.approx(130.0)
    assert acme.current_value == pytest.approx(1300.0)
    assert acme.pnl == pytest.approx(300.0)
    assert acme.pnl_pct == pytest.approx(30.0)


def test_ltp_falls_back_to_avg_when_symbol_absent(conn: sqlite3.Connection) -> None:
    _open(conn, "NEW", entry=50.0, qty=4, ts="2026-06-03T09:20:00")
    # Snapshot predates the position and lacks NEW → LTP falls back to avg.
    _snapshot(conn, "2026-06-02", {"OTHER": {"qty": 1, "value": 10.0}})
    new = {p.symbol: p for p in compute_positions(conn, as_of=date(2026, 6, 3))}["NEW"]
    assert new.ltp == pytest.approx(50.0)
    assert new.pnl == pytest.approx(0.0)
    assert new.today_pnl == pytest.approx(0.0)


def test_today_pnl_from_prior_snapshot(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1200.0}})  # prev close 120
    _snapshot(conn, "2026-06-03", {"ACME": {"qty": 10, "value": 1300.0}})  # ltp 130
    acme = compute_positions(conn, as_of=date(2026, 6, 4))[0]
    assert acme.today_pnl == pytest.approx(100.0)  # 10 * (130 - 120)


def test_today_pnl_zero_with_single_snapshot(conn: sqlite3.Connection) -> None:
    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1300.0}})
    acme = compute_positions(conn, as_of=date(2026, 6, 3))[0]
    assert acme.today_pnl == pytest.approx(0.0)  # no prior snapshot → prev_close = ltp


def test_summary_totals_include_cash_and_funds(conn: sqlite3.Connection) -> None:
    from trading.paper.funds import add_funds

    _open(conn, "ACME", entry=100.0, qty=10, ts="2026-06-01T09:20:00")
    _snapshot(conn, "2026-06-02", {"ACME": {"qty": 10, "value": 1300.0}})
    add_funds(conn, amount=20_000.0, date="2026-06-01")
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert s.invested == pytest.approx(1000.0)
    assert s.current_value == pytest.approx(1300.0)
    assert s.total_pnl == pytest.approx(300.0)
    assert s.funds_added == pytest.approx(20_000.0)
    assert s.as_of_mark == "2026-06-02"
    # account_value = cash + current_value; cash already includes the top-up.
    assert s.account_value == pytest.approx(s.cash + s.current_value)


def test_summary_empty_when_no_trades(conn: sqlite3.Connection) -> None:
    s = compute_summary(conn, as_of=date(2026, 6, 3))
    assert s.invested == 0.0
    assert s.current_value == 0.0
    assert s.as_of_mark is None
    assert compute_positions(conn, as_of=date(2026, 6, 3)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paper_positions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.paper.positions'`.

- [ ] **Step 3: Implement the module**

Create `src/trading/paper/positions.py`:

```python
"""Paper portfolio positions — per-symbol holdings + a portfolio summary.

Pure aggregation over `paper_trades` (open lots) and `portfolio_snapshots`
(offline marks). No live/network price source — LTP is the latest snapshot's
close, with deliberate fallbacks so the view never NULLs or crashes:

  * LTP missing for a symbol  → falls back to weighted avg entry (P&L = 0).
  * prev_close missing/<2 snaps → falls back to LTP (today's P&L = 0).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from trading.paper.funds import total_funds_added
from trading.paper.reconcile import INITIAL_CAPITAL, compute_paper_cash


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg: float
    invested: float
    ltp: float
    current_value: float
    pnl: float
    pnl_pct: float
    today_pnl: float


@dataclass(frozen=True)
class PortfolioSummary:
    invested: float
    current_value: float
    total_pnl: float
    total_pnl_pct: float
    today_pnl: float
    cash: float
    funds_added: float
    account_value: float
    as_of_mark: str | None


def _marks(conn: sqlite3.Connection) -> tuple[dict[str, float], dict[str, float], str | None]:
    """Return (latest per-share marks, prev per-share marks, latest date).

    Each map is `{symbol: value/qty}` parsed from a snapshot's holdings_json.
    `latest_date` is None when no snapshots exist.
    """
    rows = conn.execute(
        "SELECT date, holdings_json FROM portfolio_snapshots ORDER BY date DESC LIMIT 2"
    ).fetchall()

    def per_share(blob: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym, h in json.loads(blob).items():
            qty = float(h.get("qty") or 0.0)
            if qty:
                out[sym] = float(h.get("value") or 0.0) / qty
        return out

    if not rows:
        return {}, {}, None
    latest = per_share(rows[0]["holdings_json"])
    prev = per_share(rows[1]["holdings_json"]) if len(rows) > 1 else {}
    return latest, prev, str(rows[0]["date"])


def compute_positions(conn: sqlite3.Connection, *, as_of: date) -> list[Position]:
    """Per-symbol open holdings as of `as_of`, sorted by current value desc."""
    rows = conn.execute(
        """SELECT s.symbol AS symbol, pt.entry_price AS entry_price, pt.qty AS qty
             FROM paper_trades pt
             JOIN signals s ON s.id = pt.signal_id
            WHERE pt.ts_exit IS NULL AND date(pt.ts_entry) <= ?""",
        (as_of.isoformat(),),
    ).fetchall()

    agg: dict[str, dict[str, float]] = {}
    for r in rows:
        a = agg.setdefault(str(r["symbol"]), {"qty": 0.0, "invested": 0.0})
        a["qty"] += float(r["qty"])
        a["invested"] += float(r["entry_price"]) * float(r["qty"])

    latest, prev, _ = _marks(conn)

    positions: list[Position] = []
    for symbol, a in agg.items():
        qty = int(a["qty"])
        invested = a["invested"]
        avg = invested / qty if qty else 0.0
        ltp = latest.get(symbol, avg)
        prev_close = prev.get(symbol, ltp)
        current_value = ltp * qty
        pnl = current_value - invested
        positions.append(
            Position(
                symbol=symbol,
                qty=qty,
                avg=avg,
                invested=invested,
                ltp=ltp,
                current_value=current_value,
                pnl=pnl,
                pnl_pct=(pnl / invested * 100.0) if invested else 0.0,
                today_pnl=qty * (ltp - prev_close),
            )
        )
    positions.sort(key=lambda p: p.current_value, reverse=True)
    return positions


def compute_summary(
    conn: sqlite3.Connection,
    *,
    as_of: date,
    initial_capital: float = INITIAL_CAPITAL,
) -> PortfolioSummary:
    """Aggregate the positions and fold in cash + funds for the summary tiles."""
    positions = compute_positions(conn, as_of=as_of)
    invested = sum(p.invested for p in positions)
    current_value = sum(p.current_value for p in positions)
    total_pnl = current_value - invested
    today_pnl = sum(p.today_pnl for p in positions)
    cash = compute_paper_cash(conn, as_of=as_of, initial_capital=initial_capital)
    funds_added = total_funds_added(conn, as_of=as_of)
    _, _, as_of_mark = _marks(conn)
    return PortfolioSummary(
        invested=invested,
        current_value=current_value,
        total_pnl=total_pnl,
        total_pnl_pct=(total_pnl / invested * 100.0) if invested else 0.0,
        today_pnl=today_pnl,
        cash=cash,
        funds_added=funds_added,
        account_value=cash + current_value,
        as_of_mark=as_of_mark,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paper_positions.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/trading/paper/positions.py tests/test_paper_positions.py
git commit -m "feat(paper): positions view — per-symbol holdings + summary"
```

---

### Task 6: CLI `funds` sub-app

**Files:**
- Modify: `src/trading/cli.py` (register `funds_app`; add `add` / `list` / `balance` commands)
- Test: `tests/test_cli_funds.py`

**Interfaces:**
- Consumes: `trading.paper.funds` (`add_funds`, `list_funds`, `total_funds_added`), `trading.paper.positions.compute_summary`, `INITIAL_CAPITAL`, `get_paths`, `get_conn`, `run_migrations`.
- Produces CLI commands: `trading funds add <amount> [--note] [--date]`, `trading funds list`, `trading funds balance [--date]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_funds.py`:

```python
"""Tests for the `trading funds` CLI sub-app."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trading.cli import app


def test_funds_add_writes_row_and_prints_balance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    result = CliRunner().invoke(
        app, ["funds", "add", "50000", "--note", "June", "--date", "2026-06-19"]
    )
    assert result.exit_code == 0, result.output
    assert "50,000" in result.output or "50000" in result.output


def test_funds_add_rejects_non_positive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    result = CliRunner().invoke(app, ["funds", "add", "0"])
    assert result.exit_code != 0


def test_funds_list_shows_initial_and_topups(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    CliRunner().invoke(app, ["funds", "add", "25000", "--date", "2026-06-19"])
    result = CliRunner().invoke(app, ["funds", "list"])
    assert result.exit_code == 0, result.output
    assert "Initial capital" in result.output
    assert "25,000" in result.output or "25000" in result.output


def test_funds_balance_reflects_topup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    CliRunner().invoke(app, ["funds", "add", "30000", "--date", "2026-06-19"])
    result = CliRunner().invoke(app, ["funds", "balance", "--date", "2026-06-19"])
    assert result.exit_code == 0, result.output
    # Total funds in = 100,000 initial + 30,000 top-up = 130,000.
    assert "130,000" in result.output or "130000" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_funds.py -v`
Expected: FAIL — Typer exits non-zero with "No such command 'funds'".

- [ ] **Step 3: Register the sub-app and import helpers**

In `src/trading/cli.py`, after the existing `macro_app` registration (lines ~100-101), add:

```python
funds_app = typer.Typer(help="Paper-trading funds ledger — deposits + balance.")
app.add_typer(funds_app, name="funds")
```

Extend the existing funds import line. Change:

```python
from trading.paper.reconcile import INITIAL_CAPITAL, reconcile_day
```

to add the new imports just below it:

```python
from trading.paper.funds import add_funds, list_funds, total_funds_added
from trading.paper.positions import compute_summary
```

- [ ] **Step 4: Add the three commands**

Append near the other command definitions in `src/trading/cli.py` (after the `macro` commands block is fine):

```python
@funds_app.command("add")
def funds_add_cmd(
    amount: Annotated[float, typer.Argument(help="Top-up amount in rupees (> 0).")],
    note: Annotated[str | None, typer.Option("--note", help="Optional label.")] = None,
    as_of: Annotated[
        str | None,
        typer.Option("--date", help="Deposit date (YYYY-MM-DD). Defaults to today."),
    ] = None,
) -> None:
    """Record a capital top-up and print the new balance breakdown."""
    paths = get_paths()
    deposit_date = as_of or date.today().isoformat()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        try:
            dep = add_funds(conn, amount=amount, date=deposit_date, note=note)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        summary = compute_summary(conn, as_of=date.fromisoformat(deposit_date))
    console.print(
        f"[green]Added ₹{dep.amount:,.0f}[/green] on {dep.date}"
        + (f" ({dep.note})" if dep.note else "")
    )
    console.print(
        f"Total funds in ₹{INITIAL_CAPITAL + summary.funds_added:,.0f}  ·  "
        f"Cash available ₹{summary.cash:,.0f}"
    )


@funds_app.command("list")
def funds_list_cmd() -> None:
    """List the initial capital and every recorded top-up."""
    paths = get_paths()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        deposits = list_funds(conn)
        total_added = total_funds_added(conn, as_of=date.today())
    table = Table(show_header=True, header_style="bold")
    table.add_column("Date")
    table.add_column("Amount", justify="right")
    table.add_column("Note")
    table.add_row("—", f"₹{INITIAL_CAPITAL:,.0f}", "Initial capital")
    for d in deposits:
        table.add_row(d.date, f"₹{d.amount:,.0f}", d.note or "")
    console.print(table)
    console.print(f"[bold]Total funds in ₹{INITIAL_CAPITAL + total_added:,.0f}[/bold]")


@funds_app.command("balance")
def funds_balance_cmd(
    as_of: Annotated[
        str | None,
        typer.Option("--date", help="Balance date (YYYY-MM-DD). Defaults to today."),
    ] = None,
) -> None:
    """Print total funds in, cash available, invested, holdings value, account value."""
    paths = get_paths()
    target = date.fromisoformat(as_of) if as_of else date.today()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        summary = compute_summary(conn, as_of=target)
    table = Table(show_header=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Total funds in", f"₹{INITIAL_CAPITAL + summary.funds_added:,.0f}")
    table.add_row("Cash available", f"₹{summary.cash:,.0f}")
    table.add_row("Invested (at cost)", f"₹{summary.invested:,.0f}")
    table.add_row("Holdings value", f"₹{summary.current_value:,.0f}")
    table.add_row("Account value", f"₹{summary.account_value:,.0f}")
    console.print(table)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_funds.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/trading/cli.py tests/test_cli_funds.py
git commit -m "feat(cli): funds sub-app — add/list/balance"
```

---

### Task 7: UI data loaders (`ui/data.py`)

**Files:**
- Modify: `src/trading/ui/data.py` (add `s.horizon_days` to `load_paper_trades`; add `load_cash_ledger`, `load_paper_positions`, `load_paper_summary`)
- Test: `tests/test_ui_data_paper.py`

**Interfaces:**
- Consumes: `compute_positions`, `compute_summary`, `list_funds`, existing `paths()` / `get_conn`.
- Produces:
  - `load_paper_trades` rows gain a `horizon_days` column.
  - `load_cash_ledger() -> pd.DataFrame` (columns `date, amount, note`).
  - `load_paper_positions(as_of: str) -> pd.DataFrame`.
  - `load_paper_summary(as_of: str) -> PortfolioSummary`.

**Note on testing Streamlit-cached functions:** call `.__wrapped__` to bypass `@st.cache_data` where present (e.g. `data.load_cash_ledger.__wrapped__()`); `load_paper_summary` is intentionally uncached (returns a dataclass) so it is called directly. Set `TRADING_PROJECT_ROOT` so `paths()` resolves to the temp DB.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ui_data_paper.py`:

```python
"""Tests for the paper-portfolio loaders in trading.ui.data."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from trading.config import get_paths
from trading.paper.funds import add_funds
from trading.paper.ledger import log_signal_and_open_trade
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.repo import Signal
from trading.ui import data


def _seed(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        sig = Signal(
            id=None, ts="2026-06-01T09:20:00", symbol="ACME", side="LONG",
            entry=100.0, stop=90.0, target=120.0, horizon_days=15, created_by="auto",
        )
        log_signal_and_open_trade(
            conn, signal=sig, entry_ts="2026-06-01T09:20:00",
            entry_price=100.0, qty=10, atr_at_entry=2.0,
        )
        add_funds(conn, amount=20_000.0, date="2026-06-01")


def test_load_paper_trades_includes_horizon_days(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    df = data.load_paper_trades.__wrapped__()
    assert "horizon_days" in df.columns
    assert int(df.iloc[0]["horizon_days"]) == 15


def test_load_cash_ledger_returns_topups(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    df = data.load_cash_ledger.__wrapped__()
    assert list(df["amount"]) == [20_000.0]


def test_load_paper_positions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    df = data.load_paper_positions.__wrapped__("2026-06-03")
    assert list(df["symbol"]) == ["ACME"]
    assert int(df.iloc[0]["qty"]) == 10


def test_load_paper_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed(tmp_path)
    summary = data.load_paper_summary("2026-06-03")
    assert summary.funds_added == 20_000.0
    assert summary.invested == 1000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ui_data_paper.py -v`
Expected: FAIL — `test_load_cash_ledger`/`positions`/`summary` raise `AttributeError` (functions absent); `horizon_days` test fails (column missing).

- [ ] **Step 3: Add `horizon_days` to `load_paper_trades`**

In `src/trading/ui/data.py`, in `load_paper_trades`, extend the SELECT's signal columns:

```python
            SELECT pt.*,
                   s.symbol, s.side, s.target, s.stop AS signal_stop,
                   s.conviction, s.horizon_days
              FROM paper_trades pt
              LEFT JOIN signals s ON s.id = pt.signal_id
              {where}
             ORDER BY pt.ts_entry
```

- [ ] **Step 4: Add the new loaders**

Add the import near the top of `src/trading/ui/data.py` (with the other `trading.*` imports):

```python
from trading.paper.funds import list_funds
from trading.paper.positions import PortfolioSummary, compute_positions, compute_summary
```

Add these functions in the "Signals / paper trades / predictions" section:

```python
@st.cache_data(ttl=60)
def load_cash_ledger() -> pd.DataFrame:
    """Funds top-ups (date / amount / note), oldest first."""
    with get_conn(paths().db_path) as conn:
        deposits = list_funds(conn)
    if not deposits:
        return pd.DataFrame(columns=["date", "amount", "note"])
    return pd.DataFrame(
        [{"date": d.date, "amount": d.amount, "note": d.note or ""} for d in deposits]
    )


@st.cache_data(ttl=60)
def load_paper_positions(as_of: str) -> pd.DataFrame:
    """Per-symbol paper holdings as of `as_of` (one row per symbol)."""
    with get_conn(paths().db_path) as conn:
        positions = compute_positions(conn, as_of=_date_arg(as_of))
    if not positions:
        return pd.DataFrame(
            columns=[
                "symbol", "qty", "avg", "invested", "ltp",
                "current_value", "pnl", "pnl_pct", "today_pnl",
            ]
        )
    return pd.DataFrame([asdict(p) for p in positions])


def load_paper_summary(as_of: str) -> PortfolioSummary:
    """Portfolio summary (invested/current/P&L/cash/funds) as of `as_of`.

    Uncached — returns a dataclass and is cheap; pages call it once per render.
    """
    with get_conn(paths().db_path) as conn:
        return compute_summary(conn, as_of=_date_arg(as_of))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ui_data_paper.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/trading/ui/data.py tests/test_ui_data_paper.py
git commit -m "feat(ui): paper-portfolio data loaders + horizon_days in trades"
```

---

### Task 8: Rename Portfolio page → Kite Portfolio

**Files:**
- Rename: `src/trading/ui/pages/1_Portfolio.py` → `src/trading/ui/pages/1_Kite_Portfolio.py`
- Modify: the renamed file's page title / sidebar / header copy
- Test: `tests/test_ui_pages_smoke.py` (append a presence assertion if such a smoke test exists; otherwise the verification is the import check below)

**Interfaces:** No code interface change — copy + filename only. Streamlit derives the sidebar nav label from the filename, so the rename relabels the nav.

- [ ] **Step 1: Rename the file (preserve history)**

```bash
git mv src/trading/ui/pages/1_Portfolio.py src/trading/ui/pages/1_Kite_Portfolio.py
```

- [ ] **Step 2: Update the page copy**

In `src/trading/ui/pages/1_Kite_Portfolio.py`:

Change the page config line:

```python
st.set_page_config(page_title="Kite Portfolio · Trading", page_icon="📊", layout="wide")
```

Change the sidebar title:

```python
st.sidebar.title("📊 Kite Portfolio")
```

Change the header + caption block:

```python
    st.markdown(f"## Kite Portfolio · {as_of}")
    st.caption("Your real Zerodha account — live holdings, GTT viability, sector concentration.")
```

- [ ] **Step 3: Verify the page imports cleanly**

Run: `uv run python -c "import ast; ast.parse(open('src/trading/ui/pages/1_Kite_Portfolio.py', encoding='utf-8').read())"`
Expected: no output (syntax valid). Confirm the old filename is gone:
Run: `uv run python -c "import os; assert not os.path.exists('src/trading/ui/pages/1_Portfolio.py')"`
Expected: no output.

- [ ] **Step 4: Run the full UI-related suite + lint**

Run: `uv run pytest tests/ -k "ui" -q`
Expected: PASS (no test referenced the old module path; if any did, update the path).

- [ ] **Step 5: Commit**

```bash
git add -A src/trading/ui/pages/
git commit -m "refactor(ui): rename Portfolio page to Kite Portfolio"
```

---

### Task 9: New Paper Portfolio page

**Files:**
- Create: `src/trading/ui/pages/4_Paper_Portfolio.py`

**Interfaces:**
- Consumes: `data.load_paper_summary`, `data.load_paper_positions`, `data.load_cash_ledger`; `funds.add_funds` (the one intentional UI write); components `kpi_tile`, `format_currency`, `format_pct`, `section_header`, `empty_state`, `divider`.

**Note:** Streamlit pages run top-to-bottom on import and call `st.*`, so they are validated by syntax-parse + a manual render rather than a unit test (consistent with the existing pages, which have no per-page unit tests). The data layer feeding this page is already covered by Task 7.

- [ ] **Step 1: Create the page**

Create `src/trading/ui/pages/4_Paper_Portfolio.py`:

```python
"""Paper Portfolio — Kite-style dashboard for the paper-trading book.

Per-symbol holdings (qty/avg/invested/LTP/current/P&L/today's P&L), an
invested/current/P&L summary, and a separately-tracked funds panel (initial
₹1L + top-ups). Marks are offline — the latest portfolio snapshot's close —
so the page is labelled "as of last close". The Add-funds widget is the one
intentional UI writer; everything else is read-only.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from trading.config import get_paths
from trading.paper.funds import add_funds
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.ui import data
from trading.ui.components import (
    divider,
    empty_state,
    format_currency,
    format_pct,
    kpi_tile,
    section_header,
)

st.set_page_config(page_title="Paper Portfolio · Trading", page_icon="🧪", layout="wide")
st.sidebar.title("🧪 Paper Portfolio")

st.markdown("## Paper Portfolio")
as_of = date.today().isoformat()
summary = data.load_paper_summary(as_of)

if summary.as_of_mark:
    st.caption(f"Paper book · marks as of last close ({summary.as_of_mark}).")
else:
    st.caption("Paper book · no snapshots yet — values shown at cost.")
divider()

# ---------------------------------------------------------------------------
# Summary tiles
# ---------------------------------------------------------------------------

k1, k2, k3, k4, k5, k6 = st.columns(6)
with k1:
    kpi_tile("Invested", format_currency(summary.invested))
with k2:
    kpi_tile("Current value", format_currency(summary.current_value))
with k3:
    kpi_tile(
        "Total P&L",
        format_currency(summary.total_pnl),
        delta=format_pct(summary.total_pnl_pct),
        delta_color="normal",
    )
with k4:
    kpi_tile("Today's P&L", format_currency(summary.today_pnl))
with k5:
    kpi_tile("Cash available", format_currency(summary.cash))
with k6:
    kpi_tile("Account value", format_currency(summary.account_value))

divider()

# ---------------------------------------------------------------------------
# Holdings table
# ---------------------------------------------------------------------------

section_header("Holdings")
positions = data.load_paper_positions(as_of)
if positions.empty:
    empty_state(
        "No open paper positions",
        "Open a paper trade with <code>trading paper-open</code>; it appears here once filled.",
    )
else:
    show = positions.rename(
        columns={
            "symbol": "Symbol",
            "qty": "Qty",
            "avg": "Avg",
            "invested": "Invested",
            "ltp": "LTP",
            "current_value": "Current",
            "pnl": "P&L ₹",
            "pnl_pct": "P&L %",
            "today_pnl": "Today's P&L ₹",
        }
    )
    st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        column_config={
            "Avg": st.column_config.NumberColumn(format="₹%.2f"),
            "Invested": st.column_config.NumberColumn(format="₹%.0f"),
            "LTP": st.column_config.NumberColumn(format="₹%.2f"),
            "Current": st.column_config.NumberColumn(format="₹%.0f"),
            "P&L ₹": st.column_config.NumberColumn(format="₹%.0f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
            "Today's P&L ₹": st.column_config.NumberColumn(format="₹%.0f"),
        },
    )

divider()

# ---------------------------------------------------------------------------
# Funds panel + Add-funds widget
# ---------------------------------------------------------------------------

section_header("Funds")
ledger = data.load_cash_ledger()
total_in = 100_000.0 + summary.funds_added

f1, f2 = st.columns([2, 1])
with f1:
    st.write(f"**Initial capital** {format_currency(100_000.0)}")
    if ledger.empty:
        st.caption("No top-ups recorded yet.")
    else:
        st.dataframe(
            ledger.rename(columns={"date": "Date", "amount": "Amount ₹", "note": "Note"}),
            hide_index=True,
            width="stretch",
            column_config={"Amount ₹": st.column_config.NumberColumn(format="₹%.0f")},
        )
    st.write(f"**Total funds in** {format_currency(total_in)}")
    st.write(f"**Cash available** {format_currency(summary.cash)}")

with f2:
    st.markdown("**Add funds**")
    amount = st.number_input("Amount (₹)", min_value=0.0, step=1000.0, value=0.0)
    note = st.text_input("Note (optional)")
    if st.button("Add funds", type="primary"):
        if amount <= 0:
            st.error("Amount must be positive.")
        else:
            paths = get_paths()
            with get_conn(paths.db_path) as conn:
                run_migrations(conn)
                add_funds(conn, amount=amount, date=date.today().isoformat(), note=note or None)
            st.cache_data.clear()
            st.success(f"Added {format_currency(amount)}.")
            st.rerun()
```

- [ ] **Step 2: Verify the page parses**

Run: `uv run python -c "import ast; ast.parse(open('src/trading/ui/pages/4_Paper_Portfolio.py', encoding='utf-8').read())"`
Expected: no output (syntax valid).

- [ ] **Step 3: Lint the new page**

Run: `uv run ruff check src/trading/ui/pages/4_Paper_Portfolio.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add src/trading/ui/pages/4_Paper_Portfolio.py
git commit -m "feat(ui): Paper Portfolio dashboard page + add-funds widget"
```

---

### Task 10: Paper Journal deviation columns

**Files:**
- Modify: `src/trading/ui/pages/3_Paper_Journal.py` (Bought / Target date / Deviation columns on open + closed tables)

**Interfaces:**
- Consumes: `expected_target_date`, `deviation_label` from `trading.paper.journal`; `load_paper_trades` now carries `horizon_days` (Task 7).

**Note:** The page is validated by syntax-parse + lint (consistent with the other pages); the helpers it calls are unit-tested in Task 4.

- [ ] **Step 1: Add the import**

In `src/trading/ui/pages/3_Paper_Journal.py`, add to the imports:

```python
from datetime import date

from trading.paper.journal import deviation_label, expected_target_date
```

- [ ] **Step 2: Add a row-builder helper**

After the existing metric-helper functions (before the `k1, k2, ...` tiles), add:

```python
def _schedule_cols(df: pd.DataFrame, *, closed: bool) -> pd.DataFrame:
    """Add Bought / Target date / Deviation columns from ts_entry + horizon_days."""
    out = df.copy()
    today = date.today()
    boughts, targets, deviations = [], [], []
    for _, row in out.iterrows():
        entry_iso = str(row["ts_entry"])
        horizon = int(row["horizon_days"]) if pd.notna(row.get("horizon_days")) else 0
        target = expected_target_date(entry_iso, horizon)
        exit_iso = str(row["ts_exit"]) if closed and pd.notna(row.get("ts_exit")) else None
        boughts.append(entry_iso[:10])
        targets.append(target.isoformat())
        deviations.append(deviation_label(target, exit_iso=exit_iso, as_of=today))
    out["Bought"] = boughts
    out["Target date"] = targets
    out["Deviation"] = deviations
    return out
```

- [ ] **Step 3: Wire the helper into the Open trades table**

In the Open-trades block, after `show = open_trades.copy()`, insert:

```python
    show = _schedule_cols(show, closed=False)
```

and extend the `cols` list to include the new columns:

```python
    cols = [
        "entry_date",
        "symbol",
        "side",
        "qty",
        "entry_price",
        "current_stop",
        "target",
        "Bought",
        "Target date",
        "Deviation",
        "days_held",
    ]
```

- [ ] **Step 4: Wire the helper into the Closed trades table**

In the Closed-trades block, after `show = closed_trades.copy()`, insert:

```python
    show = _schedule_cols(show, closed=True)
```

and extend its `cols` list:

```python
    cols = [
        "entry",
        "exit",
        "symbol",
        "side",
        "qty",
        "entry_price",
        "exit_price",
        "pnl",
        "pnl_pct",
        "exit_reason",
        "Bought",
        "Target date",
        "Deviation",
        "days_held",
    ]
```

- [ ] **Step 5: Verify the page parses + lints**

Run: `uv run python -c "import ast; ast.parse(open('src/trading/ui/pages/3_Paper_Journal.py', encoding='utf-8').read())"`
Expected: no output.
Run: `uv run ruff check src/trading/ui/pages/3_Paper_Journal.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/trading/ui/pages/3_Paper_Journal.py
git commit -m "feat(ui): journal bought/target-date/deviation columns"
```

---

### Task 11: Full-suite gate + push

**Files:** none (verification + push only).

- [ ] **Step 1: Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests pass (the prior 950 + the new funds/positions/journal/cli/migration/ui tests).

- [ ] **Step 2: Lint + type-check**

Run: `uv run ruff check .`
Expected: `All checks passed!`
Run: `uv run mypy src/`
Expected: `Success: no issues found`.

- [ ] **Step 3: Leak check across the phase**

Run: `git log --oneline origin/main..HEAD` to confirm only the intended commits are present, then run `git diff origin/main..HEAD` piped through `grep -c` for the two F-005 sentinel phrases (the "real-money" and "suspended-indefinitely" wording from the FINDINGS note).
Expected: `0` (note: `grep -c` exits non-zero when the count is 0 — that is success here). Confirm none of the protected untracked files (`.mcp.json`, `CLAUDE.md`, `Research/`, `data/README.md`, `data/mutual_funds_holdings.md`, `docs/daily-workflow.md`) appear in `git diff --stat origin/main..HEAD`.

- [ ] **Step 4: Push**

```bash
git push origin main
```

---

## Self-Review

**1. Spec coverage:**
- Funds ledger (`cash_ledger` + `paper/funds.py`) → Tasks 1, 2 ✓
- Reconcile wiring (seed += top-ups, byte-identical when empty) → Task 3 ✓ (with regression guard)
- Positions view (`paper/positions.py`, `Position`/`PortfolioSummary`, offline LTP w/ avg fallback, today's P&L from prior snapshot, sort by current_value, empty-state) → Task 5 ✓
- Journal schedule (`paper/journal.py`, `expected_target_date`, `deviation_label` early/late/on-time/left/overdue) → Task 4 ✓
- CLI `funds add/list/balance` → Task 6 ✓
- UI rename `1_Portfolio.py`→`1_Kite_Portfolio.py` → Task 8 ✓
- New `4_Paper_Portfolio.py` (caption, summary tiles, holdings table, funds panel + Add-funds widget) → Task 9 ✓
- Journal deviation columns + `horizon_days` in `load_paper_trades` → Tasks 7, 10 ✓
- `ui/data.py` loaders (`load_cash_ledger`, `load_paper_positions`, `load_paper_summary`) → Task 7 ✓
- Error handling (amount≤0 reject, empty-states, mark fallbacks, historical as_of date filter) → covered in Tasks 2, 3, 5, 6 tests ✓
- YAGNI exclusions (no withdrawals, no live LTP, no positions table, initial ₹1L stays seed, date-only deviation) → respected ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code. ✓

**3. Type consistency:** `FundsDeposit`, `add_funds(conn, *, amount, date, note=None)`, `total_funds_added(conn, *, as_of)`, `Position`/`PortfolioSummary` field names, `compute_positions(conn, *, as_of)`, `compute_summary(conn, *, as_of, initial_capital=...)`, `expected_target_date(entry_iso, horizon_days)`, `deviation_label(target, *, exit_iso, as_of)` are used identically across Tasks 2–10. `summary.funds_added` excludes the initial ₹1L; the CLI/UI add `INITIAL_CAPITAL`/`100_000.0` for "Total funds in" consistently. ✓
