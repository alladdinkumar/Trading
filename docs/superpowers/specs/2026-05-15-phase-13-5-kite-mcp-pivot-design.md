# Phase 13.5 — Kite-via-MCP Architecture Pivot

**Date:** 2026-05-15
**Status:** Approved
**Predecessors:**
- [Phase 3 — Kite Connect wrapper](2026-05-11-trading-system-design.md) (now demoted to fallback)
- [Phase 13 — pre_open MVP](2026-05-15-phase-13-pre-open-design.md) (this pivot revises `_step_portfolio`)

## 1. Context & motivation

Phase 3 wired the `kiteconnect` Python SDK directly into production paths
(pre_open, future portfolio CLI, future MTM). Phase 13 added a
`--skip-kite` flag and graceful-degradation paths so the job could run
when the user's daily Kite token wasn't refreshed.

Both choices are wrong for this user's workflow. The user has a Claude
Pro plan that exposes the **Kite MCP server** in Claude Code. Every Kite
call should go through MCP, not the SDK. There is no acceptable
"degrade gracefully" path for Kite data — without holdings we don't
know what to score, without GTTs we can't project hits, without
positions we can't MTM. Skipping is not an option in production.

This pivot:
- Makes MCP the only production path to Kite data.
- Removes all "skip Kite" flags and `KITE_*` env-var dependencies from
  production code paths.
- Keeps `src/trading/data/kite.py` (the SDK wrapper) in the repo as a
  manual fallback for cases where MCP itself is broken.

The trade-off is that pre_open is no longer self-contained — it
requires a `/kite-snapshot` skill invocation as a prerequisite. This is
acceptable because pre_open already required the `/analyst` skill in
the middle of its flow (Phase 12 design); requiring another skill at
the start is a smaller incremental cost.

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  /kite-snapshot skill (Claude Code, MCP-driven)                    │
│   1. mcp__kite__get_profile → verify auth                          │
│   2. If 401: tell user to run mcp__kite__login, halt without write │
│   3. mcp__kite__get_holdings → write data/raw/YYYY-MM-DD/holdings.json │
│   4. mcp__kite__get_gtts → write gtts.json (when needed by caller) │
│   5. mcp__kite__get_positions → write positions.json (Phase 14)    │
│   6. Write _meta.json with snapshot_at + source="mcp"              │
│   7. Print "Now run trading pre-open --date YYYY-MM-DD"            │
└────────────────────────────┬───────────────────────────────────────┘
                             │ (file handshake — same pattern as Phase 12 _context.md)
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  trading pre-open --date YYYY-MM-DD                                │
│   _step_portfolio reads data/raw/YYYY-MM-DD/holdings.json          │
│   → KiteSnapshotMissingError if absent                              │
│   → KiteSnapshotStaleError if _meta.snapshot_at > 12h or wrong day │
│   Either error → CLI exits with code 2 + "Run /kite-snapshot first"│
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│  FALLBACK (unwired from production, manual only)                   │
│  trading kite-emergency-snapshot --date YYYY-MM-DD                 │
│   uses src/trading/data/kite.py (kiteconnect SDK + KITE_* env)     │
│   writes the same JSON files + _meta.source="sdk-fallback"         │
│   Only invoked if MCP is broken.                                   │
└────────────────────────────────────────────────────────────────────┘
```

The Python jobs never call MCP and never instantiate a Kite client.
They only read JSON files. The skill is the only place that knows MCP
tool names. The fallback CLI is the only place that knows the SDK.

## 3. Components

### 3.1 New: `src/trading/data/kite_snapshot.py`

```python
class KiteSnapshotMissingError(RuntimeError):
    """Raised when an expected snapshot JSON is absent for `as_of`."""

class KiteSnapshotStaleError(RuntimeError):
    """Raised when _meta.snapshot_at's date doesn't equal the requested as_of."""


