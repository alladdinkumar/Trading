# F-010 Dormant Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a writer for `fno_ban_list` (reviving the dead `passes_not_fno_banned` risk gate) and formally reserve the other 7 dormant SQLite tables, closing F-010.

**Architecture:** A best-effort NSE-CSV fetcher (`data/fno_ban.py`) feeds a delete-then-insert store (`store/fno_ban_store.py`); a new `pre_open._step_fno_ban` persists the ban list each morning and `build_scan_context` reads it into `ScanContext.fno_ban_symbols`. The other 7 tables get a `RESERVED` annotation in the migration SQL and the schema doc — no code.

**Tech Stack:** Python 3.11, `requests`/`requests-cache` (existing `CachedSession`), SQLite, pytest, ruff, mypy, UV.

## Global Constraints

- **No new dependency** — use the existing `trading.data.cache.get_cached_session`.
- **Best-effort fetch** — a feed outage must NEVER raise out of `pre_open`; degrade to an empty ban set + a warning (gate passes). Mirrors `NseEventsSource`.
- **No schema migration** — all 8 tables already exist from schema v1; this adds writers + comments only.
- **TDD** — every new function gets a failing test first; no production code without a red test.
- **Commit per task; push to `origin/main` at the end** (phase wrap-up always pushes, not just local commit).
- **Do not stage** the F-005 note in `docs/architecture/FINDINGS.md` context, nor any pre-existing untracked file (`.mcp.json`, `CLAUDE.md`, `Research/`, `data/README.md`, `data/mutual_funds_holdings.md`, `docs/daily-workflow.md`). Stage only files this plan names. Leak-check each commit: `git diff --cached | grep -c "real money\|suspended indefinitely"` must print `0`.
- Run commands with `uv run` (e.g. `uv run pytest`). Benign Windows `LF will be replaced by CRLF` warnings are expected — ignore.

---

### Task 1: Ban-list fetcher & parser (`data/fno_ban.py`)

**Files:**
- Create: `src/trading/data/fno_ban.py`
- Test: `tests/test_fno_ban.py`

**Interfaces:**
- Consumes: `trading.data.cache.get_cached_session` (existing).
- Produces:
  - `parse_fno_ban_csv(text: str) -> list[str]` — pure parser, order-preserving, deduped.
  - `fetch_fno_ban_symbols(session: CachedSession | None = None) -> list[str]` — best-effort network wrapper; any failure → `[]`.
  - `FNO_SECBAN_URL: str` constant.

- [ ] **Step 1: Write the failing parser tests**

```python
# tests/test_fno_ban.py
"""Tests for trading.data.fno_ban — NSE F&O ban-list fetch/parse (F-010)."""

from __future__ import annotations

from trading.data.fno_ban import fetch_fno_ban_symbols, parse_fno_ban_csv

# Legacy NSE fo_secban.csv: a header line then "<serial>|<SYMBOL>" rows.
_SAMPLE = """Date|02-Jan-2026
1|IDEA
2|BANDHANBNK
3|HINDCOPPER
"""


def test_parse_extracts_symbols_in_order() -> None:
    assert parse_fno_ban_csv(_SAMPLE) == ["IDEA", "BANDHANBNK", "HINDCOPPER"]


def test_parse_skips_header_and_serials() -> None:
    # No bare 'DATE', serial number, or date value leaks through as a symbol.
    out = parse_fno_ban_csv(_SAMPLE)
    assert "DATE" not in out
    assert "1" not in out
    assert all(not s[0].isdigit() for s in out)


def test_parse_handles_comma_delimiter_and_blank_lines() -> None:
    text = "Sr.No.,Symbol\n1,TATASTEEL\n\n2,M&M\n"
    assert parse_fno_ban_csv(text) == ["TATASTEEL", "M&M"]


def test_parse_dedupes_preserving_order() -> None:
    assert parse_fno_ban_csv("1|IDEA\n2|IDEA\n3|GNFC\n") == ["IDEA", "GNFC"]


def test_parse_empty_or_garbage_returns_empty() -> None:
    assert parse_fno_ban_csv("") == []
    assert parse_fno_ban_csv("\n\n   \n") == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_fno_ban.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.data.fno_ban'`.

- [ ] **Step 3: Implement the parser (minimal)**

