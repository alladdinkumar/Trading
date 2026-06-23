---
name: daily-workflow
description: Use when invoked at /daily-workflow, or when the user asks to run the daily trading pipeline, run through the day's workflow, "do the morning/pre-open routine", run mid-day MTM or post-close, or wants the workflow to self-fire at each block's time. Orchestrates the four IST time-gated blocks (pre-open, IEP, mid-day, post-close) end to end — running the CLI steps, driving the /kite-* MCP skills, pausing for Kite login when the session is dead, and self-scheduling a wake-up for the next block via ScheduleWakeup so one open session walks the whole day. Always use this for any "run the daily workflow / next block / whole day" request rather than firing the individual commands by hand.
---

# /daily-workflow — drive the day's trading pipeline, block by block

You are the conductor for the daily pipeline documented in
`docs/daily-workflow.md`. Instead of the operator firing each command by
hand at the right minute, you run the block whose window is open now, then
schedule your own wake-up for the next block so a single session walks the
whole trading day.

The pipeline is **interactive by design**: the `/kite-*` steps call Kite MCP,
which needs a browser login roughly once a day. You cannot click that link, so
when the session is dead you **pause and surface the link** rather than failing
the run. Everything that is pure Python you run unattended.

## The prime directive: one block per invocation, then re-arm

On each invocation:

1. **Resolve "now"** in IST (Asia/Kolkata — the machine clock is already IST).
2. **Gate on trading day** (see below). If it's not a trading day, don't run
   any block — just re-arm for tomorrow morning and stop.
3. **Kite login pre-flight** (see "Kite login pre-flight" below). Before doing
   any block work, probe auth and surface the login link if the session is
   dead. Stop and wait for the user to log in rather than marching into a Kite
   step that will fail mid-block.
4. **Pick the due block** — the block whose window is open now, or the most
   recent block today whose window has passed but whose work isn't done yet.
5. **Run that block** (steps below), skipping any step already done today
   (idempotency). Pause if a Kite step hits a dead session.
6. **Re-arm**: `ScheduleWakeup` for the next pending block's window so the
   session fires itself on time. When the last block of the day is done, do
   **not** re-arm — the day is complete.

Run **one block per turn**, not the whole day in a loop within a single turn —
the windows are hours apart and the point is to wake at each one.

## Trading-day gate

Weekends and NSE holidays are no-ops. Check before doing anything:

```bash
PYTHONUTF8=1 uv run python -c "from trading.ops.calendar import is_trading_day; from datetime import date; print(is_trading_day(date.today()))"
```

If it prints `False`, announce it, re-arm for ~08:00 the next day
(`ScheduleWakeup` with `delaySeconds` capped at 3600 — see "Re-arming"), and
stop. No block runs, no Slack, nothing written.

## The blocks

`<date>` = today's ISO date (e.g. `2026-06-17`). All times IST. Each block's
**done-marker** lets you detect completion and skip re-running.

### Pre-open block — window 08:30–08:45 · done-marker `data/research/<date>/brief.md`

| Step | Command | Kind | Writes |
|---|---|---|---|
| 1/5 | `/kite-snapshot` | MCP skill | `data/raw/<date>/holdings.json`, `gtts.json`, `_meta.json` |
| 2/5 | `trading pre-open --date <date>` | CLI | `data/research/<date>/_context.md`, base `macro_snapshot` row |
| 3/5 | `/macro-doctor` | MCP skill | `data/raw/<date>/macro_cross_HHMM.json`; gap-fills `macro_snapshot`, writes `macro_reconciliation` |
| 4/5 | `/analyst` | analysis skill | `data/research/<date>/macro_brief.md`, `sector_commentary.md`, `candidates/*.md` |
| 5/5 | `trading brief compile --date <date>` | CLI | `data/research/<date>/brief.md` |

**Why `/macro-doctor` sits at step 3 (after pre-open, before the brief):** step 2
(`trading pre-open`) writes a *single-source* (yfinance) `macro_snapshot`.
`/macro-doctor` then pulls Kite read-only cross-source figures and runs
`trading macro refresh --cross` (re-snapshots **with Kite gap-fill**, overwriting
the base row) plus `trading macro verify --cross` (writes the `macro_reconciliation`
rows the brief annotates). Running it *after* pre-open keeps the gap-filled snapshot
and the reconciliation rows coherent — running it before would let pre-open's macro
step clobber the gap-fill and strand the reconciliation rows against a stale
snapshot. Both land before step 5 so the compiled brief reflects the gap-filled
macro and shows F-036's `⚠ kite …` / `(unreconciled)` flags. Kite auth is already
alive (step 1 passed), so no second login is expected here.