def read_holdings(paths: Paths, as_of: date) -> list[Holding]: ...
def read_gtts(paths: Paths, as_of: date) -> list[GttOrder]: ...
def read_positions(paths: Paths, as_of: date) -> list[Position]: ...
```

All three follow the same pattern: open `data/raw/<as_of>/<resource>.json`,
parse → list of dicts, splat each into the matching dataclass from
`data/kite.py`. Missing file → `KiteSnapshotMissingError` with the
expected path and remediation hint. Each reader also opens
`_meta.json` and checks that `snapshot_at`'s date equals the `as_of`
argument; on mismatch → `KiteSnapshotStaleError`. (Date-equality is
enough; pre_open uses D-1's close anyway, so intra-day freshness
within `as_of` is moot.)

### 3.2 New: `.claude/skills/kite-snapshot/`

`SKILL.md` — frontmatter (`name: kite-snapshot`, description focused
on "fetch live Kite data via MCP and write it to disk for downstream
Python jobs to consume") plus step-by-step:

1. Determine `as_of` (today in `Asia/Kolkata` unless user supplied).
2. Probe `mcp__kite__get_profile`. On auth error, print remediation
   and halt without writing files.
3. Call `mcp__kite__get_holdings` → write `data/raw/<as_of>/holdings.json`
   atomically (`.tmp` → rename).
4. Call `mcp__kite__get_gtts` → write `gtts.json` (only if the caller
   says they need GTTs; for Phase 13 alone, holdings is enough).
5. Write `_meta.json` with `snapshot_at` (current timestamp) and
   `source: "mcp"`.
6. Print summary + the next-step command.

The skill is the only place that knows MCP tool names or maps MCP
response shapes to the on-disk schema (§4).

### 3.3 Modify: `src/trading/jobs/pre_open.py`

- Remove `make_client` / `get_holdings` / `KiteAuthError` imports.
- Add `from trading.data.kite_snapshot import (KiteSnapshotMissingError,
  KiteSnapshotStaleError, read_holdings)`.
- Add a new `class PreOpenAborted(RuntimeError)` near the top of the
  module — raised when the run cannot proceed because a prerequisite
  is missing.
- Rewrite `_step_portfolio`: drop the `skip_kite` parameter; drop the
  Kite-token / SDK paths entirely; call `read_holdings(paths, as_of)`
  and let `KiteSnapshotMissingError` / `KiteSnapshotStaleError`
  propagate as `PreOpenAborted` (re-raised with the original message
  preserved).
- `run_pre_open` no longer accepts `skip_kite`. Drop it from the
  signature and the docstring.

### 3.4 Modify: `src/trading/cli.py`

- `pre-open` command: drop `--skip-kite`. Wrap `run_pre_open` in a
  try/except for `PreOpenAborted` → print the message in red, exit 2.
- `kite-login` command: rename to `kite-emergency-login`. Same
  behaviour. Help text starts "FALLBACK: …".
- New `kite-emergency-snapshot --date YYYY-MM-DD` command: uses the
  SDK wrapper to populate `data/raw/<as_of>/holdings.json` (and
  optionally `gtts.json`) when MCP is broken. Writes the same JSON
  shape and `_meta.json` with `source: "sdk-fallback"`. Help text
  starts "FALLBACK: …".

### 3.5 Unchanged

- `src/trading/data/kite.py` — SDK wrapper stays as-is. Used only by
  the two `kite-emergency-*` CLI commands now.
- `src/trading/portfolio/gtt.py` — uses `GttOrder` dataclass, which
  lives in `data/kite.py` and is unchanged.
- `src/trading/config.py` — `kite_api_key` / `kite_access_token` /
  `kite_api_secret` stay in `Settings` for the fallback. No production
  code reads them after this pivot.
- `pyproject.toml` — `kiteconnect` stays as a runtime dep (fallback).
- `.env.example` — `KITE_*` keys stay, marked as "fallback only".
- `tests/test_kite.py` and `tests/test_gtt.py` — unchanged.

## 4. On-disk file contract

```
data/raw/2026-05-15/
  holdings.json     ← required by pre_open _step_portfolio
  gtts.json         ← required by Phase 12.6 / future GTT viability
  positions.json    ← required by Phase 14 mid_day MTM
  _meta.json        ← {"snapshot_at": "2026-05-15T08:30:00",
                       "source": "mcp" | "sdk-fallback",
                       "skill_version": "1"}
