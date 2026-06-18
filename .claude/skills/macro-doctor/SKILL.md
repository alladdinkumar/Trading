---
name: macro-doctor
description: Use when invoked at /macro-doctor or when the user asks to pull a second-source reading of the macro figures (India VIX, USDINR) from Kite via MCP and cross-check / back-fill the stored macro snapshot. Writes data/raw/YYYY-MM-DD/macro_cross_HHMM.json, then runs `trading macro refresh` (gap-fill) and `trading macro verify` (cross-check).
---

# /macro-doctor — second-source macro reconciliation via Kite MCP

The macro snapshot (`macro_snapshot`, one row/day) is single-source (yfinance +
nsepython). Your job is to pull a **structured second source** from the read-only
Kite MCP session and let the deterministic CLI gap-fill anything missing and flag
any figure that disagrees. You are the orchestrator; **the CLI does every DB
write** (F-035/F-036).

> **Read-only.** Use only `mcp__kite__get_profile` / `get_quotes` / `get_ltp`.
> NEVER call `place_order`, `modify_order`, or any GTT/order tool here.

## Inputs

The user may pass a date. If absent, use today in `Asia/Kolkata`.

## Auth probe

Call `mcp__kite__get_profile`. If it raises an auth error (401 / not logged in),
DO NOT write any files. Print:

> Kite MCP is not authenticated. Please run `mcp__kite__login` to complete the
> browser handshake, then re-invoke /macro-doctor.

Then halt.

## Fetch the second source

Call `mcp__kite__get_quotes` for:

- **India VIX** — `"NSE:INDIA VIX"` (reliable; this is the primary value of this
  skill).
- **USDINR** — the near-month USDINR currency future on CDS as a proxy (e.g.
  `"CDS:USDINR<EXPIRY>FUT"`). Use `mcp__kite__search_instruments` if you need to
  resolve the current expiry. This is best-effort — if you cannot resolve it
  cleanly, omit `usdinr` and let `verify` mark it `missing_secondary`.

Read the last price (`last_price`) for each. Kite has **no FII/DII feed** — do
not attempt to source those; the CLI flags them `unreconciled` automatically.

## Output schema

Write `data/raw/<date>/macro_cross_HHMM.json` (HHMM = current local time `%H%M`).
Use atomic `.tmp` + rename. Include only the fields you actually captured:

```json
{
  "source": "kite_mcp",
  "captured_at": "<current ISO timestamp>",
  "vix": 19.55,
  "usdinr": 83.20
}
```

`source` must be `"kite_mcp"`. Omit `vix` / `usdinr` rather than writing a guess
or `null` for a figure you could not fetch — the reader treats an absent field as
"no second source for this figure".

## Hand off to the CLI

Run both, in order, relaying their output to the user:

1. `trading macro refresh --date <date> --cross data/raw/<date>/macro_cross_HHMM.json`
   — re-pulls the primary snapshot and **gap-fills** any VIX/USDINR still missing
   from the Kite reading, recording provenance.
2. `trading macro verify --date <date> --cross data/raw/<date>/macro_cross_HHMM.json`
   — cross-checks the (now refreshed) figures against Kite. It exits **1** on any
   `mismatch`.

If `verify` exits 1, surface the mismatching field(s) prominently and tell the
user to confirm the correct value before trusting the bundle — do **not** edit
any figure yourself.

## After writing

Print a one-line summary, e.g.:

> macro_cross captured at HH:MM (vix 19.55, usdinr 83.20); refresh + verify done —
> all reconciled.  (or: VIX mismatch — bundle 19.40 vs kite 22.10, please verify.)

Then suggest re-assembling the bundle if anything was gap-filled:

> Run `trading brief assemble-context --date YYYY-MM-DD` to refresh the bundle
> with the reconciled figures.

## Failure modes

- MCP auth error → halt without writing files (see Auth probe).
- `get_quotes` returns empty / unfamiliar shape → write only the fields you could
  parse (or nothing). Never write a guessed figure. `verify` will mark the rest
  `missing_secondary`.
- USDINR future expiry unresolved → omit `usdinr`; VIX alone is still useful.