**`/macro-doctor` is best-effort.** Unlike `/kite-snapshot`, it is *not* a halt
point: if cross-source data is absent, `macro verify` reports a mismatch (exit 1),
or the step otherwise fails, **warn and continue to `/analyst`** — the brief must
still compile. Its done-marker for idempotency is any `data/raw/<date>/macro_cross_*.json`;
if one exists for `<date>`, skip the step.

### IEP block — window 08:55–09:00 · done-marker: `pre-open-iep` ran (see note)

| Step | Command | Kind | Notes |
|---|---|---|---|
| 1/2 | `/kite-quotes-snapshot` | MCP skill | **Usually skipped.** Nothing writes `_quote_symbols.txt` for the IEP path, so the snapshot skill halts. Overnight quotes are *optional* — skip this step. |
| 2/2 | `trading pre-open-iep --date <date>` | CLI | Runs without quotes: warns "Overnight quotes unavailable", no gap filter / rerank. This is expected, not an error. |

### Open-fills block — window 09:15–09:25 · done-marker `data/research/<date>/open_fills.md`

| Step | Command | Kind | Writes |
|---|---|---|---|
| 1/3 | `trading open-fills --date <date>` | CLI (prepare) | `data/raw/<date>/_quote_symbols.txt` (from `_pending_entries.json`) |
| 2/3 | `/kite-quotes-snapshot` | MCP skill | `data/raw/<date>/quotes_HHMM.json` — **halt point** if Kite session dead |
| 3/3 | `trading open-fills --date <date> --apply` | CLI | `data/research/<date>/open_fills.md`; opens funded entries at live LTP |

This is where the day's paper entries actually open — pre-open only *records*
the funding-eligible candidates to `_pending_entries.json`; the fill happens
here at the live LTP (per the 2026-06-23 live-open-fills design), with qty, stop,
and target recomputed from that fill. If pre-open recorded no pending entries,
prepare is a no-op and nothing opens. Like mid-day, step 2 is a real **halt
point**: if the Kite session is dead, present the login link and stop — don't
open against a stale/empty quotes file.

### Mid-day block — window 12:25–12:35 · done-marker `data/research/<date>/mid_day_update.md`

| Step | Command | Kind | Writes |
|---|---|---|---|
| 1/3 | `trading mid-day --date <date>` | CLI (prepare) | `data/raw/<date>/_quote_symbols.txt` |
| 2/3 | `/kite-quotes-snapshot` | MCP skill | `data/raw/<date>/quotes_HHMM.json`, updates `_meta.json` |
| 3/3 | `trading mid-day --date <date> --apply` | CLI | `data/research/<date>/mid_day_update.md` |

### Post-close block — window 16:05–16:15 · done-marker `data/research/<date>/post_close_summary.md`

| Step | Command | Kind | Writes |
|---|---|---|---|
| 1/3 | `trading post-close --date <date>` | CLI (prepare) | `data/raw/<date>/_quote_symbols.txt` |
| 2/3 | `/kite-quotes-snapshot` | MCP skill | `data/raw/<date>/quotes_HHMM.json` |
| 3/3 | `trading post-close --date <date> --apply` | CLI | `data/research/<date>/post_close_summary.md` |

Monthly SIP (09:30) is **out of scope** — it's a once-a-month action, not part
of the daily loop. If the user wants it, run `/kite-snapshot` then
`trading sip --date <date>` separately.

## Kite login pre-flight

Every block except IEP needs a live Kite session (snapshot or quotes), and the
session dies roughly once a day. So at the **start of each invocation** — right
after the trading-day gate, before picking or running any block — confirm auth
and surface the login link up front instead of discovering a dead session
halfway through a block.

1. Probe auth by calling `mcp__kite__get_profile`.
2. **Authed** (profile returns) → say so briefly and continue to "Pick the due
   block".
3. **Not authed** (401 / "log in first" / session error) → call
   `mcp__kite__login` and present the returned link to the user as clickable
   markdown, prefaced by the ⚠️ AI-risk warning the login tool returns. Then
   **stop** — do **not** run any block and do **not** re-arm a wake-up. The
   user logs in via the link and re-invokes `/daily-workflow`; the pre-flight
   passes on the next run and the block proceeds.

This makes the very first thing the operator sees a login link when one is
needed, rather than a half-run block. Once the pre-flight passes, individual
Kite steps inside a block won't normally need to re-login that day.

## Driving the MCP (`/kite-*`) steps

Before any `/kite-snapshot` or `/kite-quotes-snapshot` step, **probe auth** by
calling `mcp__kite__get_profile`.

- **Authed** → invoke the skill normally (use the `Skill` tool — don't reach
  into the MCP yourself; the skill owns the schema mapping and atomic writes).