```python
# src/trading/data/fno_ban.py
"""NSE F&O securities-ban list fetcher (F-010).

The daily ban CSV lists symbols barred from fresh F&O positions. We use it to
populate `fno_ban_list` so Layer A's `passes_not_fno_banned` gate (dead since
F-019 left the context empty) can veto a banned candidate.

Best-effort by contract: any network/parse failure yields `[]`, so a feed
outage degrades the gate to a pass with a warning rather than killing pre-open.
"""

from __future__ import annotations

import re

from requests_cache import CachedSession

from trading.data.cache import get_cached_session

FNO_SECBAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"

# A desktop UA — NSE rejects empty/unknown agents (matches the news fetchers).
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 10

# Tokens the CSV uses for its header/columns, never a real symbol.
_HEADER_TOKENS = frozenset(
    {"DATE", "SYMBOL", "SYMBOLS", "SRNO", "SR", "SR.NO.", "SERIALNUMBER"}
)
# A ticker: leading letter, then letters/digits plus the `&`/`-` seen in
# BAJAJ-AUTO / M&M. Excludes serial numbers and the date value.
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9&-]*")


def _is_symbol(token: str) -> bool:
    return bool(token) and token not in _HEADER_TOKENS and bool(
        _SYMBOL_RE.fullmatch(token)
    )


def parse_fno_ban_csv(text: str) -> list[str]:
    """Extract ban-list symbols from the NSE CSV body (pipe- or comma-delimited).

    Order-preserving and deduped. Tolerant of the header line, the leading
    serial column, and stray blank lines.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for field in re.split(r"[|,]", line):
            token = field.strip().upper()
            if _is_symbol(token) and token not in seen:
                seen.add(token)
                out.append(token)
    return out


def fetch_fno_ban_symbols(session: CachedSession | None = None) -> list[str]:
    """Fetch + parse the NSE F&O ban list. Best-effort: any failure → []."""
    try:
        sess = session if session is not None else get_cached_session()
        resp = sess.get(FNO_SECBAN_URL, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        resp.raise_for_status()
        return parse_fno_ban_csv(resp.text)
    except Exception:  # pragma: no cover — defensive; feed outage must not raise
        return []
```

- [ ] **Step 4: Run parser tests to verify they pass**

Run: `uv run pytest tests/test_fno_ban.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Write the failing fetcher (best-effort) tests**

Append to `tests/test_fno_ban.py`:

```python
class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _OkSession:
    def get(self, url: str, **_: object) -> _FakeResp:
        return _FakeResp("1|IDEA\n2|GNFC\n")


class _BoomSession:
    def get(self, url: str, **_: object) -> _FakeResp:
        raise RuntimeError("nse down")


def test_fetch_parses_session_body() -> None:
    assert fetch_fno_ban_symbols(session=_OkSession()) == ["IDEA", "GNFC"]


def test_fetch_is_best_effort_on_error() -> None:
    assert fetch_fno_ban_symbols(session=_BoomSession()) == []
```

- [ ] **Step 6: Run to verify the new tests pass**

Run: `uv run pytest tests/test_fno_ban.py -v`
Expected: PASS (7 passed). `_OkSession`/`_BoomSession` exercise the wrapper without network.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check src/trading/data/fno_ban.py tests/test_fno_ban.py
uv run ruff format src/trading/data/fno_ban.py tests/test_fno_ban.py
uv run mypy src/trading/data/fno_ban.py
git add src/trading/data/fno_ban.py tests/test_fno_ban.py
git diff --cached | grep -c "real money\|suspended indefinitely"   # expect 0
git commit -m "feat(F-010): NSE F&O ban-list fetcher (best-effort)"
```

---

### Task 2: Ban-list store (`store/fno_ban_store.py`)

**Files:**
- Create: `src/trading/store/fno_ban_store.py`
- Test: `tests/test_fno_ban_store.py`

**Interfaces:**
- Consumes: `trading.store.db.get_conn`, `trading.store.migrations.run_migrations`, the existing `fno_ban_list(date, symbol)` table.
- Produces:
  - `replace_fno_ban_list(conn: sqlite3.Connection, date_iso: str, symbols: Iterable[str]) -> None`
  - `get_fno_ban_symbols(conn: sqlite3.Connection, date_iso: str) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fno_ban_store.py
"""Tests for trading.store.fno_ban_store — fno_ban_list writer/reader (F-010)."""

from __future__ import annotations

from pathlib import Path

from trading.store.db import get_conn
from trading.store.fno_ban_store import get_fno_ban_symbols, replace_fno_ban_list
from trading.store.migrations import run_migrations


def test_replace_then_get_roundtrips(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "GNFC"])
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["GNFC", "IDEA"]  # ORDER BY symbol


def test_replace_overwrites_same_date(tmp_path: Path) -> None:
    db = tmp_path / "b2.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "GNFC"])
        replace_fno_ban_list(conn, "2026-06-19", ["TATASTEEL"])
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["TATASTEEL"]


def test_empty_list_clears_the_date(tmp_path: Path) -> None:
    db = tmp_path / "b3.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA"])
        replace_fno_ban_list(conn, "2026-06-19", [])
        assert get_fno_ban_symbols(conn, "2026-06-19") == []


def test_duplicate_input_does_not_violate_pk(tmp_path: Path) -> None:
    db = tmp_path / "b4.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "IDEA"])
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["IDEA"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_fno_ban_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.store.fno_ban_store'`.

