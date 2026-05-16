# Phase 14.C — Pre-open IEP Gap Filter Design

**Date:** 2026-05-16  
**Phase:** 14.C  
**Status:** Spec (awaiting implementation plan)

---

## Overview

Phase 14.C introduces a pre-market candidate re-ranking job that runs at 08:55 — five minutes before market open — to filter and reorder the candidate list from Phase 13 (pre_open) based on overnight Index Entry Price (IEP) gaps and sector momentum alignment.

**Business logic:** Candidates that gapped in alignment with today's regime (up gaps when market rallying, down gaps when market declining) and whose sectors are leading the move are ranked higher; candidates that gapped against the regime or in lagging sectors are removed from the brief. This surfaces the highest-conviction setups to the analyst at market open.

**Timing:** 08:55 (5 minutes before 09:00 market open)

**Integration:** Reads Phase 13's `_context.md`, updates it in-place with filtered + reordered candidates, then feeds to `/analyst` skill for narrative generation.

---

## Success Criteria

1. **Filtering:** Anti-regime candidates (gap direction opposite to regime, or gap in lagging sector) are removed from the brief.
2. **Ranking:** Remaining candidates reordered by composite score: gap_pct (60% weight) + sector_momentum_percentile (40% weight).
3. **Preservation:** All metadata (candidate rules passed, parquet history reference) preserved in `_context.md`; only symbol order and metadata annotations change.
4. **Graceful degradation:** If overnight quotes missing, filter by sector only; if sector data missing, filter by gap + regime only; if regime missing, assume NEUTRAL (no directional filter).
5. **Idempotency:** Running 14.C multiple times on the same date with same input data produces identical output.
6. **Test coverage:** >90% of pre_open_iep.py logic covered; 100% on core algorithm (filter + rank).

---

## Architecture & Integration

### Timing & Inputs

- **Runtime:** 08:55 (after pre_open has written bundle; before analyst skill manual invocation)
- **Inputs:**
  - `data/research/<as_of>/_context.md` — candidate list from Phase 13 (pre_open)
  - `data/raw/<as_of>/quotes_HHMM.json` — overnight Kite snapshots (LTP as of ~08:50 capture time)
  - Parquet OHLCV files — yesterday's close prices for each candidate
  - Regime classification — from Phase 9 (freshly computed by pre_open)
  - Sector momentum data — NSE sectoral indices or derived from holdings

- **Outputs:**
  - Updated `data/research/<as_of>/_context.md` — reordered candidates section + gap/sector metadata
  - Stdout summary: input/filtered/removed counts, list of removed symbols with reasons

### Pipeline Position

```
Phase 13: pre_open
  ↓
  writes _context.md + candidates/{SYM}.md
  ↓
Phase 14.C: pre_open_iep (08:55)
  ↓
  reads _context.md
  filters + reorders by gap + sector momentum
  writes updated _context.md
  ↓
/analyst skill (manual, ~09:00)
  ↓
  reads filtered _context.md
  writes macro_brief.md, candidates/{SYM}.md, etc.
  ↓
Phase 12.3: compile_brief
  ↓
  concatenates into brief.md (candidates in filtered order)
```

### Design Choice: In-Place Context Update

14.C modifies `_context.md` **in place** rather than producing a separate output file. This ensures:
- Single source of truth for the candidate list (no duplicate logic in `/analyst` skill)
- Analyst skill automatically sees filtered + reordered candidates
- No additional glue code needed in compile_brief or CLI

The update is non-destructive: `_context.md` grows with annotations (gap/sector metadata in candidate headings); removed candidates are listed with explanations at the end.

---

## Core Algorithm

### Gap Calculation

```
gap_pct = ((ltp - yesterday_close) / yesterday_close) × 100
```

Example: If LTP = 305 and yesterday's close = 300, gap_pct = +1.67%.

### Filter 1: Regime Alignment

| Regime | Keep candidates with | Remove if |
|---|---|---|
| RISK_ON | gap_pct ≥ 0% | gap_pct < 0% (down gap) |
| RISK_OFF | gap_pct ≤ 0% | gap_pct > 0% (up gap) |
| NEUTRAL | all | (no directional filter) |