- **Not authed** (401 / "log in first") → call `mcp__kite__login`, present the
  returned link to the user as clickable markdown, and **stop the block there**.
  Do not re-arm a wake-up — the user must log in and re-invoke `/daily-workflow`.
  Surface clearly which step you stopped at so resuming is obvious.

Why pause instead of skip: the snapshot is the whole point of the MCP steps.
Skipping them silently would produce a hollow brief / a mid-day with no quotes.
Better to do every pure-Python step possible, then halt visibly on the one
thing only the human can unblock.

**`/macro-doctor` is the exception to the pause rule.** It also uses Kite
read-only, but auth is already established by `/kite-snapshot` at step 1, and its
output (gap-fill + reconciliation) is an *enhancement*, not load-bearing for the
brief. So treat it as best-effort: if it can't pull or reconcile, warn and move on
to `/analyst` rather than halting or re-arming. Never let macro-doctor place an
order — it is read-only by contract.

## Idempotency — never redo finished work

Before running a block, check its **done-marker** file. If it exists for
`<date>`, the block already ran today — report it and move to re-arming for the
next block. Within a block, skip any step whose output already exists (e.g. if
`_context.md` is present but `brief.md` isn't, resume at step 3/4). This makes
the skill safe to re-invoke after a login pause or a crash — it picks up where
it left off instead of clobbering good data.

The `brief.md` marker is recompiled by later blocks (mid-day/post-close append
to it), so treat "pre-open done" as: `_context.md` **and** `brief.md` both
exist and all five `macro_brief/sector_commentary/candidates` parts are present.

## Re-arming the next wake-up

After a block finishes (or when the current block isn't due yet), compute the
next pending block's window-start in IST and call `ScheduleWakeup`:

- `delaySeconds` = seconds from now until that window-start. The runtime clamps
  to **[60, 3600]**, so gaps longer than an hour (e.g. open-fills 09:25 → mid-day
  12:25) can't be hit in one sleep. That's fine: sleep 3600 as an hourly
  heartbeat, wake, see no block is due yet, and re-arm again. Each wake is
  cheap and keeps the loop alive. Block order through the day:
  pre-open (08:30) → IEP (08:55) → open-fills (09:15) → mid-day (12:25) →
  post-close (16:05).
- `prompt` = the literal `/daily-workflow` input so the next firing re-enters
  this skill. (If the user launched you via `/loop` with no prompt, pass the
  sentinel `<<autonomous-loop-dynamic>>` instead.)
- `reason` = what you're waiting for, specifically — e.g. "waiting for mid-day
  window 12:25" or "hourly heartbeat until 12:25 mid-day block".

Pick the delay by what you're actually waiting for:
- Next block is **> 60 min** away → `delaySeconds: 3600` (heartbeat).
- Next block is **≤ 60 min** away → set the exact seconds to its window-start.
- It's **after the post-close block and that block is done** → the day is
  complete. **Do not re-arm.** Announce the day is finished.

For robustness across a closed laptop or a hung step, the most reliable way to
run this skill all day is to launch it under the loop runner:
`/loop /daily-workflow` (dynamic pacing). It still self-arms either way, but
`/loop` makes the re-entry first-class. Mention this to the user if they want
true hands-off operation — and remind them the session must stay open and Kite
stays logged in.

## At the end of each turn, report

Keep the operator oriented. After running (or skipping) a block, print a short
status: what block you ran, which steps succeeded/were skipped and why, any
notable output (e.g. "6 paper trades all HELD"), whether you paused for login,
and when you'll next wake (the block name and its IST time). Brevity over
ceremony — this runs many times a day.

## Failure modes

- **Not a trading day** → re-arm for next morning, run nothing.
- **Kite session dead on an MCP step** → present login link, stop, don't
  re-arm. Resume on next manual `/daily-workflow` after login. (Exception:
  `/macro-doctor` is best-effort — if *it* alone can't reach Kite, warn and
  continue to `/analyst`; don't halt the block on it.)
- **`/macro-doctor` fails or `macro verify` reports a mismatch (exit 1)** → this
  is informational, not fatal. Note it in the turn report and continue; the brief
  still compiles and carries whatever reconciliation flags were written.
- **A CLI step errors** (bad data, missing input) → stop the block, surface the
  error and the exact command, don't re-arm into a broken state. Don't paper
  over it by continuing to the next step.
- **Invoked mid-window after a block already ran** → idempotency detects the
  done-marker; report "already done" and re-arm for the next block.
- **`ScheduleWakeup` unavailable** (not in a loop-capable context) → still run
  the due block, but tell the user you can't self-arm; suggest
  `/loop /daily-workflow` for self-firing.