- [ ] **Step 3: Implement the store (minimal)**

```python
# src/trading/store/fno_ban_store.py
"""Persistence helpers for `fno_ban_list` (one row per (date, symbol)).

Populated daily by `pre_open._step_fno_ban` from the NSE F&O ban CSV and read
back by `build_scan_context` into `ScanContext.fno_ban_symbols`, which Layer A's
`passes_not_fno_banned` gate vetoes against (F-010).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def replace_fno_ban_list(
    conn: sqlite3.Connection, date_iso: str, symbols: Iterable[str]
) -> None:
    """Replace the ban list for `date_iso` (delete-then-insert; idempotent).

    An empty `symbols` clears the date. Duplicate inputs are collapsed so the
    (date, symbol) primary key is never violated.
    """
    conn.execute("DELETE FROM fno_ban_list WHERE date = ?", (date_iso,))
    deduped = list(dict.fromkeys(symbols))
    conn.executemany(
        "INSERT INTO fno_ban_list (date, symbol) VALUES (?, ?)",
        [(date_iso, s) for s in deduped],
    )


def get_fno_ban_symbols(conn: sqlite3.Connection, date_iso: str) -> list[str]:
    """Return the banned symbols for `date_iso`, ordered by symbol."""
    rows = conn.execute(
        "SELECT symbol FROM fno_ban_list WHERE date = ? ORDER BY symbol",
        (date_iso,),
    ).fetchall()
    return [r["symbol"] for r in rows]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_fno_ban_store.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/trading/store/fno_ban_store.py tests/test_fno_ban_store.py
uv run ruff format src/trading/store/fno_ban_store.py tests/test_fno_ban_store.py
uv run mypy src/trading/store/fno_ban_store.py
git add src/trading/store/fno_ban_store.py tests/test_fno_ban_store.py
git diff --cached | grep -c "real money\|suspended indefinitely"   # expect 0
git commit -m "feat(F-010): fno_ban_list store (replace/get)"
```

---

### Task 3: Wire the ban set into `build_scan_context`

**Files:**
- Modify: `src/trading/jobs/pre_open.py` (imports + `build_scan_context`, ~lines 41–43 and 256–276)
- Test: `tests/test_jobs_pre_open.py` (add focused tests)

**Interfaces:**
- Consumes: `get_fno_ban_symbols` (Task 2), existing `build_scan_context(conn, as_of) -> ScanContext`, `strategy.rules.passes_not_fno_banned`.
- Produces: `build_scan_context` now sets `fno_ban_symbols=frozenset(get_fno_ban_symbols(conn, as_of.isoformat()))`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_jobs_pre_open.py` (import what you need at the top of the new block):

```python
def test_build_scan_context_populates_fno_ban(tmp_path) -> None:
    from datetime import date

    from trading.jobs.pre_open import build_scan_context
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import replace_fno_ban_list
    from trading.store.migrations import run_migrations

    db = tmp_path / "c.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA", "GNFC"])
        ctx = build_scan_context(conn, date(2026, 6, 19))
    assert ctx.fno_ban_symbols == frozenset({"IDEA", "GNFC"})


def test_banned_symbol_fails_gate_via_context(tmp_path) -> None:
    from datetime import date

    from trading.jobs.pre_open import build_scan_context
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import replace_fno_ban_list
    from trading.store.migrations import run_migrations
    from trading.strategy.rules import passes_not_fno_banned

    db = tmp_path / "d.db"
    with get_conn(db) as conn:
        run_migrations(conn)
        replace_fno_ban_list(conn, "2026-06-19", ["IDEA"])
        ctx = build_scan_context(conn, date(2026, 6, 19))
    assert passes_not_fno_banned("IDEA", ctx).passed is False
    assert passes_not_fno_banned("RELIANCE", ctx).passed is True
