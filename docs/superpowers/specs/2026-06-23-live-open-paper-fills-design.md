# Live-Open Fills for the Paper Book — Design

**Date:** 2026-06-23
**Status:** Approved (design); pending spec review before implementation plan.

## Goal

Make paper-trade entries fill at a **live price after the market opens**, not at
the **previous close**. Today `pre_open._step_auto_open` opens each funded
candidate at `cand.close` — D-1's close ("limit order at close", spec §4.4) — at
08:30, before the market is open. On a gap day that price is unobtainable, so the
paper book books entries no real market order could have achieved, biasing P&L.

This design defers the actual open to a **new post-open block (~09:15–09:25)**
that fills each entry at the **live LTP at run time** and recomputes qty, stop,
and target from that fill. Pre-open becomes plan-and-record only.

## Background — what exists today

- **`jobs/pre_open.py::_step_auto_open(conn, as_of, scored, regime,
  pool_capital, daily_cap, risk_pct, warnings)`** — for each `ScoredCandidate`
  marked `selected`, computes `stop = cand.close − 1.5·atr_14`,
  `target = target_price(cand.close, stop)`, builds a `Signal(entry=cand.close,
  …)`, collects funding-eligible picks into `BudgetCandidate(entry=cand.close,
  …)`, runs `plan_daily_entries(...)`, and **opens** each funded entry via
  `log_signal_and_open_trade(entry_price=entry.entry, qty=entry.qty, …)`.
  Non-selected candidates and unfunded-but-selected ones are logged as
  visibility-only signals. `_already_opened_today` guards same-day duplicates.
  ATR per symbol lives only in the in-memory `atr_by_symbol` dict.
- **`strategy/daily_budget.py::plan_daily_entries(budget_cands, *, available_cash,
  deployed_by_symbol, regime, …)`** — pure; EV-ranks `BudgetCandidate`s by
  `p_win × implied_return` and greedy-fills the ₹daily_cap budget bounded by cash.
  It consumes each candidate's `entry`/`stop`/`target` as given — it does **not**
  care where the entry price came from, so feeding it a live LTP needs **no change**.
- **`paper/ledger.py::log_signal_and_open_trade(...)`** — atomic insert of
  signal + paper_trade + prediction; `entry_price`, `qty`, `atr_at_entry` are all
  caller-supplied.
- **`jobs/mid_day.py` + `trading mid-day` (two-phase prepare→apply)** — the
  existing template for "write `_quote_symbols.txt` → `/kite-quotes-snapshot`
  fetches live quotes → `--apply` consumes `quotes_HHMM.json`". The new block
  mirrors this exactly.
- **`/kite-quotes-snapshot` skill** — reads `data/raw/<date>/_quote_symbols.txt`,
  calls `mcp__kite__get_quotes`, writes `data/raw/<date>/quotes_HHMM.json`,
  updates `_meta.quotes_at`. A real halt point when Kite auth is dead.

## Key decisions (resolved during brainstorming)

1. **Price basis = live LTP at run time.** The entry fills at the last-traded
   price when the apply step runs (≈09:20), modelling a market order placed when
   the operator acts — not the day's 09:15 opening print, and not prev close.
2. **Qty/stop/target recomputed from the live fill.** Re-run `plan_daily_entries`
   with `entry = LTP`; `stop = LTP − 1.5·ATR`, `target = target_price(LTP, stop)`.
   This keeps 2%-risk sizing and the daily cap honest against the actual fill.
   ATR stays the D-1 `atr_14` computed at pre-open (not re-derived intraday).
3. **New post-open block, two-phase.** Pre-open stops opening trades; a new
   `trading open-fills` command (prepare→`/kite-quotes-snapshot`→apply) does the
   real open after market open. Mid-day and post-close are unchanged.
4. **Run for today (2026-06-23) once built.** Today's pre-open funded 0 trades, so
   this may still open nothing today — acknowledged.

## Architecture — split "decide" from "fill"

### Pre-open (08:30) — plan and record only

`_step_auto_open` no longer calls `log_signal_and_open_trade`. It still:

- ranks candidates and logs visibility-only signals for **non-selected**
  candidates (the existing `if not sc.selected` branch, unchanged),
- runs `plan_daily_entries` for the **brief preview** (so the brief shows intended
  buys), and
- writes the funding-eligible (selected) candidates to a handoff file
  **`data/raw/<date>/_pending_entries.json`**, one record per eligible symbol:

  ```json
  {"date": "2026-06-23",
   "regime": "NEUTRAL",
   "entries": [
     {"symbol": "TATASTEEL", "atr_14": 4.30, "ml_score": 0.61, "ref_close": 198.97}
   ]}
  ```

  `atr_14` is taken from the existing `atr_by_symbol`; `ref_close` is retained only
  for the open_fills drift report. The brief annotates these as
  "to be filled at the 09:15 open".

### New post-open block (~09:15–09:25) — `trading open-fills`

