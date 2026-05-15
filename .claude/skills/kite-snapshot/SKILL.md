---
name: kite-snapshot
description: Use when invoked at /kite-snapshot or when the user asks to refresh Kite holdings/GTTs/positions from MCP for a downstream Python job (pre_open, portfolio CLI). Fetches via mcp__kite__* tools, writes data/raw/YYYY-MM-DD/{holdings,gtts,positions}.json + _meta.json so trading.data.kite_snapshot.read_* can consume them.
---

# /kite-snapshot — fetch Kite data via MCP and write to disk

You are the Kite data ingest layer. Python jobs (pre_open, `trading portfolio`)
need today's holdings / GTTs / positions but cannot call MCP themselves. Your
job is to fetch via `mcp__kite__*` and write JSON files to
`data/raw/YYYY-MM-DD/`.

## Inputs

The user may pass a date. If absent, use today in `Asia/Kolkata`.

The user may pass a list of resources to snapshot (default: holdings + GTTs).
Positions are only needed by Phase 14 mid_day / post_close jobs.

## Auth probe

Always start by calling `mcp__kite__get_profile`. If it raises an auth error
(401 / not logged in), DO NOT write any files. Print:

> Kite MCP is not authenticated. Please run `mcp__kite__login` to complete
> the browser handshake, then re-invoke `/kite-snapshot`.

Then halt. The user will run `mcp__kite__login` (which opens a browser) and
re-invoke this skill.

## Resource fetch + write

For each requested resource:

| Resource | MCP tool | Output file |
|----------|----------|-------------|
| holdings | `mcp__kite__get_holdings` | `data/raw/<date>/holdings.json` |
| gtts | `mcp__kite__get_gtts` | `data/raw/<date>/gtts.json` |
| positions | `mcp__kite__get_positions` | `data/raw/<date>/positions.json` |

For each: call the MCP tool, map the response to the on-disk schema (see
below), and write to a `.tmp` file then rename to the final filename
(atomic replace).

## On-disk schema

Each file is a flat JSON list of dicts, one per record. Field names match the
dataclasses in `src/trading/data/kite.py` exactly so Python can do
`Holding(**row)` without a mapping layer.

### `holdings.json` row shape

```json
{
  "tradingsymbol": "RVNL", "exchange": "NSE", "isin": "INE415G01027",
  "quantity": 32, "average_price": 305.0, "last_price": 329.6,
  "close_price": 327.1, "pnl": 787.2, "day_change": 2.5,
  "day_change_percentage": 0.76
}
```

### `gtts.json` row shape

```json
{
  "id": 12345, "type": "single", "status": "active",
  "tradingsymbol": "RVNL", "exchange": "NSE",
  "trigger_values": [350.0], "last_price": 329.6,
  "created_at": "2026-05-10T10:00:00",
  "orders": [{"transaction_type": "SELL", "quantity": 32, "price": 350.0}]
}
```

### `positions.json` row shape

```json
{
  "tradingsymbol": "NTPC", "exchange": "NSE", "product": "CNC",
  "quantity": 10, "average_price": 303.0, "last_price": 305.5, "pnl": 25.0
}
```

If MCP returns an unfamiliar shape, map what you can (use the dataclass field
names) and ask the user about anything you don't know how to map. Do not
silently drop fields the dataclass requires.

## `_meta.json` (always written last)

After all requested resources are written, write `_meta.json`:

```json
{
  "snapshot_at": "<current ISO timestamp>",
  "source": "mcp",
  "skill_version": "1"
}
```

## After writing

Print a one-line summary per resource (`holdings: 12 rows`, `gtts: 3 rows`,
etc.) and the next-step suggestion:

> Snapshot ready. Now run `trading pre-open --date YYYY-MM-DD` (or
> `trading portfolio --date YYYY-MM-DD`).

## Failure modes

- MCP auth error → halt without writing files (see Auth probe).
- MCP tool returns empty list → write `[]` to the file. Empty is valid.
- MCP tool returns unexpected shape (missing required field) → ask the user
  before guessing. Do not write a partial / wrong file.
- Resource MCP tool errors mid-flight (network blip) → write the resources
  that did succeed + `_meta.json`, then surface which one failed and tell the
  user to re-invoke.
