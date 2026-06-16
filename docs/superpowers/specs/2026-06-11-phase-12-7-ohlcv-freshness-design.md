# Phase 12.7 — OHLCV freshness hardening

**Date:** 2026-06-11
**Status:** ✅ implemented 2026-06-16 (F-018) — `data/ohlcv_refresh.py`,
`pre_open._step_ohlcv`/`_step_cross_check`, scan staleness guard +
`Candidate.bar_date`, `trading refresh-ohlcv` CLI. IST-clock centralisation
(§ "centralise the IST clock") deferred to F-004.

## Problem

On 2026-06-11 the pre-open run produced a brief whose every technical number
(RSI, SMA stack, ATR, rule passes) was computed on bars from 2026-05-13/14 —
a month stale. Investigation findings:

1. **No refresh step exists.** `pre_open._step_scan` reads parquet written by
   `trading ingest-history`, which was last run 2026-05-15 00:32. Nothing in
   `docs/daily-workflow.md` or the pre_open job refreshes OHLCV.
2. **No staleness guard.** `strategy/rules.py` evaluates `df.iloc[-1]` after
   `read_ohlcv(end=scan_date)` — the last bar at-or-before scan_date becomes
   "today's close" no matter how old it is. No warning fired.
3. **Partial/dirty store.** Only 12 of 60 universe symbols have parquet; 5 of
   those still hold Feb-2025 smoke-test data. COALINDIA's final bar
   (2026-05-14) has NaN OHLC with real volume — a partial-bar artifact from
   ingesting at 00:32 while Yahoo's daily bar was half-published.

yfinance itself is **not** lagging: a live fetch on 2026-06-11 09:15 IST
returned bars through 2026-06-10 with close 451.00 — exactly matching Kite's
official close — plus the forming 2026-06-11 bar.

## Alternatives considered

| Source | Verdict |
|---|---|
| yfinance (current) | Accurate and current when actually fetched (verified against Kite). Free, integrated. **Keep as primary.** |
| Kite historical via MCP | Broker-exact, but MCP is Claude-side only — Python jobs can't call it. Use Kite data already on disk (holdings.json `close_price`) as a cross-check instead. |
| NSE bhavcopy | Authoritative + free but needs a new fetcher; deferred — possible future validation layer. |
| Paid feeds (TrueData, EODHD…) | Not justified while free sources agree with the broker. |

## Design

### 1. `src/trading/data/ohlcv_refresh.py` (new)

```python
@dataclass(frozen=True)
class RefreshResult:
    symbols_refreshed: int
    symbols_failed: int
    bars_added: int
    warnings: list[str]

def refresh_ohlcv(paths: Paths, as_of: date, symbols: list[str] | None = None) -> RefreshResult
```

Per symbol (default: `load_universe()`):

- Read existing parquet via `store/ohlcv.py`. Determine last good bar date.
- Fetch the missing window from yfinance: `start = last_bar + 1 day`; when no
  parquet exists, fetch the full default history window (3y, matching
  `ingest_history`'s default start).
- **Hygiene on every fetched frame**:
  - NaN-OHLC rows (partial-bar artifact) are dropped inside
    `data/yfinance.py::fetch_ohlcv`, so the `ingest_history` backfill path
    benefits too;
  - same-day partial bars are excluded by calling `fetch_ohlcv` with
    `end=as_of` (end is exclusive) — refresh never requests the forming bar.
- Append to existing frame, dedupe on date (new wins), sort, write parquet.
- Per-symbol error isolation: one failed ticker appends a warning and
  continues; never raises out of the loop.
- No-op (0 bars) when the symbol is already current — must stay cheap so the
  daily run is fast.

### 2. pre_open wiring + CLI

- `_step_ohlcv(paths, as_of, warnings) -> int` (bars_added) inserted **before**
  `_step_scan` in `run_pre_open`. Failures degrade to warnings; the job never
  aborts on refresh problems (the scan guard below handles residual staleness).
- `PreOpenResult` gains `ohlcv_bars_added: int`; CLI pre-open table renders it.
- New CLI `trading refresh-ohlcv [--date YYYY-MM-DD] [--symbols/-s ...]` for
  manual runs, printing a Rich summary (refreshed / failed / bars added +
  warnings).
- `docs/daily-workflow.md` unchanged — refresh is inside `trading pre-open`
  (zero new manual steps). Add a note in the file's Notes section.

### 3. Staleness guard in the scanner

- `MAX_BAR_AGE_DAYS = 5` (calendar days; covers weekends + holiday clusters)
  in `strategy/rules.py`.
- In `scan()`: after loading a symbol's frame, if
  `scan_date - last_bar_date > MAX_BAR_AGE_DAYS`, skip the symbol and append a
  warning (`"SYMBOL: last bar YYYY-MM-DD is stale — skipped"`). Scan must
  return warnings to the caller — `scan()` gains an optional `warnings`
  accumulator param (list, appended in place) so `pre_open` can surface them.
- `Candidate` gains `bar_date: date`; `llm/context.py` renders it on the first
  candidate bullet line (`- close 462.25 (bar 2026-06-10), RSI …`) so the
  analyst always sees the data basis.

### 4. Kite close cross-check

- `cross_check_closes(paths, as_of, holdings) -> list[str]` in
  `data/ohlcv_refresh.py`: for each holding (already read from holdings.json
  by `_step_portfolio`), compare Kite `close_price` to the symbol's parquet
  last close; relative deviation > 0.5% → warning string
  (`"SYMBOL: parquet close 462.25 vs Kite close 451.00 (+2.5%) — stale or split?"`).
- Wired into `run_pre_open` after `_step_portfolio` (holdings are in scope
  there); warnings go into the run's warning list and the CLI output.
- Symbols without parquet are skipped silently (the guard/refresh already
  warn about those).

## Error handling summary

yfinance fully down → refresh emits warnings, scan guard skips stale symbols,
pre_open completes with fewer/zero candidates and loud warnings. The failure
mode changes from "silent stale brief" to "visible degraded run".

## Testing

- `tests/test_data_ohlcv_refresh.py` (mocked yfinance): incremental window
  start computed from last bar; full fetch when parquet absent; NaN-OHLC rows
  dropped; `>= as_of` rows dropped; dedupe prefers new bars; per-symbol error
  isolation; no-op when current; RefreshResult counts.
- `tests/test_rules.py` additions: stale symbol skipped + warning; fresh
  symbol passes; `bar_date` populated on Candidate.
- Cross-check tests: within-tolerance silent; >0.5% warns; missing parquet
  skipped.
- `tests/test_jobs_pre_open.py`: orchestrator calls `_step_ohlcv` before scan
  (mocked); `ohlcv_bars_added` on result; cross-check warnings surface.
- `tests/test_cli.py`: `refresh-ohlcv` happy path + failure exit.
- Snapshot re-record for `test_llm_context.ambr` (bar-date bullet).

## Rollout

1. Implement + tests green (TDD per task).
2. One-time backfill: `trading ingest-history` over the full 60-symbol
   universe (overwrites the 12 stale/dirty files).
3. Re-run today's pipeline on fresh data: `trading pre-open` →
   `trading pre-open-iep` → `/analyst` → `trading brief compile`. The
   2026-06-11 outputs produced before this fix are void.