Two-phase, mirroring `mid-day`:

1. **`trading open-fills --date <date>` (prepare)** — reads `_pending_entries.json`,
   writes the symbol list to `data/raw/<date>/_quote_symbols.txt`. No-op with a
   clear message if the handoff file is absent or has no entries.
2. **`/kite-quotes-snapshot`** — live LTP into `data/raw/<date>/quotes_HHMM.json`.
   Real halt point if Kite auth is dead.
3. **`trading open-fills --date <date> --apply`** — for each pending symbol:
   - read **LTP** from the freshest `quotes_HHMM.json`;
   - `stop = LTP − 1.5·atr_14`; **skip with warning if `LTP ≤ stop`** (ATR too wide);
   - `target = target_price(LTP, stop)`;
   - build `BudgetCandidate(symbol, entry=LTP, stop, target, ml_score)`;
   - run `plan_daily_entries(..., available_cash=compute_paper_cash(conn, as_of),
     deployed_by_symbol=_deployed_by_symbol(conn), regime=<from handoff>)`;
   - for each funded entry, `_already_opened_today` guard, then
     `log_signal_and_open_trade(entry_price=LTP, qty=entry.qty, atr_at_entry=atr_14,
     entry_ts=<now IST>, attribution=EntryAttribution(regime, sector, neg_news_7d))`.
   - for each **selected-but-unfunded** symbol, insert a visibility-only signal at
     LTP plus its skip reason (this responsibility moves here from pre-open, since
     funding is now decided at open time);
   - write the done-marker `data/research/<date>/open_fills.md` summarising opened
     symbols (LTP, qty, prev-close→LTP drift %), and funded/skipped reasons.

Signal ownership: pre-open inserts signals **only** for non-selected candidates;
`open-fills` inserts the authoritative signal (via `log_signal_and_open_trade`)
for each funded entry and a visibility signal for each selected-but-unfunded one.
No symbol gets a duplicate signal. `plan_daily_entries` is reused **unchanged**.

## Data flow

```
pre_open (08:30): rank → log signals → plan (preview) → write _pending_entries.json
                  (NO paper_trades opened)
   │
open-fills prepare (~09:20): _pending_entries.json → _quote_symbols.txt
   │
/kite-quotes-snapshot: _quote_symbols.txt → quotes_HHMM.json (live LTP)
   │
open-fills --apply: LTP → recompute stop/target → plan_daily_entries(@LTP)
                  → log_signal_and_open_trade(@LTP) → open_fills.md
```

## Edge cases & error handling

- **Kite session dead at the snapshot step** → halts exactly like mid-day; the
  block re-runs cleanly after login (idempotent — see below).
- **No `_pending_entries.json`** (pre-open didn't run, or funded nothing eligible)
  → prepare reports "no pending entries", apply opens nothing. Not an error.
- **`LTP ≤ stop`** → skip that symbol with a warning; others still fill.
- **Idempotency** → `open_fills.md` is the block done-marker; within the block,
  `_already_opened_today(symbol)` prevents a second open if `--apply` re-runs.
- **`_quote_symbols.txt` shared with mid-day** → written fresh each run; quotes
  output is timestamped, so no clobber across blocks.
- **Available cash / deployed** → read live in the apply step, so funding reflects
  state at open time, not at 08:30.

## Daily-workflow (`SKILL.md`) changes

Insert a new block between IEP and mid-day:

> **Open-fills block — window 09:15–09:25 · done-marker `data/research/<date>/open_fills.md`**
>
> | Step | Command | Kind |
> |---|---|---|
> | 1/3 | `trading open-fills --date <date>` | CLI (prepare) — writes `_quote_symbols.txt` |
> | 2/3 | `/kite-quotes-snapshot` | MCP skill — live LTP; **halt point** if Kite dead |
> | 3/3 | `trading open-fills --date <date> --apply` | CLI — re-plan @ LTP, open trades |

Re-arming: pre-open/IEP → open-fills (09:15) → mid-day (12:25) → post-close (16:05).

## Testing (TDD)

- `pre_open` writes `_pending_entries.json` with eligible candidates and opens
  **zero** paper trades.
- `open-fills --apply` opens a funded entry at LTP with qty/stop/target recomputed
  from LTP (assert stop = LTP − 1.5·ATR, qty from `plan_daily_entries(@LTP)`).
- `LTP ≤ stop` → symbol skipped, others unaffected.
- Idempotency: second `--apply` opens nothing (guard + done-marker).
- Missing/empty `_pending_entries.json` → graceful no-op.
- Missing `quotes_HHMM.json` (Kite never ran) → apply halts with the documented
  warning, opens nothing.

## Out of scope

- Re-pricing or migrating historical trades already in the ledger — untouched.
- Changing the budget/sizing math (the in-flight `max_per_stock_pct` work in
  `daily_budget.py` is independent and left as-is).
- Monthly SIP, mid-day, and post-close flows.
