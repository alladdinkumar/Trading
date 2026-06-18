# Macro self-healing & cross-source reconciliation (F-035 / F-036)

_Design — 2026-06-18. Spun off from F-026 (deterministic narrative guardrails)._

## Problem

The macro snapshot (`macro_snapshot`, one row/day) is single-source and not
self-healing:

- **F-035 — no auto-refresh.** When a figure is stale or missing (e.g. yfinance
  rate-limited VIX to `None`), nothing re-pulls it. F-026 only *refuses* a stale
  bundle or *degrades* to `_(no data)_`; the remedy is a manual
  `assemble-context` re-run.
- **F-036 — no cross-verification.** VIX/USDINR/FII/DII come from one provider
  (yfinance + nsepython). A wrong upstream value enters the bundle unflagged.
  F-026's figure check only verifies the *brief against the bundle*, never the
  *bundle against reality*.

## Core principle — LLM orchestrates, code executes the writes

The Claude session holds a Kite MCP auth login, so a skill can pull **structured,
trusted** data (India VIX, USDINR proxy, prices/OHLC). That is the opposite of
LLM web-scraping and is a legitimate second source. But to preserve
reproducibility and an audit trail:

- The **skill is the brain**: detect stale/missing, auth-probe Kite, pull
  cross-source values (read-only), decide a refresh/verify is warranted.
- **Deterministic code is the hands**: a small `trading macro …` CLI does the
  tolerance math and every DB write, through a validated boundary with logged
  provenance.

This mirrors the existing `analyst → brief compile` and
`/kite-quotes-snapshot → trading mid-day` splits. The skill cannot corrupt the
DB; a tool-call hiccup can't silently write a wrong value.

### Source coverage (decided: "Kite + yfinance, flag gaps")

| Field      | Primary (existing) | Secondary (new, Kite MCP)        | On disagreement |
|------------|--------------------|----------------------------------|-----------------|
| VIX        | yfinance `^INDIAVIX` | `NSE:INDIA VIX` LTP             | reconcile, flag |
| USDINR     | yfinance `INR=X`   | USDINR near-month future (proxy) | reconcile, flag |
| FII / DII  | nsepython          | **none on Kite**                 | `unreconciled` flag only |
| candidate prices/OHLC | yfinance/Kite snapshot | `get_ltp`/`get_ohlc`   | (out of scope here) |

Kite is read-only (`get_quotes`/`get_ltp`/`get_ohlc`/`get_profile`) — never
`place_order`/`modify`. FII/DII cross-checking against an NSE second feed is
explicitly deferred (a larger data integration); they are flagged `unreconciled`,
not silently `ok`.

## Architecture

```
/macro-doctor skill                          deterministic code (trading CLI)
─────────────────                            ────────────────────────────────
1. detect stale/missing  ◄── reads ──────────  macro_snapshot row + bundle
2. auth-probe Kite (get_profile)
3. pull India VIX + USDINR-fut LTP
   via mcp__kite__get_quotes (READ-ONLY)
4. write macro_cross_HHMM.json ──── consumed by ──►  trading macro verify/refresh
                                                     ├─ validate JSON (F-002 boundary)
                                                     ├─ reconcile_macro() tolerance math
                                                     ├─ upsert macro_snapshot (fill gaps + provenance)
                                                     └─ upsert macro_reconciliation (flags)
5. read summary, report   ◄── prints ─────────────  status: ok / mismatch / unreconciled
```

## Components (code half — fully testable, no LLM)

1. **`macro_reconciliation` side table** (new migration). Columns:
   `date TEXT, field TEXT, primary_value REAL, primary_source TEXT,
   secondary_value REAL, secondary_source TEXT, abs_delta REAL,
   status TEXT, checked_at TEXT`, **PK `(date, field)`**. Provenance lives here,
   off `macro_snapshot`, so that table stays clean. `status ∈ {ok, mismatch,
   missing_primary, missing_secondary, unreconciled}`.