**Rationale:** Gaps in the direction of the trend are more likely to persist; anti-trend gaps often fill quickly (low-probability entries).

### Filter 2: Sector Momentum

1. Compute sector momentum for each of the candidate's sector:
   ```
   sector_momentum_pct = ((sector_idx_today_open - sector_idx_yesterday_close) 
                           / sector_idx_yesterday_close) × 100
   ```

2. Determine sector status:
   - If RISK_ON regime: keep candidate if sector_momentum_pct ≥ 0 (sector not lagging). Remove if sector is losing ground.
   - If RISK_OFF regime: keep candidate if sector_momentum_pct ≤ 0 (sector not rallying). Remove if sector is rallying into weakness.
   - If NEUTRAL regime: no sector-based removal; rerank by sector strength.

**Rationale:** Candidates in leading sectors have stronger fills and higher probability of holding the setup through market open.

### Re-Ranking: Composite Score

For candidates that pass both filters:

```
score = (gap_pct_normalized × 0.6) + (sector_momentum_percentile × 0.4)

where:
  gap_pct_normalized = gap_pct / max(|gap_pcts in filtered set|)
    → normalized to [0, 1] range relative to largest gap in the filtered set
  
  sector_momentum_percentile = percentile rank of candidate's sector_momentum 
                               within all sectors represented in filtered set
    → e.g., if candidate's sector is top 2 of 5 sectors, percentile ≈ 80%
```

