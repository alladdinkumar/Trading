---
name: kite-quotes-snapshot
description: Use when invoked at /kite-quotes-snapshot or when the user asks to fetch fresh intraday quotes from Kite via MCP for a downstream Python job (mid_day MTM). Reads the symbol list from data/raw/YYYY-MM-DD/_quote_symbols.txt (written by `trading mid-day` prepare mode), calls mcp__kite__get_quotes, writes data/raw/YYYY-MM-DD/quotes_HHMM.json, updates _meta.quotes_at.
---

# /kite-quotes-snapshot — fetch intraday quotes via MCP

Python's `trading mid-day --apply` reads `data/raw/<date>/quotes_HHMM.json`
to drive paper-trade MTM. Your job is to refresh that file by calling
`mcp__kite__get_quotes` for whatever symbols Python pre-flighted.

## Inputs

The user may pass a date. If absent, use today in `Asia/Kolkata`.

## Pre-flight check

Read `data/raw/<date>/_quote_symbols.txt` (one ticker per line). If the
file is absent, halt without writing any quotes file. Print:

> No `_quote_symbols.txt` found for <date>. Run `trading mid-day --date
> <date>` (without --apply) first to generate the symbol list, then
> re-invoke /kite-quotes-snapshot.

## Auth probe

Call `mcp__kite__get_profile`. If it raises an auth error (401 / not
logged in), DO NOT write any files. Print:

> Kite MCP is not authenticated. Please run `mcp__kite__login` to
> complete the browser handshake, then re-invoke /kite-quotes-snapshot.

Then halt.

## Fetch quotes

Build the instrument list. Kite MCP `get_quotes` accepts identifiers
like `"NSE:RVNL"`, so qualify each ticker with its exchange. For
holdings + paper-trades the exchange is in the underlying data; for
the simple MVP, default to `NSE:` and let the user fix any BSE
mismatches manually if they appear.

Call `mcp__kite__get_quotes(instruments=["NSE:RVNL", "NSE:NTPC", ...])`.

## Output schema

Write `data/raw/<date>/quotes_HHMM.json` (HHMM = current local time
`%H%M`). Use atomic `.tmp` + rename. The file is a flat JSON list of
dicts; each row's field names match `data.kite.Quote` plus a
top-level `tradingsymbol`:

```json
[
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
]
```

The MCP response shape may differ — map fields explicitly. If a field
is missing from the MCP response, set it to `null` rather than dropping
the row. Use `null` for fields that don't apply (e.g. `oi` for
non-derivatives).

## Update `_meta.json`

Read `data/raw/<date>/_meta.json` if present (the morning
`/kite-snapshot` skill will have created it). Merge:

```json
{
  "quotes_at": "<current ISO timestamp>"
}
```

into the existing object and write back atomically. Preserve
`snapshot_at`, `source`, `skill_version` if they exist. If `_meta.json`
is absent (no morning snapshot today), create one with `source: "mcp"`.

## After writing

Print a one-line summary (`quotes: 12 rows captured at HH:MM`) and the
next-step suggestion:

> Quotes snapshotted. Now run `trading mid-day --date YYYY-MM-DD --apply`.

## Failure modes

- MCP auth error → halt without writing files (see Auth probe).
- `mcp__kite__get_quotes` returns empty dict → write `quotes_HHMM.json: []` +
  update `_meta`. mid_day will SKIP every open trade.
- MCP returns unfamiliar shape → ask user before guessing. Do not write
  a partial / wrong file.