2. **`data/reconcile.py`** — pure
   `reconcile_macro(primary: MacroSnapshot, secondary: MacroCrossSource,
   tolerances) -> list[ReconRow]`. Per-field tolerance: VIX `abs ≤ 0.5`,
   USDINR `rel ≤ 0.5%`. No secondary → `unreconciled`; one side `None` →
   `missing_*`. Pure → unit-testable.

3. **Cross-source file schema + validated reader** —
   `data/raw/<date>/macro_cross_HHMM.json`:
   `{"source": "kite_mcp", "captured_at": "<iso>", "vix": 19.55,
   "usdinr": 83.20}`. Validated like the broker snapshot (F-002 pattern) →
   `MacroCrossSource` dataclass; malformed → typed error with remediation.

4. **`trading macro refresh --date <d>`** (F-035, phase 1). Deterministic
   re-fetch (`snapshot_and_classify`) + `upsert_macro_snapshot`. Optional
   `--cross <file>`: fill a field that is still `None` after the re-fetch from
   the Kite cross-source value, recording `primary_source="kite_mcp"` provenance
   in `macro_reconciliation`. Standalone re-pull for the stale/missing path that
   pre_open already does inline.

5. **`trading macro verify --date <d> --cross <file>`** (F-036, phase 2). Read
   the `macro_snapshot` row + validated cross-source file → `reconcile_macro` →
   upsert `macro_reconciliation` → print a per-field summary. Exit-1 on any
   `mismatch` so the daily flow / skill can react.

6. **`context._render_macro` annotation** (F-036, phase 2). Read the day's
   `macro_reconciliation` rows; annotate flagged figures inline, e.g.
   `| VIX | 19.40 ⚠ kite 19.55 |` or `| FII flow (₹ cr) | +1234 (unreconciled) |`.
   The bundle — and therefore F-026's brief cross-check — now carries the
   reconciliation state.

## Component (skill half — `/macro-doctor`, phase 2)

Orchestrates steps 1–5. Reuses `/kite-quotes-snapshot` conventions verbatim:
auth-probe `mcp__kite__get_profile` (on failure: do not write, print the login
hint, halt); pull `NSE:INDIA VIX` (+ USDINR future) via `mcp__kite__get_quotes`;
write `macro_cross_HHMM.json` atomically (`.tmp` + rename); then invoke
`trading macro refresh`/`verify` and relay the summary. Read-only Kite. On empty
Kite response, write the file with the available fields and let `verify` mark the
rest `missing_secondary`. Never places orders.

## What stays out of scope (and why)

- **LLM patching the DB directly** — breaks reproducibility/audit; all writes go
  through the CLI.
- **LLM web-search verification of precise figures** — unreliable, a
  hallucination vector. Verification is against structured feeds only.
- **NSE second feed for FII/DII** — a larger integration; flagged
  `unreconciled` for now.
- **Anything touching `compile_brief`'s network-free purity** — the bundle stays
  the compile-time source of truth (F-026). Self-healing happens upstream, in
  ingestion.

## Build phases

- **Phase 1 — F-035 (this commit).** Migration for `macro_reconciliation`
  (used in phase 2, created now), `trading macro refresh` (deterministic
  re-fetch + upsert; `--cross` gap-fill with provenance), and the validated
  `MacroCrossSource` reader. TDD; commit + push.
- **Phase 2 — F-036 + `/macro-doctor`.** `data/reconcile.py`,
  `trading macro verify`, `_render_macro` annotation, and the skill. TDD;
  commit + push.

## Testing

- `reconcile_macro` — agree / mismatch (over tolerance) / missing-primary /
  missing-secondary / no-secondary→unreconciled.
- `MacroCrossSource` reader — good file / malformed / missing fields → typed error.
- `trading macro refresh` — re-fetch upserts (monkeypatched fetchers); `--cross`
  fills only still-`None` fields and records provenance.
- `trading macro verify` — writes reconciliation rows, exit-1 on mismatch.
- `_render_macro` — annotates a flagged figure; no annotation when all `ok`.
- Skill: exercised manually (consistent with the other Kite skills).