**Weights:** 60% gap (size of the move) + 40% sector (leadership in today's move).

**Example:**
```
Regime: RISK_ON
Filtered candidates: RVNL (+2.5%, PSU/Infra +1.8%), NTPC (+1.2%, Power +0.9%)

gap_pct_max = 2.5
sector percentiles: PSU/Infra 100%, Power 50%

RVNL: (2.5/2.5 × 0.6) + (100% × 0.4) = 0.60 + 0.40 = 1.00
NTPC: (1.2/2.5 × 0.6) + (50% × 0.4)  = 0.29 + 0.20 = 0.49

Output order: RVNL (score 1.00), NTPC (score 0.49)
```

### Removed Candidates List

Candidates that fail Filter 1 or 2 are recorded with reason and appended to `_context.md`:

```markdown
_(RELIANCE removed: gapped +0.8% with RISK_ON regime, but Energy sector lagging -0.3%)_
_(COALINDIA removed: gapped -1.2% against RISK_ON regime)_
```

---

## Implementation Structure

### Module Location & Organization

**File:** `src/trading/jobs/pre_open_iep.py`

**Dataclass:**
```python
@dataclass(frozen=True)
class PreOpenIepResult:
    as_of: date
    regime: Regime
    candidates_input: int        # from pre_open
    candidates_filtered: int     # after regime + sector filter
    candidates_removed: int      # gap/sector misaligned
    rerank_applied: bool
    context_path: Path | None    # updated _context.md path
    removed_symbols: list[str]   # symbols that were filtered out
    warnings: list[str]
```

**Main function:**
```python
def run_pre_open_iep(
    as_of: date,
    *,
    paths: Paths | None = None,
) -> PreOpenIepResult:
    """Filter + reorder pre_open's candidate list by gap + sector momentum.
    
    Reads from:
      - data/research/<as_of>/_context.md (candidate list)
      - data/raw/<as_of>/quotes_HHMM.json (overnight quotes)
      - parquet OHLCV (yesterday's closes)
      - regime (from Phase 9 macro snapshot)
    
    Writes:
      - data/research/<as_of>/_context.md (updated with reordering + metadata)
    
    Returns PreOpenIepResult with counts and warnings.
    Raises PreOpenIepAborted if context missing or malformed.
    """
```

**Private helpers:**
- `_parse_candidates_from_context(context_md: str) -> list[tuple[str, int]]` — extract (symbol, rules_passed) from `_context.md` header lines
- `_compute_gaps(symbols, quotes, yesterday_closes) -> dict[str, float]` — gap_pct for each symbol
- `_load_sector_momentum(regime) -> dict[str, float]` — sector_momentum_pct for each sector represented in candidates
- `_score_sectors_percentile(candidates, sector_momentum) -> dict[str, float]` — percentile rank for each candidate's sector
- `_filter_and_rank(candidates, gaps, sector_scores, regime) -> tuple[list[str], list[str]]` — returns (kept_symbols, removed_symbols) sorted by score
- `_update_context_markdown(context_md, kept_order, removed_symbols) -> str` — return updated markdown with reordered candidates section

**Execution mode:** Single-phase (no prepare/apply split like 14.A/14.B)
- Why: Pre_open has already written the bundle; 14.C just reorders it at a fixed time.

**Exception:**
```python
class PreOpenIepAborted(RuntimeError):
    """Raised when run_pre_open_iep cannot proceed (missing context, etc.)."""
```

### CLI Interface

```bash
trading pre-open-iep --date 2026-05-16 [--dry-run]
```

- `--date`: ISO date string (YYYY-MM-DD)
- `--dry-run` (optional): Print the reordering plan without modifying `_context.md`

**Stdout output:**
```
Regime: RISK_ON
Candidates input: 5
Candidates filtered: 3 (gap + sector alignment OK)
Candidates removed: 2
Removed: RELIANCE (gap +0.8%, sector lagging), COALINDIA (gap -1.2%, anti-regime)
Updated: data/research/2026-05-16/_context.md
Ready for /analyst skill
```

**Exit codes:**
- 0: Success
- 2: Hard stop (missing context, malformed context)

### Windows Launcher

**File:** `scripts/pre_open_iep.bat`
```batch
@echo off
REM Phase 14.C pre-open IEP gap filter.
REM Usage: pre_open_iep.bat YYYY-MM-DD
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: pre_open_iep.bat YYYY-MM-DD & exit /b 2)
uv run python -m trading.jobs.pre_open_iep %1
```

---

## Integration Points

### With `/analyst` Skill

1. Pre_open writes `_context.md` with candidate list (scanner order)
2. User (or Phase 17 scheduler) runs `trading pre-open-iep --date YYYY-MM-DD` at 08:55
3. 14.C updates `_context.md`: reorders candidates, adds gap/sector annotations, lists removed candidates
4. User invokes `/analyst` skill (manual at ~09:00, or automated in Phase 17)
5. Analyst reads the already-filtered & reordered `_context.md`, writes narratives in that order
6. `compile_brief` respects the ordering

### Context Markdown Transformation

**Before (pre_open output):**
```markdown
# Trading context bundle — 2026-05-16 (mode: pre_open)

## Today's candidates

### RELIANCE — passes 8/10 rules

### RVNL — passes 9/10 rules

### NTPC — passes 7/10 rules

### COALINDIA — passes 6/10 rules

### TATAPOWER — passes 5/10 rules
```

**After (14.C update, RISK_ON regime):**
```markdown
# Trading context bundle — 2026-05-16 (mode: pre_open)

## Today's candidates

### RVNL — passes 9/10 rules | gap +2.5% (sector leading PSU/Infra +1.8%)

### TATAPOWER — passes 5/10 rules | gap +1.8% (sector leading Power +0.9%)

### NTPC — passes 7/10 rules | gap +1.2% (sector leading Power +0.9%)

_(RELIANCE removed: gapped +0.8% with RISK_ON regime, but sector Energy lagging -0.3%)_
_(COALINDIA removed: gapped -1.2% against RISK_ON regime)_
```

Analyst narratives are still written to `candidates/RVNL.md`, `candidates/TATAPOWER.md`, etc. (files not deleted), but the `_context.md` index reflects the new order.

---

## Error Handling & Edge Cases

### Hard Stops (exit code 2)

| Condition | Message | Remediation |
|---|---|---|
| `_context.md` missing | "Context bundle missing. Run `trading pre-open YYYY-MM-DD` first." | Run pre_open |
| `_context.md` stale (>12h old) | "Context bundle is stale (date mismatch). Re-run `trading pre-open YYYY-MM-DD`." | Re-run pre_open |
| `_context.md` malformed (no candidate list parseable) | "Cannot parse candidate list from context. Context file corrupted or format changed." | Manual fix or re-run pre_open |
| No passing candidates in context | Return early with `candidates_input=0, candidates_filtered=0, context_path=<unchanged>` | (not an error; quiet day) |

### Graceful Degradations (warnings, continue)

| Condition | Fallback | Warning Message |
|---|---|---|
| Overnight quotes (`quotes_HHMM.json`) missing | Filter by sector momentum only; skip gap-based ranking | "Overnight quotes missing; filtering by sector momentum only." |
| Sector data unavailable (yfinance down, nsepython timeout) | Filter by gap direction + regime only; skip sector ranking | "Sector data unavailable; filtering by gap direction + regime only." |
| Candidate symbol not in parquet | Skip that candidate from ranking (assume no history available) | "Symbol {SYM} not in parquet; skipping from re-rank." |
| Regime not yet classified (macro snapshot missing) | Assume NEUTRAL regime (no directional filter; reorder by gap + sector magnitude only) | "Regime not yet classified; using NEUTRAL (no directional filter)." |
| Sector momentum is exactly 0% | Treat as "neutral" — keep if regime allows, rerank as 50th percentile | (no warning; edge case handled) |

### Edge Cases

1. **All candidates filtered out:** If regime is RISK_ON but all candidates gapped down or into lagging sectors → `candidates_filtered=0`. `_context.md` updated with empty candidate list + explanatory comments. Brief renders with 0 candidates (acceptable on rare days).

2. **Only 1 candidate remains:** Reranking still applied (for consistency), but no reordering occurs. Output notes: "1 candidate passed filter."

3. **Ties in rerank score:** Use stable sort — preserve pre-filter order for candidates with identical scores.

4. **Negative gap scenario (RISK_OFF regime, all down gaps):** Rerank by gap magnitude (larger down gaps score higher in a down regime).

5. **Idempotency:** Running 14.C twice on the same date with same input data produces identical output. If new quote snapshot arrives (new `quotes_HHMM.json`), re-run will re-parse and potentially produce different ordering (acceptable; always use freshest data).

---

## Testing Strategy

### Unit Tests (~15 tests)

**Gap calculation:**
- `test_compute_gaps_basic` — formula verification
- `test_compute_gaps_missing_quotes` — graceful handling
- `test_compute_gaps_zero_gap` — edge case

**Regime filter:**
- `test_filter_risk_on_keeps_positive_gaps`
- `test_filter_risk_on_removes_negative_gaps`
- `test_filter_risk_off_keeps_negative_gaps`
- `test_filter_neutral_keeps_all`
- `test_filter_edge_case_zero_gap`

**Sector momentum filter:**
- `test_sector_filter_removes_lagging_in_risk_on`
- `test_sector_filter_keeps_leading`
- `test_sector_percentile_ranking`

**Reranking:**
- `test_rerank_score_formula`
- `test_rerank_ordering_descending`
- `test_rerank_ties_stable_sort`

**Context parsing:**
- `test_parse_candidates_from_context`
- `test_update_context_markdown_preserves_sections`
- `test_update_context_adds_metadata_annotations`

### Integration Tests (~5 tests)

- `test_pre_open_iep_happy_path_risk_on` — realistic 5-candidate scenario with regime RISK_ON
- `test_pre_open_iep_quiet_day_all_filtered` — all candidates anti-regime
- `test_pre_open_iep_idempotent_on_rerun` — same input → same output
- `test_pre_open_iep_missing_context_raises` — hard stop tested
- `test_pre_open_iep_missing_quotes_degrades` — graceful degradation tested

### Test Fixtures

- `_QUOTE_SNAPSHOT_RISK_ON` — realistic overnight moves (RVNL +2.5%, RELIANCE +0.8%, NTPC +1.2%, COALINDIA -0.5%, TATAPOWER +1.8%)
- `_SECTOR_MOMENTUM_DATA` — sector indices for PSU/Infra, Energy, Power, etc.
- `_CONTEXT_5_CANDIDATES` — pre_open output with 5 passing candidates
- Reuse conftest's `_seed_ohlcv` for yesterday's closes

### Coverage Goal

- >90% of `pre_open_iep.py` logic covered
- 100% of core algorithm (gap, filter, rank)

**Test file:** `tests/test_jobs_pre_open_iep.py` (~20 tests total)

---

## Out of Scope

1. **Sector data ingestion:** Phase 12.6 (deferred) will wire real sector indices into `assemble_context`. For 14.C MVP, sector momentum derived from Kite holdings' sector performance (proxy).
2. **Real-time market data:** 14.C uses EOD closes from parquet + overnight LTP from quotes snapshot. Intraday mid-quotes not needed.
3. **Analyst overrides:** No user-facing knobs to manually adjust filter thresholds or override removals. MVP is hard-coded gap + sector logic (can extend in Phase 16+ with learned model).
4. **Dry-run execution logging:** `--dry-run` flag prints the plan but does not write logs to disk. Actual run writes to `data/logs/` only if Phase 17 logging is enabled.

---

## Success Metrics (Phase 18+ measurement)

Once live paper-trading begins:
- **Filter quality:** Measure removal accuracy (% of removed candidates that would have failed anyway on close)
- **Rank quality:** Measure correlation between re-rank order and daily P&L of trades opened in that order
- **Degradation impact:** When overnight quotes or sector data missing, track hit-rate vs full-data runs
- **Idempotency:** Verify re-runs on same date produce identical output (regression test)

---

## Appendix: Example Walkthrough

**Date:** 2026-05-16 (hypothetical, RISK_ON regime)

**Inputs:**
- Pre_open found 5 candidates: RELIANCE, RVNL, NTPC, COALINDIA, TATAPOWER
- Overnight quotes: RELIANCE LTP 2805, RVNL LTP 312, NTPC LTP 176, COALINDIA LTP 159, TATAPOWER LTP 147
- Yesterday's closes: RELIANCE 2789, RVNL 304, NTPC 174, COALINDIA 160, TATAPOWER 144
- Sector momentum: PSU/Infra +1.8%, Energy -0.3%, Power +0.9%
- Regime: RISK_ON (indices rally +1.5%)

**Calculation:**

| Symbol | Sector | Yesterday Close | LTP | Gap % | Sector Mom | Filter 1 (Gap) | Filter 2 (Sector) | Pass? |
|---|---|---|---|---|---|---|---|---|
| RVNL | PSU/Infra | 304 | 312 | +2.63% | +1.8% | ✓ | ✓ | YES |
| TATAPOWER | Power | 144 | 147 | +2.08% | +0.9% | ✓ | ✓ | YES |
| NTPC | Power | 174 | 176 | +1.15% | +0.9% | ✓ | ✓ | YES |
| RELIANCE | Energy | 2789 | 2805 | +0.57% | -0.3% | ✓ | ✗ | NO (sector lagging) |
| COALINDIA | PSU/Infra | 160 | 159 | -0.63% | +1.8% | ✗ | — | NO (gap against regime) |

**Re-ranking (filtered set: RVNL, TATAPOWER, NTPC):**

```
gap_max = 2.63%
sector_percentiles: PSU/Infra (2 symbols: RVNL, COALINDIA) = 100%
                    Power (2 symbols: NTPC, TATAPOWER) = 50%

RVNL: (2.63/2.63 × 0.6) + (100% × 0.4) = 0.60 + 0.40 = 1.00
TATAPOWER: (2.08/2.63 × 0.6) + (50% × 0.4) = 0.47 + 0.20 = 0.67
NTPC: (1.15/2.63 × 0.6) + (50% × 0.4) = 0.26 + 0.20 = 0.46
```

**Output (candidates reordered, metadata added):**
1. RVNL (score 1.00) — gap +2.63%, sector leading
2. TATAPOWER (score 0.67) — gap +2.08%, sector neutral
3. NTPC (score 0.46) — gap +1.15%, sector neutral
- RELIANCE removed (sector Energy lagging RISK_ON regime)
- COALINDIA removed (gap -0.63% against RISK_ON regime)

---

## Next Steps

1. User reviews this spec for accuracy and completeness
2. Invoke `superpowers:writing-plans` skill to create implementation plan
3. Execute plan: write tests first (TDD), then implementation, commit per sub-task
4. Real-data smoke test with actual Kite quotes + regime classification
5. Update PROGRESS.md and push to origin/main
