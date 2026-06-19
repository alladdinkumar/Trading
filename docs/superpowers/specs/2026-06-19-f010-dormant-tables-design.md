# F-010 — Dormant tables: implement `fno_ban_list`, reserve the other 7

**Date:** 2026-06-19
**Finding:** F-010 (`GAP`, Med, Phase 2) — 8 of 16 SQLite domain tables are defined
in schema v1 but have zero writers (dormant schema reservations).
**Decision:** Per-table. Implement a writer for the one table with both a live
feed and a real consumer (`fno_ban_list`); formally mark the other 7 reserved.

## Background

`migrations.py` (schema v1) creates 8 tables that nothing in `src/trading`
writes: `oi_daily`, `fno_ban_list`, `bulk_block_deals`, `corp_actions`,
`account_events`, `preopen_snapshot`, `live_quotes`, `event_calendar`.

Only `fno_ban_list` has both:
- **A live, stable feed** — NSE publishes a daily F&O securities-ban CSV.
- **A real consumer** — `strategy.rules.passes_not_fno_banned` already reads
  `ScanContext.fno_ban_symbols`, but F-019 left that field empty
  (`build_scan_context` never populates it), so the gate is a dead
  unconditional pass.

The other 7 are either no-feed, duplicate of an existing store, or gated to the
suspended real-money execution path (F-005). They get a formal reservation, not
code.

`t2t_symbols` (the sibling dead gate from F-019) has **no table** and is out of
F-010's scope; it stays empty.

No schema migration is required — all 8 tables already exist from v1. This work
adds writers and documentation only.

## Architecture

```
NSE fo_secban.csv ──(CachedSession, best-effort)──> data/fno_ban.py
                                                          │ list[str]
                                                          ▼
pre_open._step_fno_ban ──> store/fno_ban_store.replace_fno_ban_list ──> fno_ban_list
                                                                              │
build_scan_context ──> store/fno_ban_store.get_fno_ban_symbols ───────────────┘
        │ fno_ban_symbols=frozenset(...)
        ▼
ScanContext ──> strategy.rules.passes_not_fno_banned   (gate now live)
```

The fetch runs inside the existing unattended `pre_open` spine (alongside the
yfinance and news fetches) — it is not a broker/Kite step, so it needs no skill
orchestration.

## Components

### `data/fno_ban.py` — fetcher

```python
def fetch_fno_ban_symbols(session: CachedSession | None = None) -> list[str]: ...
```

- GETs `https://nsearchives.nseindia.com/content/fo/fo_secban.csv` via the shared
  `get_cached_session()` with the same UA header pattern used by the news
  fetchers.
- Tolerant parser: per non-blank line, skip the header/date line, extract the
  symbol token (uppercase ticker). The legacy file is quirky (pipe/comma
  variants), so the parser pins symbols by token shape rather than a fixed column
  index. Exact behaviour is fixed by a recorded fixture in tests.
- **Best-effort**, mirroring `NseEventsSource`: any exception, non-200, or empty
  body returns `[]` rather than raising — a feed outage must never kill pre-open.

### `store/fno_ban_store.py` — writer/reader

```python
def replace_fno_ban_list(conn, date_iso: str, symbols: Iterable[str]) -> None: ...
def get_fno_ban_symbols(conn, date_iso: str) -> list[str]: ...
```

- `replace_fno_ban_list` deletes existing rows for `date_iso` then inserts
  `(date, symbol)` per symbol — idempotent on re-run, no duplicates, and an empty
  list clears the date. Mirrors `reconciliation_store` style.
- `get_fno_ban_symbols` returns the symbols for the date (sorted, deduped).

### `jobs/pre_open.py` — wiring

- New `_step_fno_ban(conn, paths, as_of, warnings)` runs **before** `_step_scan`:
  `replace_fno_ban_list(conn, as_of.isoformat(), fetch_fno_ban_symbols())`.
  On any failure append a warning (`"F&O ban list unavailable — gate degraded"`)
  and leave the table empty.
- `build_scan_context` reads `get_fno_ban_symbols(conn, as_of.isoformat())` into
  `fno_ban_symbols=frozenset(...)`. Docstring updated: the F&O ban gate is now
  live; `t2t_symbols` stays empty (no feed/table).

### No new CLI command

Population rides the daily `pre_open` step, like the news fetch. A standalone
`trading refresh-fno-ban` is YAGNI for now (noted as a possible follow-up).

## Reservations — the other 7 tables

Annotated as `RESERVED` in `migrations.py` (comment above each table) and in
`docs/architecture/02-data-schema.md` §4.2, each with a one-line rationale and a
revisit trigger:

| Table | Rationale | Revisit when |
|---|---|---|
| `oi_daily` | No clean OI-history feed via Kite/yfinance | An options-flow strategy is added |
| `bulk_block_deals` | Informational only, no consumer | A smart-money signal is built |
| `corp_actions` | yfinance already serves split/div-adjusted OHLCV | Raw (unadjusted) prices are stored |
| `account_events` | Audit log for the real-money execution path | Phase 19 (F-005) is unsuspended |
| `preopen_snapshot` | IEP persisted as `raw/<date>` JSON | IEP history queries are needed |
| `live_quotes` | Intraday quotes already in `quotes_HHMM.json` | A DB mirror of ticks is needed |
| `event_calendar` | NSE events land in `news_items` + `sentiment_daily` | A structured forward calendar is needed |

## Error handling

- Fetch is best-effort and never blocks pre-open.
- On an empty/failed fetch the gate degrades to "pass": a banned stock could slip
  through, but a warning is emitted. This matches every other Layer-A gate and is
  acceptable for a paper run (the gate is additive risk-reduction, not
  load-bearing for correctness).

## Testing (TDD, no real network)

1. **Parser** (`data/fno_ban.py`): recorded fixture CSV → expected symbol list;
   malformed/empty body → `[]`; injected exception (fake session raising) → `[]`.
2. **Store** (`store/fno_ban_store.py`): `replace` + `get` roundtrip; second
   `replace` for the same date overwrites (no dupes); empty list clears the date.
3. **`build_scan_context`**: with ban rows present → `ctx.fno_ban_symbols`
   populated; a banned symbol fails `passes_not_fno_banned` through the real
   scan path.
4. **`_step_fno_ban` best-effort**: a fetch that raises → warning appended, table
   empty, `_step_scan` still runs.

## Docs touched

- `docs/architecture/02-data-schema.md` §4.2 — reservations + `fno_ban_list` now
  live.
- `docs/architecture/FINDINGS.md` — F-010 → Fixed (note the commit); update the
  F-019 note (F&O ban gate now live; `t2t` still pending an NSE feed).
- `jobs/pre_open.py` `build_scan_context` docstring.

## Out of scope

- T2T segment feed / gate (no table; separate follow-up).
- Any writer for the 7 reserved tables.
- A standalone `trading refresh-fno-ban` CLI command.
- Real-money execution (F-005, suspended indefinitely).
