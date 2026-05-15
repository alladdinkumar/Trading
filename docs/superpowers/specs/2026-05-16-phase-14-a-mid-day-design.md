# Phase 14.A — mid_day MVP Design

**Date:** 2026-05-16
**Status:** Approved
**Predecessors:**
- [Phase 11 — paper-trade ledger + mtm](2026-05-11-trading-system-design.md) (`mtm_open_trades` is reused unchanged)
- [Phase 13 — pre_open MVP](2026-05-15-phase-13-pre-open-design.md) (file-handshake pattern)
- [Phase 13.5 — Kite MCP pivot](2026-05-15-phase-13-5-kite-mcp-pivot-design.md) (MCP-driven snapshot pattern)

## 1. Context & motivation

Phase 14 in PROGRESS.md bundles three independent jobs (mid_day @ 12:30,
post_close @ 16:00, pre_open_iep @ 08:55). Each has its own data flow,
its own MCP snapshot need, and could ship independently. We split it
into 14.A / 14.B / 14.C and tackle mid_day first because:

- It exercises the most-reused infrastructure (live quotes, MTM) that
  post_close also needs — building it first de-risks 14.B.
- It produces user-visible value the day it ships (intraday paper-trade
  exits actually trigger).

The MVP is intentionally narrow: quote snapshot via MCP → MTM →
markdown update. Kill-switches and volume-spike scan from spec §10
are explicitly out of scope (§7).

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  trading mid-day --date YYYY-MM-DD             (mode: prepare)     │
│   • gather_quote_symbols(conn, paths, as_of):                      │
│       open paper-trade symbols ∪ today's signals.symbol            │
│       ∪ holdings.json symbols (degraded if absent)                 │
│   • write data/raw/<as_of>/_quote_symbols.txt (sorted, deduped)    │
│   • print "now run /kite-quotes-snapshot, then re-run with --apply"│
└──────────────────────────────┬─────────────────────────────────────┘
                               │ (file handshake, same as Phase 13.5)
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  /kite-quotes-snapshot skill (MCP-driven)                          │
│   1. mcp__kite__get_profile → auth probe (halt + remediation on 401)│
│   2. read data/raw/<date>/_quote_symbols.txt                       │
│   3. mcp__kite__get_quotes(symbols)                                │
│   4. write data/raw/<date>/quotes_HHMM.json (atomic .tmp+rename)   │
│      — HHMM is the capture time so multiple snapshots per day      │
│        coexist without overwriting each other                      │
│   5. update data/raw/<date>/_meta.json with quotes_at: <ts>        │
│      (preserves snapshot_at from the morning /kite-snapshot run)   │
│   6. print "Now run trading mid-day --date <date> --apply"         │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ (file handshake)
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  trading mid-day --date YYYY-MM-DD --apply       (mode: apply)     │
│   • read_latest_quotes(paths, as_of) → newest quotes_HHMM.json     │
│       — QuoteSnapshotMissingError if no quotes file for today      │
│       — QuoteSnapshotStaleError if newest > 30 min old             │
│       — both re-raised as MidDayAborted → CLI exit 2 + remediation │
│   • _quotes_to_bars: build dict[symbol, Bar] with                  │
│       close = quote.last_price (not quote.close = yesterday's)     │
│   • paper.mtm.mtm_open_trades(conn, bars, as_of)                   │
│   • _render_mid_day_update → write data/research/<date>/mid_day_update.md │
└────────────────────────────────────────────────────────────────────┘
```

The two-phase invocation (`prepare` then `apply`, with the skill in
between) keeps Python out of MCP's reach — same pattern as Phase 13.5
and the Phase 12 `/analyst` flow.

## 3. Components

### 3.1 New: `src/trading/data/quotes_snapshot.py`

```python
class QuoteSnapshotMissingError(RuntimeError):
    """No quotes_*.json file present for the requested date."""

class QuoteSnapshotStaleError(RuntimeError):
    """Newest quotes_*.json exists but its capture time is too old."""


def read_latest_quotes(
    paths: Paths,
    as_of: date,
    *,
    max_age_minutes: int = 30,
) -> tuple[dict[str, Quote], datetime]:
    """Find the most recent `quotes_HHMM.json` for `as_of`, parse → dict[symbol, Quote].

    Returns (quotes_by_symbol, capture_ts). Capture_ts comes from the
    filename's HHMM component combined with `as_of` (the filename is
    the single source of truth for capture time; `_meta.quotes_at` is
    informational only). Staleness compares `capture_ts` against
    real-time `datetime.now()` — the file is "stale" if more than
    `max_age_minutes` of wall-clock time has passed since capture.
    Each row's `tradingsymbol` field is popped before splatting into
    `Quote(**row)` (Quote dataclass doesn't carry the symbol; we
    use it as the dict key instead). Raises `QuoteSnapshotMissingError`
    if no `quotes_*.json` exists for `as_of`, `QuoteSnapshotStaleError`
    if the staleness threshold is exceeded. Reuses `Quote` from
    `data/kite.py`.
    """
```

Pure file I/O. No network, no DB. Tests seed `tmp_path/data/raw/<date>/quotes_HHMM.json` directly.

### 3.2 New: `src/trading/jobs/mid_day.py`

```python
@dataclass(frozen=True)
class MidDayResult:
    as_of: date
    quotes_capture_ts: datetime | None      # None when mode=prepare
    bars_built: int                          # 0 when mode=prepare
    trades_evaluated: int                    # 0 when mode=prepare
    trades_closed: int
    trades_held: int
    update_path: Path | None                 # None when mode=prepare
    symbols_path: Path | None                # written when mode=prepare
    warnings: list[str]


class MidDayAborted(RuntimeError):
    """Raised when run_mid_day cannot proceed (analogue of PreOpenAborted)."""


def gather_quote_symbols(
    conn: sqlite3.Connection, paths: Paths, as_of: date
) -> list[str]:
    """Return sorted, deduped symbol list: open paper-trades ∪ signals(as_of) ∪ holdings.

    Holdings come from the existing kite_snapshot.read_holdings; if it
    raises (e.g. missing) the function still returns paper+signals — the
    mid-day MTM doesn't strictly need holdings, but the briefing context
    benefits from quoting them.
    """


def run_mid_day(
    as_of: date,
    *,
    paths: Paths | None = None,
    apply: bool = False,
) -> MidDayResult:
    """Orchestrate mid_day. `apply=False` (default) writes the symbol-list
    file and exits. `apply=True` reads the latest quotes JSON, runs MTM,
    writes the markdown update.
    """
```

Plus private helpers `_quotes_to_bars(quotes, as_of) -> dict[str, Bar]`
and `_render_mid_day_update(capture_ts, results) -> str`.

`_main` typer entry mirrors `pre_open.py`'s pattern.

### 3.3 New: `.claude/skills/kite-quotes-snapshot/SKILL.md`

Step-by-step instructions:
1. Auth probe via `mcp__kite__get_profile`. On 401, halt without writing.
2. Read `data/raw/<date>/_quote_symbols.txt`. If missing, halt and tell user
   to run `trading mid-day --date <date>` (prepare) first.
3. Call `mcp__kite__get_quotes(symbols)` (chunked if >100 symbols, but
   the MVP universe is <50 so chunking is a YAGNI item).
4. Write `data/raw/<date>/quotes_HHMM.json` atomically (`.tmp` + rename).
   `HHMM` is current local time `%H%M`.
5. Read `_meta.json` if present, merge `quotes_at: <iso ts>`, write back
   atomically. If `_meta.json` is absent (no morning snapshot), create
   one with `source: "mcp"`.
6. Print summary + next-step instruction.

### 3.4 Modify: `src/trading/cli.py`

```
trading mid-day --date YYYY-MM-DD [--apply]
```

Without `--apply`: prepare mode. With `--apply`: apply mode. On
`MidDayAborted`, print remediation in red, exit 2. Otherwise print a
Rich summary table (trades evaluated / closed / held / warnings) +
update markdown path.

### 3.5 Modify: `src/trading/llm/briefing.py` (small extension)

`compile_brief(date_dir, mode)` already supports `pre_open` and
`post_close`. Add `mode="mid_day"`:
- Required parts: `macro_brief.md`, `candidates/{SYMBOL}.md` per symbol.
- Optional parts: `sector_commentary.md`, **`mid_day_update.md`**.
- Compiled brief order: header → Macro → Sector → Candidates →
  **Mid-day update** (if present) → (Post-close recap, post_close mode only).

This change is purely additive — existing pre_open and post_close paths
are unchanged. The new mode is opt-in for callers that want the mid-day
section in the compiled brief.

### 3.6 New: `scripts/mid_day.bat`

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

### 3.7 Reused unchanged

- `paper.mtm.mtm_open_trades` — drives all exit decisions.
- `paper.ledger.open_trades` — listing open paper-trades.
- `data.kite.Quote` dataclass — typed shape for parsed JSON rows.
- `data.kite_snapshot.read_holdings` — for holdings symbols in pre-flight.
- `store.repo.list_signals_by_date` — for today's signal symbols.

## 4. On-disk file contract

```
data/raw/2026-05-16/
  _quote_symbols.txt    ← sorted unique tickers, written by mid_day prepare
  quotes_1232.json      ← list[Quote-shaped dict], written by skill
  quotes_1430.json      ← (if /kite-quotes-snapshot ran twice — newest wins)
  _meta.json            ← {snapshot_at: <morning>, quotes_at: <latest>, source, skill_version}
  holdings.json         ← from /kite-snapshot (Phase 13.5)
  gtts.json             ← from /kite-snapshot (Phase 13.5)
```

Quote row shape (matches `data.kite.Quote` fields):

```json
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
```

The `tradingsymbol` field is added at the row top-level by the skill so
`read_latest_quotes` can build the `dict[symbol, Quote]` index.
`Quote(**row)` requires the field to be ignored or the dataclass
extended; since the existing `Quote` dataclass doesn't have
`tradingsymbol`, `read_latest_quotes` pops the key before splatting.

## 5. Quote → Bar conversion

```python
Bar(
    date=as_of,                  # the trading day
    open=quote.open,             # today's open price
    high=quote.high,             # intraday high so far
    low=quote.low,               # intraday low so far
    close=quote.last_price,      # current LTP — drives MTM evaluation
    volume=quote.volume,
)
```

Critical: `close = quote.last_price`, **not** `quote.close` (which is
yesterday's close in Kite's convention). The exit logic checks
`bar.low <= current_stop` and `bar.high >= target` against intraday
extremes — using `quote.close` would mask intraday hits.

## 6. `mid_day_update.md` shape

```markdown
## Mid-day update — captured 2026-05-16T12:32:14

| symbol | action | exit price | reason | new stop |
|---|---|---|---|---|
| RVNL | EXIT_STOP | 295.00 | bar.low ≤ stop | — |
| NTPC | HOLD | — | trail moved | 311.50 |
| COALINDIA | HOLD | — | no movement | 458.40 |

3 open trades evaluated; 1 closed (EXIT_STOP/TARGET/TIME); 2 held; 0 skipped (no quote).
```

`compile_brief(mode="mid_day")` includes this section after the
candidates section if the file is present.

## 7. Error handling

| Failure | Behaviour |
|---|---|
| `_quote_symbols.txt` missing when skill runs | Skill halts without writing `quotes_*.json`. Tells user to run prepare first. |
| MCP `get_profile` 401 | Same as `/kite-snapshot`: print `mcp__kite__login` remediation, halt without writing. |
| MCP `get_quotes` returns empty dict | Skill writes `quotes_HHMM.json: []` + updates `_meta`. `mid_day --apply` reads it, every open trade flows through `mtm` as SKIP. Update markdown notes the skips. |
| `mid_day --apply` with no `quotes_*.json` for today | `QuoteSnapshotMissingError` → `MidDayAborted` → exit 2 + "Run /kite-quotes-snapshot first". |
| Newest `quotes_*.json` > 30 min old | `QuoteSnapshotStaleError` → exit 2 + "quotes are stale, re-run /kite-quotes-snapshot". |
| Open paper-trade for symbol absent from snapshot | `mtm_open_trades` flows it through as `SKIP` with "no bar for symbol" (existing Phase 11 behaviour). Update markdown lists it as a warning row. |
| Re-run `mid_day --apply` against same quotes file | `mtm_open_trades` iterates over **open** trades only; closed ones are excluded. Re-runs naturally re-evaluate only still-open ones. No special guard needed. |
| Holdings.json missing during pre-flight | `gather_quote_symbols` degrades: returns paper-trade symbols ∪ signal symbols, omits holdings. Warns in stdout. Still proceeds. |

## 8. Testing

| File | Coverage | Approx count |
|------|----------|--------------|
| `tests/test_quotes_snapshot.py` (new) | `read_latest_quotes` happy path; missing → `QuoteSnapshotMissingError`; stale → `QuoteSnapshotStaleError`; multiple snapshots → newest wins; empty list returns `{}` | 5 |
| `tests/test_jobs_mid_day.py` (new) | `gather_quote_symbols` unions paper+signals+holdings (and degrades on missing holdings); `_quotes_to_bars` builds bars with `close=last_price` not `quote.close`; `run_mid_day(apply=False)` writes `_quote_symbols.txt`; `run_mid_day(apply=True)` reads quotes + calls mtm + writes markdown; `MidDayAborted` raised on missing/stale; idempotent re-run produces same result on already-closed trades | 7 |
| `tests/test_cli.py` (extend) | `trading mid-day --date X` (prepare) writes symbols file + prints next-step; `trading mid-day --date X --apply` happy path; aborts with exit 2 when quotes missing | 3 |
| `tests/test_llm_briefing.py` (extend) | `compile_brief(mode="mid_day")` appends `mid_day_update.md` when present; required-parts list still raises on missing macro/candidates | 2 |

Total: ~17 new tests. Existing tests stay green — only briefing.py
adds a new branch keyed on `mode="mid_day"`.

## 9. Sub-task breakdown (PROGRESS.md)

```
## Phase 14.A — mid_day MVP

- [ ] 14.A.1 src/trading/data/quotes_snapshot.py: read_latest_quotes +
       QuoteSnapshotMissingError / StaleError + tests
- [ ] 14.A.2 .claude/skills/kite-quotes-snapshot/SKILL.md: reads
       _quote_symbols.txt, writes quotes_HHMM.json, updates _meta.quotes_at
- [ ] 14.A.3 src/trading/jobs/mid_day.py: gather_quote_symbols +
       run_mid_day (prepare/apply) + _quotes_to_bars + _render_mid_day_update
       + MidDayAborted + MidDayResult + tests
- [ ] 14.A.4 src/trading/cli.py: trading mid-day --date YYYY-MM-DD [--apply]
       subcommand + tests
- [ ] 14.A.5 src/trading/llm/briefing.py: extend mode to "mid_day"
       (appends mid_day_update.md when present) + tests
- [ ] 14.A.6 scripts/mid_day.bat Windows launcher
- [ ] 14.A.7 Real-data smoke (prepare → /kite-quotes-snapshot → apply) +
       PROGRESS.md + commit + push
```

Phase 14.B (post_close) and 14.C (pre_open_iep) get their own specs
after 14.A ships.

## 10. Out of scope

- **Kill-switches** (VIX/Nifty intraday breach detection + `data/state/kill_switches_<date>.json` flag for next pre_open). Defer to a 14.A.8+ follow-up or fold into 14.B.
- **Volume-spike scan** on watchlist. New feature with no spec detail; defer.
- **Auto-scheduling** (Phase 17 Task Scheduler).
- **Multi-snapshot history retention / pruning** — `data/raw/` keeps everything; cleanup is a Phase 17 ops concern.
- **Exit price slippage modelling** — `mtm_open_trades` uses `bar.close` directly; that's fine for paper trading.
- **Quote chunking for >100 symbols** — current universe is <50; YAGNI.
- **Phase 14.B (post_close) and Phase 14.C (pre_open_iep)** — separate specs after 14.A ships.
