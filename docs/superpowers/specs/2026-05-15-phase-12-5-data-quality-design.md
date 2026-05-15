# Phase 12.5 — Data quality cleanup (pre-Phase-13 prep)

**Date:** 2026-05-15
**Status:** Approved
**Predecessor:** [Phase 12 LLM-skill design](2026-05-15-phase-12-llm-skill-design.md)
**Memory:** `project_data_quality_gaps_2026_05_15` (saved end-of-Phase-12 smoke test)

## 1. Context & motivation

The Phase 12 real-data smoke test on 2026-05-15 produced a coherent `brief.md`
but exposed four upstream data-quality gaps. None are Phase 12 bugs — the
renderer correctly flagged each as `_(no data)_` — but the daily brief reads
as half-broken if every candidate carries a `sentiment: _(no daily aggregate)_`
line and several have `close NaN`. The Phase 13 MVP would inherit this and
the user would lose trust in the system.

Phase 12.5 fixes three of the four gaps quickly and downgrades the fourth
(`sector_daily`) to optional, with a follow-up Phase 12.6 to build it
properly.

## 2. Sub-tasks

### 12.5.1 — Drop trailing all-NaN-OHLC rows

**Source of bug:** yfinance returns a row for the current trading day with
NaN OHLC because the day hasn't closed yet. RECLTD's parquet, for example,
shows `2026-05-14 NaN NaN NaN NaN 5287079`. The scanner then computes
indicators on a NaN close, propagates NaN through the rule evaluators, and
the candidate row in `_context.md` shows `close nan, RSI nan`.

**Fix:** in `src/trading/store/ohlcv.py`, after `pd.read_parquet`, drop any
trailing rows where `close` is NaN. Implement as a small private helper
`_drop_trailing_nan_close(df)` so it's testable in isolation. The fix
applies in `read_ohlcv` so all consumers (scanner, MTM, technicals_from_history)
get clean data.

**Test:** `tests/test_ohlcv_store.py` — synthetic frame with two valid rows
followed by one NaN-close row → returns 2 rows, drops the NaN row. Edge
case: all-NaN frame → returns empty frame (no error).

### 12.5.2 — Filter future-dated news at render time

**Source of bug:** `NseEventsSource` writes corporate events into `news_items`
with their *future* event date as `ts` (e.g. "RVNL: Financial Results on
2026-05-21" appears in news_items with ts=2026-05-21 even though today is
2026-05-15). The "Recent headlines" section then leaks future events.

**Why not the cleaner fix:** the architecturally correct fix is to route NSE
corporate events into the existing `event_calendar` table instead of
`news_items`. That's a larger refactor (touches news ingest, sentiment
aggregation, and the renderer's expectations) and belongs in Phase 13's
event-risk wiring. For Phase 12.5 we apply a targeted render-time filter.

**Fix:** in `src/trading/llm/context.py`, change
`_render_news_for_symbol`'s SQL from `WHERE symbol = ? AND ts >= ?` to also
add `AND ts <= ?` where the upper bound is `as_of.isoformat() + 'T23:59:59'`.
Add a one-line comment pointing at `event_calendar` as the long-term home.

**Test:** `tests/test_llm_context.py` — seed a headline at
`2026-05-20T10:00:00` (future) and one at `2026-05-13T10:00:00` (past) → only
the past one renders. Re-record the existing
`test_full_pre_open_bundle_snapshot` since the SQL change doesn't affect its
seeded data but the snapshot is sensitive to the SQL clause.

### 12.5.3 — Sentiment aggregate refresh

**Source of "bug":** the existing smoke test ran `trading ingest-news` with
`--skip-score`, so the news_items table has 511 rows but no FinBERT
scoring and almost no `sentiment_daily` aggregates. The `aggregate_daily`
function works correctly (Phase 8 tests cover it); we just haven't called
it with scoring on for today's universe.

**No code change.** This is a one-shot data refresh.

**Action:** run `trading ingest-news --date 2026-05-15` (default scoring on)
against the current alias map. Verify post-run that `sentiment_daily` for
2026-05-15 has rows for every symbol in `data/parquet/nifty200/` plus current
Kite holdings.