```

JSON shape: a flat list of dicts, one per record. Field names match
the dataclass fields exactly so `Holding(**row)` works without a
mapping layer:

```json
// holdings.json
[
  {
    "tradingsymbol": "RVNL", "exchange": "NSE", "isin": "INE415G01027",
    "quantity": 32, "average_price": 305.0, "last_price": 329.6,
    "close_price": 327.1, "pnl": 787.2, "day_change": 2.5,
    "day_change_percentage": 0.76
  }
]
```

`_meta.json` is required. Both writers (skill and emergency CLI)
produce it; both readers (`read_*`) require it. This keeps stale
snapshots from sneaking into a different day's run and lets us audit
which path produced today's data.

## 5. Error handling

Single-user, local — fail loud, fix the cause.

| Failure | Behaviour |
|---|---|
| `holdings.json` missing for `as_of` | `read_holdings` raises `KiteSnapshotMissingError` with the expected path. `_step_portfolio` re-raises as `PreOpenAborted`. CLI catches → exit code 2 + "Run `/kite-snapshot` skill in Claude Code first." |
| `_meta.json` shows `snapshot_at`'s date ≠ `as_of` | `KiteSnapshotStaleError` — same exit-2 + guidance. User must re-run /kite-snapshot. |
| MCP `mcp__kite__get_profile` returns 401 inside the skill | Skill prints: "Kite MCP not authenticated. Run `mcp__kite__login` to authenticate, then re-invoke /kite-snapshot." Skill halts; **no** files are written (no silent partial snapshots). |
| `mcp__kite__get_holdings` succeeds but returns empty list | Write `holdings.json: []` + `_meta.json` normally. Pre_open's `_step_portfolio` returns empty list with no warning — empty holdings are a valid state for a new account. |
| Skill writes JSON but pre_open fails to parse (missing field, schema drift) | Plain `KeyError` / `TypeError` from `Holding(**row)`. Crash loud — surfaces a real bug in the skill's mapping. No try/except. |
| User runs `kite-emergency-snapshot` while a `/kite-snapshot` run is in flight | Both writers use atomic `.tmp` + rename, so partial reads are impossible. Last write wins. `_meta.source` reflects who wrote last. |
| Old snapshot files from previous days | Untouched. `data/raw/` keeps history; only the current `as_of` directory is read. Phase 17 cleanup is out of scope. |

## 6. Testing

| File | Coverage | Approx count |
|------|----------|--------------|
| `tests/test_kite_snapshot.py` (new) | `read_holdings/gtts/positions` happy path with stub JSONs in tmp_path; `KiteSnapshotMissingError` when file absent; `KiteSnapshotStaleError` when `_meta.snapshot_at` mismatches; empty-list snapshot returns `[]` cleanly; `Holding(**row)` shape preservation across the existing dataclass fields | ~8 |
| `tests/test_jobs_pre_open.py` (modify) | Replace 3 existing `test_step_portfolio_*` tests. New: (a) reads stub holdings.json from tmp_path → returns scored Holdings; (b) raises `PreOpenAborted` when snapshot missing; (c) `run_pre_open` no longer accepts `skip_kite`. | net +1 |
| `tests/test_cli.py` (modify) | Drop `--skip-kite` from the pre-open CLI happy-path test. Add 1 test for `kite-emergency-snapshot` (mocked SDK → JSON file written). Rename test for `kite-emergency-login`. | net +1 |
| `tests/test_kite.py` (unchanged) | Existing 15 SDK-wrapper tests stay — they cover the fallback path. | 0 change |
| `tests/test_gtt.py` (unchanged) | Uses `GttOrder` dataclass which still lives in `data/kite.py`. | 0 change |

Total: ~10 new tests, 3 replacements, no snapshot tests needed.

## 7. PROGRESS.md placement

Insert a new "Phase 13.5 — Kite MCP pivot" block between Phase 13 and
Phase 14, mirroring the layout used for Phase 12.5. Sub-tasks:

- 13.5.1 `data/kite_snapshot.py` + reader functions + typed errors + tests
- 13.5.2 `.claude/skills/kite-snapshot/SKILL.md` + MCP-call instructions + auth handling
- 13.5.3 Rewrite `_step_portfolio` to use `kite_snapshot`; drop `--skip-kite`; add `PreOpenAborted` + CLI exit handling
- 13.5.4 Rename `kite-login` → `kite-emergency-login`; add `kite-emergency-snapshot` CLI
- 13.5.5 Real-data smoke (run /kite-snapshot, then trading pre-open) + PROGRESS.md + commit + push

Status snapshot table gets a new row: `| 13.5 | Kite MCP pivot | [x] |`.

Update Phase 13's body block with a "Superseded by Phase 13.5" note
on sub-task 13.1 (the `_step_portfolio` design) so future readers see
the pivot.

## 8. Out of scope

- Migrating Phase 12.6 (sector_daily) to MCP — sector indices come from
  yfinance/nsepython, not Kite.
- Migrating yfinance OHLCV ingest to `mcp__kite__get_historical_data` —
  real change, separate spec.
- Phase 17 Task Scheduler implications — pre_open is no longer fully
  unattended; user wakes Claude Code, runs `/kite-snapshot`, runs
  `trading pre-open`, runs `/analyst`, runs `trading brief compile`.
  Documented; no scheduler changes here.
- Caching / dedupe of multiple snapshot pulls per day — last write
  wins, no version history.
- Pruning historical `data/raw/<date>/` directories — Phase 17 ops
  concern.