```

> Note: confirm the `RuleResult` boolean attribute name (`.passed`) against
> `src/trading/strategy/rules.py`; adjust the assertion to the real field if it
> differs.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_jobs_pre_open.py -k fno_ban -v`
Expected: FAIL — `ctx.fno_ban_symbols` is the empty default `frozenset()`.

- [ ] **Step 3: Implement the wiring**

Add the import alongside the other store imports (near `pre_open.py:43`):

```python
from trading.store.fno_ban_store import get_fno_ban_symbols
```

Replace the `build_scan_context` body's return + docstring tail (lines ~265–276):

```python
    `nifty200_drawdown_5d_pct` is left `None` (not yet computed/stored — the
    regime rule degrades gracefully). `fno_ban_symbols` ← today's `fno_ban_list`
    rows (F-010), reviving the `passes_not_fno_banned` veto. `t2t_symbols` still
    needs an NSE T2T feed (no table) and stays empty.
    """
    snap = get_macro_snapshot(conn, as_of.isoformat())
    india_vix = snap.vix if snap is not None else None
    critical = list_critical_symbols(conn, as_of.isoformat())
    ban = get_fno_ban_symbols(conn, as_of.isoformat())
    return ScanContext(
        scan_date=as_of,
        india_vix=india_vix,
        critical_event_symbols=frozenset(critical),
        fno_ban_symbols=frozenset(ban),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_jobs_pre_open.py -k fno_ban -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
uv run mypy src/trading/jobs/pre_open.py
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git diff --cached | grep -c "real money\|suspended indefinitely"   # expect 0
git commit -m "feat(F-010): build_scan_context reads fno_ban_list (revives gate)"
```

---

### Task 4: `_step_fno_ban` + orchestrator wiring

**Files:**
- Modify: `src/trading/jobs/pre_open.py` (import `fetch_fno_ban_symbols` + `replace_fno_ban_list`; add `_step_fno_ban`; call it before `_step_scan` at ~line 124)
- Test: `tests/test_jobs_pre_open.py`

**Interfaces:**
- Consumes: `fetch_fno_ban_symbols` (Task 1), `replace_fno_ban_list` (Task 2).
- Produces: `_step_fno_ban(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> int` (count of banned symbols persisted); called in `run_pre_open` immediately before `_step_scan`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_jobs_pre_open.py`:

```python
def test_step_fno_ban_persists_and_counts(tmp_path, monkeypatch) -> None:
    from datetime import date

    import trading.jobs.pre_open as po
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import get_fno_ban_symbols
    from trading.store.migrations import run_migrations

    monkeypatch.setattr(po, "fetch_fno_ban_symbols", lambda: ["IDEA", "GNFC"])
    db = tmp_path / "e.db"
    warnings: list[str] = []
    with get_conn(db) as conn:
        run_migrations(conn)
        n = po._step_fno_ban(conn, date(2026, 6, 19), warnings)
        assert n == 2
        assert get_fno_ban_symbols(conn, "2026-06-19") == ["GNFC", "IDEA"]
    assert warnings == []