**Acceptance:** re-running the Phase 12 smoke (`trading brief assemble-context`
+ inspect `_context.md`) shows real `sentiment 7d {value}` lines for the
candidate symbols instead of `sentiment: _(no daily aggregate)_`.

**Out of scope:** widening the alias map to attribute more RSS headlines
to symbols. That's a Phase 13 prep item if the smoke shows the alias map
isn't covering the universe well.

### 12.5.4 — Make sector_commentary optional

**Decision:** sector_daily isn't built yet (no module computes NSE sectoral
relative strength). Building it properly needs ~2-4 hrs and a real spec
(Phase 12.6). For now, downgrade `sector_commentary.md` from required to
optional so `compile_brief` doesn't raise and the analyst skill knows it
can skip.

**Fix:**
- `src/trading/llm/briefing.py`: split `expected_parts(mode, candidate_symbols)`
  into two functions: `required_parts(mode, candidate_symbols)` (returns the
  list `compile_brief` raises on — `macro_brief.md`, `candidates/{SYMBOL}.md`
  for each symbol, plus `post_close_recap.md` for post_close) and
  `optional_parts(mode)` (returns `["sector_commentary.md"]`).
  Remove the public `expected_parts` function — it has no production callers
  outside the tests.
- `compile_brief`: only raise `MissingNarrativeError` for absent required
  parts. For optional parts that are absent, the `## Sector commentary`
  header stays in `brief.md` but the body is replaced with the literal
  placeholder string `_(sector commentary not yet wired — see Phase 12.6)_`
  on its own line. The placeholder is hardcoded in `compile_brief` since
  there's exactly one optional file in v1.
- `.claude/skills/analyst/SKILL.md`: change the "Outputs" table to mark
  `sector_commentary.md` as optional and tell the analyst they may skip it
  while sector_daily is unwired (Phase 12.6).

**Tests:** `tests/test_llm_briefing.py`:
- Replace `test_expected_parts_pre_open` and `test_expected_parts_post_close`
  with parallel tests against `required_parts` and `optional_parts`.
- Add `test_compile_brief_substitutes_placeholder_when_sector_missing`
  (no sector_commentary.md → succeeds, brief.md contains the placeholder
  string under the `## Sector commentary` header).
- Re-record the existing `test_compile_brief_pre_open_happy_path_snapshot`
  and `test_compile_brief_post_close_includes_recap` snapshots — both still
  write sector_commentary.md so output is unchanged in shape, but the
  snapshot strings were captured against the old code path and may need
  re-recording defensively after the function rename.

### 12.5.5 — Smoke + PROGRESS.md + commit + push

**Action:** re-run `trading brief assemble-context --date 2026-05-15 --mode pre_open`,
inspect `_context.md` to confirm:
- No `close nan` rows in candidates section
- No future-dated headlines in any "Recent headlines" block
- Real `sentiment 7d {value}` for symbols with sentiment_daily rows

Update PROGRESS.md:
- Insert "Phase 12.5 — Data quality cleanup" block (5 sub-tasks marked
  `[x]`) between Phase 12 and Phase 13 in the body.
- Add row `| 12.5 | Data quality cleanup | [x] |` and `| 12.6 | Sector data | [ ] |`
  to the status snapshot table.
- Update `Currently working on` / `Next up` pointers.

Commit `feat(data): Phase 12.5 quality fixes (parquet NaN, news ts, optional sector)`,
push to origin/main.

## 3. Dependencies between sub-tasks

- 12.5.1 and 12.5.2 are independent code changes.
- 12.5.4 is independent of 12.5.1 and 12.5.2 (different files).
- 12.5.3 is a runtime action — should run *after* 12.5.1 and 12.5.2 are
  in place so the smoke (12.5.5) reads against the cleaned-up code.
- 12.5.5 is last.

## 4. Out of scope (deferred to Phase 12.6 or Phase 13 prep)

- Routing NSE corporate events into `event_calendar` table (architectural
  fix for 12.5.2's source).
- Building `sector_daily` (Phase 12.6 — separate spec).
- Widening the news alias map to cover the full Nifty 50 universe.
- Backfilling `sentiment_daily` for historical dates (only today is
  needed for Phase 13 prep).