def test_step_fno_ban_degrades_on_empty(tmp_path, monkeypatch) -> None:
    from datetime import date

    import trading.jobs.pre_open as po
    from trading.store.db import get_conn
    from trading.store.fno_ban_store import get_fno_ban_symbols
    from trading.store.migrations import run_migrations

    monkeypatch.setattr(po, "fetch_fno_ban_symbols", lambda: [])
    db = tmp_path / "f.db"
    warnings: list[str] = []
    with get_conn(db) as conn:
        run_migrations(conn)
        n = po._step_fno_ban(conn, date(2026, 6, 19), warnings)
        assert n == 0
        assert get_fno_ban_symbols(conn, "2026-06-19") == []
    assert any("ban list" in w.lower() for w in warnings)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_jobs_pre_open.py -k step_fno_ban -v`
Expected: FAIL — `AttributeError: module 'trading.jobs.pre_open' has no attribute '_step_fno_ban'`.

- [ ] **Step 3: Implement the step + wiring**

Add to the imports (near `pre_open.py:25–26`, with the other `trading.data` imports):

```python
from trading.data.fno_ban import fetch_fno_ban_symbols
```

Add to the store imports (near `pre_open.py:43`, beside the Task-3 import):

```python
from trading.store.fno_ban_store import get_fno_ban_symbols, replace_fno_ban_list
```

> If Task 3 already added `get_fno_ban_symbols`, replace that single-name import
> with the combined line above rather than importing twice.

Add the new step beside the other `_step_*` helpers (e.g. just after `_step_ohlcv`, ~line 254):

```python
def _step_fno_ban(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> int:
    """Fetch the NSE F&O ban list and persist it for `as_of`. Best-effort.

    `fetch_fno_ban_symbols` already swallows network errors (returns []). On an
    empty result — a real no-ban day or a feed outage — we still overwrite the
    date (clearing any stale rows) and warn that the gate degraded to a pass.
    Returns the number of banned symbols persisted.
    """
    symbols = fetch_fno_ban_symbols()
    replace_fno_ban_list(conn, as_of.isoformat(), symbols)
    if not symbols:
        warnings.append("F&O ban list empty/unavailable — ban gate degraded to pass")
    return len(symbols)
```

Wire it into `run_pre_open` immediately before the scan (at `pre_open.py:124`):

```python
        _step_fno_ban(conn, as_of, warnings)
        candidates = _step_scan(conn, p, as_of, warnings)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_jobs_pre_open.py -k step_fno_ban -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full pre_open test module (no regressions)**

Run: `uv run pytest tests/test_jobs_pre_open.py -v`
Expected: PASS (all green — the new step runs inside any full `run_pre_open` test; its fetch hits the network best-effort and returns `[]` offline, which is a warning, not a failure).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
uv run mypy src/trading/jobs/pre_open.py
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git diff --cached | grep -c "real money\|suspended indefinitely"   # expect 0
git commit -m "feat(F-010): _step_fno_ban populates ban list each pre-open"
```

---

### Task 5: Reserve the other 7 tables + update docs

**Files:**
- Modify: `src/trading/store/migrations.py` (SQL comments in the v1 string, ~lines 127–209)
- Modify: `docs/architecture/02-data-schema.md` (§4.2, ~lines 158–180; and the reserved note ~line 271)
- Modify: `docs/architecture/FINDINGS.md` (F-010 row + detail + roadmap line; F-019 note)
- Test: `tests/test_migrations.py` (assert the SQL still applies cleanly — likely already covered; add a guard only if missing)

**Interfaces:** Documentation/annotation only — no new symbols.

- [ ] **Step 1: Confirm the migration still applies (guard test)**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: PASS. This is the regression guard for the comment edits in Step 2 (SQL comments are inert; `CREATE TABLE IF NOT EXISTS` stays idempotent). If `tests/test_migrations.py` lacks a test that simply runs `run_migrations` on a fresh DB and asserts the 8 table names exist, add one now (red→green) before editing the SQL.

- [ ] **Step 2: Annotate the migration SQL**

In `src/trading/store/migrations.py`, add a comment line directly above each table in the v1 string. For `fno_ban_list`, mark it live; for the other 7, mark `RESERVED (F-010)` with the rationale:

```sql
-- F-010: LIVE — written daily by pre_open._step_fno_ban (NSE fo_secban.csv).
CREATE TABLE IF NOT EXISTS fno_ban_list (
```

```sql
-- RESERVED (F-010): no clean OI-history feed; revisit for an options-flow strategy.
CREATE TABLE IF NOT EXISTS oi_daily (
```

```sql
-- RESERVED (F-010): informational, no consumer; revisit for a smart-money signal.
CREATE TABLE IF NOT EXISTS bulk_block_deals (
```

```sql
-- RESERVED (F-010): yfinance already serves adjusted OHLCV; revisit if raw prices are stored.
CREATE TABLE IF NOT EXISTS corp_actions (
```

```sql
-- RESERVED (F-010): audit log for the real-money path (F-005, suspended); revisit at Phase 19.
CREATE TABLE IF NOT EXISTS account_events (
```

```sql
-- RESERVED (F-010): IEP persists to raw/<date> JSON; revisit if IEP history queries are needed.
CREATE TABLE IF NOT EXISTS preopen_snapshot (
```

```sql
-- RESERVED (F-010): intraday quotes persist to quotes_HHMM.json; DB mirror unneeded.
CREATE TABLE IF NOT EXISTS live_quotes (
```

```sql
-- RESERVED (F-010): NSE events land in news_items + sentiment_daily; revisit for a structured calendar.
CREATE TABLE IF NOT EXISTS event_calendar (
```

- [ ] **Step 3: Run the migration guard again**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: PASS (comments are inert).

- [ ] **Step 4: Update `docs/architecture/02-data-schema.md` §4.2**

Rewrite the §4.2 heading + body to reflect "7 dormant + 1 now live". Replace the section (lines ~158–180) with:

```markdown
### 4.2 Reserved tables (defined in v1) and the one now live (F-010)

`fno_ban_list` is **now written** daily by `pre_open._step_fno_ban` (NSE
`fo_secban.csv`) and read by `build_scan_context` into `ScanContext`, reviving
the `passes_not_fno_banned` veto (was dead per F-019). The other 7 tables remain
**schema reservations** — a reviewer should treat them as reserved, not live
data. → **F-010**.

| Table | Status | Rationale / revisit trigger |
|---|---|---|
| `fno_ban_list` | **LIVE** | Written by `_step_fno_ban`; feeds `passes_not_fno_banned` |
| `oi_daily` | Reserved | No clean OI-history feed; for an options-flow strategy |
| `bulk_block_deals` | Reserved | Informational, no consumer; for a smart-money signal |
| `corp_actions` | Reserved | yfinance serves adjusted OHLCV; for raw-price storage |
| `account_events` | Reserved | Audit log for the real-money path (F-005, suspended) |
| `preopen_snapshot` | Reserved | IEP persists to `raw/<date>` JSON |
| `live_quotes` | Reserved | Intraday quotes persist to `quotes_HHMM.json` |
| `event_calendar` | Reserved | NSE events land in `news_items` + `sentiment_daily` |

The critical-event veto (`passes_no_critical_event`) is served by
`sentiment_daily` (F-019), so `event_calendar` stays reserved without leaving a
gate dead. `t2t_symbols` has no table and still awaits an NSE T2T feed.
```

Then update the reserved note near line 271 to read: `→ F-010 (fno_ban_list now live; 7 reserved), F-011.`

- [ ] **Step 5: Update `docs/architecture/FINDINGS.md`**

Make four edits (do NOT touch the F-005 user-comment lines):

1. **Status table (line ~43):** remove `F-010` from the `Med` open row; add `F-010` to the `✅ Fixed` row and bump its count.
2. **Findings table row for F-010 (line ~170):** change Status to:
   `✅ Fixed 2026-06-19 — fno_ban_list now written by pre_open._step_fno_ban (NSE fo_secban.csv) + read by build_scan_context (revives passes_not_fno_banned); other 7 tables formally reserved in migration + schema doc`
3. **Detail §F-010 (lines ~309–315):** append a `**Resolution (2026-06-19):**` paragraph summarizing the writer + the 7 reservations, mirroring the schema-doc table.
4. **F-019 note (line ~69 and detail ~482):** update "`fno_ban`/`t2t` still need NSE feeds (F-010)" to "`fno_banned` now live via `fno_ban_list` (F-010); `t2t` still awaits an NSE T2T feed".
5. **Roadmap line (line ~99 and ~126):** change "Remaining: F-010 …" / "next is F-010" to note F-010 done 2026-06-19.

- [ ] **Step 6: Commit the docs + migration comments**

```bash
git add src/trading/store/migrations.py docs/architecture/02-data-schema.md docs/architecture/FINDINGS.md
git diff --cached | grep -c "real money\|suspended indefinitely"   # expect 0 — if not, you staged the F-005 note; unstage it
git commit -m "docs(F-010): mark 7 tables reserved, fno_ban_list live; close finding"
```

> If the leak-check prints non-zero, the F-005 user-comment line got staged via
> FINDINGS.md. Run `git restore --staged docs/architecture/FINDINGS.md`, redo the
> FINDINGS edits without touching lines 47–48 / 109–111, and re-stage.

---

### Task 6: Full verification + push

**Files:** none (verification + remote push only).

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -q`
Expected: PASS (all green). Investigate any failure before proceeding.

- [ ] **Step 2: Lint + type-check the whole tree**

```bash
uv run ruff check .
uv run mypy src/
```
Expected: clean.

- [ ] **Step 3: Confirm the working tree holds only intended changes**

Run: `git status`
Expected: the pre-existing untracked files (`.mcp.json`, `CLAUDE.md`, `Research/`, `data/README.md`, `docs/daily-workflow.md`, …) remain untracked and unstaged; no stray modifications.

- [ ] **Step 4: Push to origin/main**

```bash
git push origin main
```
Expected: the Task 1–5 commits land on `origin/main` (phase wrap-up always pushes).

- [ ] **Step 5: Report**

Summarize: `fno_ban_list` now populated each pre-open and feeding the revived F&O ban veto; 7 tables formally reserved; F-010 closed; all commits pushed.
```
