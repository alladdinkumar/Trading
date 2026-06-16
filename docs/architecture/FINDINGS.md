# Architecture Review — Findings Log

> Running log of **vulnerabilities, gaps, inaccuracies, missing tests/fixtures,
> and tech debt** discovered while writing the [`docs/architecture/`](./PROGRESS.md)
> design set. The layer docs describe *what the code does today*; anything that
> *should change* lands here instead, so it can be triaged and fixed in a
> dedicated pass — after which we revisit and update the docs.

## Executive summary

The architecture review (docs 00–08) produced **31 active findings** (1 earlier
finding superseded). **2 are now fixed** (F-014, F-012 — Nifty-50 ingest,
shipped 2026-06-16), leaving **29 open**. The system is **well-engineered at the
seams** — graceful degradation, idempotency, pure-function cores, clean
job/CLI/UI layers — but two themes undermine its current goal of proving itself
in a live paper-trade run:

1. **Data coverage.** ~~It trades **12 stocks, not the intended Nifty 50**~~
   (✅ fixed — now scans all 50 Nifty constituents) and ~~never refreshes
   prices~~ (✅ fixed — F-018: `pre_open` refreshes OHLCV + a staleness guard
   skips stale symbols). News attribution still covers only 12 symbols (F-015),
   so the sentiment signals remain partly starved.
2. **Measurement integrity.** Four of ten risk rules are silently disabled, every
   prediction is a constant +20%, and the paper **equity curve never compounds
   realised P&L** — which is exactly the metric the Phase 18.5 go/no-go gate
   ("OOS Sharpe > 1.0") depends on. The system cannot currently measure whether
   it works.

**Bottom line:** the build is solid; the gaps are in *what data flows through it*
and *how its results are measured*. Both are fixable with localized changes.

### Breakdown

| Severity | Count | IDs |
|---|---:|---|
| **High** | 4 | F-002, F-005†, F-019, F-023 |
| Med | 16 | F-001, F-003, F-006, F-007, F-008, F-010, F-015, F-016, F-020, F-021, F-022, F-024, F-025, F-026, F-029, F-032 |
| Low | 8 | F-004, F-009, F-013, F-017, F-027, F-028, F-030, F-031 |
| ✅ Fixed | 3 | F-012, F-014, F-018 |

† F-005 (real-money execution / kill-switch) is `Needs decision`, gated to a
future Phase 19 — out of scope for hardening the paper run.

| Category | Count |
|---|---:|
| VULN (correctness/data-integrity) | 5 (F-019, F-022, F-023, F-024, F-029) |
| GAP (missing functionality/guardrail) | 11 |
| INACC (code ≠ spec/docstring) | 7 |
| DEBT (cleanup) | 8 |

## Remediation roadmap

Recommended order. Each wave is independently shippable; Wave 1 is the minimum to
make the Nifty-50 paper run both *real* and *measurable*.

### Wave 1 — Make the live run real & measurable (do first)
The highest-leverage cluster; everything else is noise until these land.

| ID | Sev | Fix | Why first |
|---|---|---|---|
| ~~**F-014 + F-012**~~ ✅ | High/Med | **Done (2026-06-16).** Ingested OHLCV for all 50 Nifty constituents + 8 holdings; pinned `nifty50.txt` (candidate set) and rebuilt `universe.txt` (ingest set); added `load_candidate_universe()`; `_step_scan` now scans the 50; aligned `sector_map.csv`. Verified: `pre-open` `candidates_total` 12→50. *(`nifty200/` subdir rename deferred — cosmetic.)* | Unblocks the real universe **and** gives the ranker enough labels to ever promote |
| ~~**F-018**~~ ✅ | High | **Done (2026-06-16).** New `data/ohlcv_refresh.py` (`refresh_ohlcv` + `cross_check_closes`); `pre_open._step_ohlcv` runs before the scan; `scan()` skips symbols whose last bar is >5 days stale (warns); `Candidate.bar_date` rendered in the brief; new `trading refresh-ohlcv` CLI | Prevents the scan running on stale prices |
| **F-019** | High | Populate `ScanContext` in `_step_scan` from data already fetched (regime, `has_critical`; ban-list/T2T as available) | Re-enables 3 risk vetoes + the regime gate |
| **F-023** | High | Paper-cash ledger: debit on open, credit net P&L on close; equity = cash + open MTM | Makes the Phase-18.5 Sharpe metric trustworthy |
| **F-029** | Med | Derive `predicted_return_pct` + `signal.target` from the real target/ranker, not a constant +20% | Makes calibration meaningful |

### Wave 2 — Correctness & accounting hygiene
| ID | Sev | Fix |
|---|---|---|
| F-024 | Med | Compute `days_held` from `ts_entry` (not per-MTM-call) |
| F-025 | Med | Apply slippage + charges in paper MTM (with the F-023 ledger) |
| F-022 | Med | Wire fundamentals + sentiment into health; implement vote-count threshold scaling |
| F-015 | Med | Alias map for all 50 names |
| F-016 | Med | DB-level news dedup (unique index / insert-or-ignore) |
| F-002 | High | Validate broker/quote JSON at the read boundary |
| F-021 | Med | Add buy-side charges to backtest `total_costs` |

### Wave 3 — Robustness & ops
F-003 (half-run/run-state detection), F-032 (run broker-free steps unattended),
F-013 (retention policy), F-031 (train/serve skew), F-026 (narrative validation),
F-010 (decide each dormant table: implement or mark reserved).

### Wave 4 — Structural & cleanup (low-risk, alongside)
F-001 (prune unused deps), F-004 (canonical IST clock), F-006/F-007/F-008/F-009
(layering: domain module, fetch/classify split, break cycle, import-linter),
F-017 (macro column labels), F-020 (regime name collision), F-027 (heading
constant + spanning test), F-028 (docstring), F-030 (guard visibility signals).

### Separate track — needs a decision
F-005: real-money execution path + kill-switch/risk-halt. Gated behind the Phase
18.5 outcome; needs its own spec before any code.

> **Suggested first PR:** ~~F-014 + F-012 (Nifty-50 ingest).~~ ✅ **Shipped
> 2026-06-16.** F-018 (OHLCV freshness guard) also shipped 2026-06-16. Next
> up in Wave 1: F-019 (ScanContext wiring), F-023 (paper-cash ledger), F-029
> (real predictions).

## How to use this file

- Each finding gets a stable ID (`F-NNN`) so docs and commits can reference it.
- Add findings as they're discovered, per phase. Don't fix inline during doc
  writing — capture here, keep moving.
- When fixed: set **Status → Fixed**, note the commit, and update any doc the
  fix invalidates.

### Categories

| Tag | Meaning |
|---|---|
| `VULN` | Security / correctness vulnerability or data-integrity risk |
| `GAP` | Missing functionality, validation, or guardrail |
| `INACC` | Code disagrees with a spec, comment, docstring, or manifest |
| `TEST` | Missing test coverage or a fixture needed to test safely |
| `DEBT` | Cleanup / maintainability / clarity |

### Severity

`High` (can cause wrong trades, data loss, or silent failure) ·
`Med` (degrades robustness or misleads) · `Low` (cosmetic / nice-to-have)

### Status

`Open` · `In progress` · `Fixed` · `Wontfix` (with reason) · `Needs decision`

---

## Findings

| ID | Cat | Sev | Phase | Title | Status |
|---|---|---|---|---|---|
| F-001 | INACC | Med | 0 | `vectorbt` + `anthropic` declared deps but unused in production paths | Open |
| F-002 | GAP | High | 0 | No schema validation on broker/quote JSON contract between `/kite-*` skills and `data/*_snapshot.py` | Open |
| F-003 | GAP | Med | 0 | Daily flow is ~13 manually-sequenced commands with no half-run/missed-step detection | Open |
| F-004 | DEBT | Low | 0 | Each job re-derives "today" in IST independently; no single canonical clock | Open |
| F-005 | GAP | High | 0 | No real-money execution path, kill-switch, or risk-halt design (gated to future Phase 19, tracked here so it isn't forgotten) | Needs decision |
| F-006 | DEBT | Med | 1 | Domain DTOs live in `data/` so `store` depends "up" into the ingestion layer | Open |
| F-007 | DEBT | Med | 1 | `data.macro.snapshot_and_classify` puts a decision concern (regime classify) in the data layer (upward back-edge into `features`) | Open |
| F-008 | DEBT | Med | 1 | `strategy ⇄ backtest` package-level import cycle, broken only by lazy/TYPE_CHECKING imports | Open |
| F-009 | GAP | Low | 1 | No automated dependency-layering enforcement (e.g. import-linter); layering is convention-only | Open |
| F-010 | GAP | Med | 2 | 8 of 16 SQLite domain tables are defined but have zero writers (dormant schema reservations) | Open |
| F-011 | VULN | High | 2 | Rule gates depend on empty tables — **superseded by F-019** (root cause is unpopulated `ScanContext`, not the tables) | Superseded |
| F-012 | INACC | Med | 2 | Universe scope: paper-trading candidate set should be **Nifty 50 (50 stocks)** per user req; currently ~57 (Nifty 50 + holdings) under a `nifty200/` subdir | ✅ Fixed (2026-06-16) — candidate set pinned to Nifty 50; subdir rename deferred (cosmetic) |
| F-013 | GAP | Low | 2 | No retention/compaction policy for `news_items` (append-only) or `data/raw/<date>/` JSON — unbounded growth | Open |
| F-014 | GAP | High | 3 | Only 12 symbols have parquet OHLCV on disk → live candidate universe is 12, not the configured ~57 nor the required Nifty 50 | ✅ Fixed (2026-06-16) — all 50 Nifty + 8 holdings ingested; `candidates_total` 12→50 |
| F-015 | GAP | Med | 3 | News symbol-attribution alias map covers only 12 symbols → sparse `sentiment_daily`, near-empty per-symbol sentiment/critical inputs | Open |
| F-016 | DEBT | Med | 3 | News dedup is URL-only and single-run; daily event/headline re-fetch creates duplicate `news_items` rows (no DB-level uniqueness) | Open |
| F-017 | INACC | Low | 3 | `macro_snapshot.dow_fut`/`nasdaq_fut` store spot index closes not futures; `sgx_nifty` always NULL | Open |
| F-018 | GAP | High | 3 | No automated daily OHLCV refresh and no read-time freshness guard; scan can silently run on stale parquet. Quote staleness also assumes host clock == IST | ✅ Fixed (2026-06-16) — refresh step + scan staleness guard + Kite close cross-check; IST-clock centralisation ([[F-004]]) still open |
| F-019 | VULN | High | 4 | 4 of 10 Layer-A rules (regime, fno_banned, t2t, critical_event) are unconditional passes — `pre_open`/`scan` build `ScanContext` with all defaults; risk vetoes + regime gate are dead despite the data being available | Open |
| F-020 | INACC | Med | 4 | Two different "regime" concepts share the name: `features.regime` 4-axis voter (feeds sizing) vs Layer-A `passes_regime` rule (VIX<25/dd gate, unused) — different thresholds/inputs | Open |
| F-021 | INACC | Med | 5 | `BacktestResult.total_costs` omits buy-side charges (only sell-side accumulated); aggregate cost-drag understated (per-trade `costs_paid` is correct) | Open |
| F-022 | VULN | Med | 5 | Health scorer structurally TRIM-biased: fundamentals AND sentiment never wired (pre_open/monthly_sip pass empty snapshots) → technicals-only + critical-news EXIT veto dead; docstring claims vote-count scaling not implemented (fixed ±3) | Open |
| F-023 | VULN | High | 5 | Paper equity curve never compounds realised P&L — cash is a constant; closing a winner drops its gain from `portfolio_snapshots.equity`. Equity/drawdown are not a true track record | Open |
| F-024 | VULN | Med | 5 | `days_held` bumped per MTM call, so mid-day + post-close double-count → 25-day time stop fires at ~12 calendar days | Open |
| F-025 | INACC | Med | 5 | Cost asymmetry: backtest applies full Zerodha costs but live paper MTM applies none (raw-price fills) → paper results flatter than backtest | Open |
| F-026 | GAP | Med | 6 | Analyst narrative is unvalidated against the bundle — "evidence-first" is an LLM instruction, not a code check; wrong/invented numbers in brief.md pass through. Refuse-stale (12h) is also advisory-only | Open |
| F-027 | DEBT | Low | 6 | Brittle 3-way coupling on the `### SYM — passes N/M rules` heading (context renderer / pre_open_iep rewrite / briefing regex), no spanning test | Open |
| F-028 | INACC | Low | 6 | `assemble_context` docstring omits the sector + Layer-B ranker sections (added Phases 12.6/16) | Open |
| F-029 | VULN | Med | 7 | `pre_open._step_auto_open` hardcodes `predicted_return_pct=20.0` + target=+20% for every signal → prediction calibration is a single meaningless bucket; signal.target disagrees with exit engine's min(+20%,2.5R) | Open |
| F-030 | DEBT | Low | 7 | Visibility-only (non-selected) signals inserted unconditionally each pre_open run → duplicate `signals` rows on re-run | Open |
| F-031 | INACC | Low | 7 | Train/serve skew: `negative_news_count_7d` empty during weekly retrain (`negative_news_lookup={}`) but populated at inference | Open |
| F-032 | GAP | Med | 8 | Daily pipeline is human-run reminders only (sole unattended job is weekly_train); a missed day = no snapshot/bundle/MTM, open trades unmanaged, track-record holes | Open |

---

## Detail

### F-001 — Unused declared dependencies (`INACC`, Med, Phase 0)
`pyproject.toml` declares `vectorbt>=0.26` and `anthropic>=0.40`, but the
backtester is a hand-rolled event loop (Phase 7 deviation) and the LLM runs via
Claude Code skills (Phase 12 deviation, no API credits). A reader could mistake
the manifest for the architecture.
- **Fix idea:** Remove both, or move to an `optional`/commented block with a
  one-line note pointing at the deviation specs. Confirm nothing imports them
  (`grep -r "import vectorbt\|import anthropic"`).
- **Doc to revisit after fix:** `00-overview.md` §3 tech-stack note.

### F-002 — No validation of the broker JSON contract (`GAP`, High, Phase 0)
`/kite-snapshot` and `/kite-quotes-snapshot` write JSON that `data/kite_snapshot.py`
and `data/quotes_snapshot.py` read back into dataclasses. The contract is
enforced only by the skill obeying its `SKILL.md`. A malformed write (wrong
`exchange`, missing required field, wrong types) may pass partially and corrupt
downstream logic (e.g. a wrong-exchange quote driving an MTM exit).
- **Fix idea:** Add a pydantic/dataclass validator at the read boundary that
  fails loudly with a clear remediation message; add `TEST` fixtures of malformed
  payloads.
- **Depends on:** F-006-range fixtures (to be added when Phase 3 is written).

### F-003 — No half-run detection in the daily flow (`GAP`, Med, Phase 0)
The operator runs the blocks manually; skipping IEP (or running blocks out of
order) silently leaves the bundle un-reranked or stale. Nothing reconciles
"which steps ran today".
- **Fix idea:** A lightweight per-date run-state file (or a `run_log` table)
  each step stamps, plus a `trading status --date` that reports gaps.

### F-004 — No canonical clock (`DEBT`, Low, Phase 0)
"Today in IST" is re-derived in several places (`ops/runner._today_ist`, jobs,
skills). Centralising would reduce drift and make freezegun-based tests simpler.

### F-005 — No execution / kill-switch design (`GAP`, High → Needs decision, Phase 0)
The system is paper-only. The Phase 18.5 gate (≥3 months OOS Sharpe > 1.0)
leads to a real-money Phase 19 that currently has no spec, no order-placement
path, and no risk-halt/kill-switch. Logged so the hardening review explicitly
decides scope and sequencing.

---

### F-006 — Domain types in `data/` couple `store` to ingestion (`DEBT`, Med, Phase 1)
`store/{ohlcv,macro_store,news_store,sector_store}.py` import their row types
from `data/{yfinance,macro,news,sector}.py`. Persistence depends on the fetcher
for "what the row is."
- **Fix idea:** Introduce `trading/domain.py` (or `types/`) holding the frozen
  DTOs; have both `data` and `store` import from there.
- **Doc to revisit:** `01-architecture.md` §3.1 + layer table.

### F-007 — Regime classification lives in the data layer (`DEBT`, Med, Phase 1)
`data.macro.snapshot_and_classify` lazily imports `features.regime` — the only
upward (ingestion→analysis) edge.
- **Fix idea:** Split into a pure `fetch` (in `data`) + `classify` (in
  `features` or the `pre_open` job). Keep `data` fetch-only.
- **Doc to revisit:** `01-architecture.md` §3.2; `03-data-layer.md` (Phase 3).

### F-008 — `strategy ⇄ backtest` cycle (`DEBT`, Med, Phase 1)
Mutual package dependency: `backtest.engine` runs `strategy.*`; `strategy.ranker*`
reuse `backtest.*` for labels/OOS scoring. Only lazy imports prevent an
import-time cycle.
- **Fix idea:** Extract the shared exit-replay/labeling logic into a small
  neutral module both depend on; or formally accept the cycle and document the
  import rules (never import across the seam at module top).
- **Doc to revisit:** `01-architecture.md` §3.3; Phase 4/5 docs.

### F-009 — No layering enforcement (`GAP`, Low, Phase 1)
The downward-dependency rule is convention, unenforced.
- **Fix idea:** Add `import-linter` contracts to CI mirroring the L0–L6 layers.

---

### F-010 — Dormant tables (`GAP`, Med, Phase 2)
`oi_daily`, `fno_ban_list`, `bulk_block_deals`, `corp_actions`, `account_events`,
`preopen_snapshot`, `live_quotes`, `event_calendar` are defined in schema v1 but
have no writer anywhere in `src/trading`.
- **Fix idea:** Either implement the fetchers/writers, or annotate them as
  reserved in the migration and the schema doc. Decide per-table.
- **Doc to revisit:** `02-data-schema.md` §4.2.

### F-011 — Strategy gates read empty tables (`VULN`, High, Phase 2)
`fno_ban_list` and `event_calendar` are empty (F-010) yet back the Layer-A gates
`passes_not_fno_banned` and `passes_no_critical_event`. If those gates read the
DB (or a context populated from it), an F&O-banned or event-risk stock is **not**
filtered out — a real correctness risk for live selection.
- **Verify in:** Phase 4 — confirm what the gates actually read (DB vs.
  `ScanContext` passed by the job).
- **Fix idea:** Populate the tables (ban list from NSE, events from the news/NSE
  calendar already fetched) and wire them into `ScanContext`; until then, make
  the gate's data-absence explicit (warn, not silently pass).

### F-012 — Universe scope vs. naming (`INACC`, Low→**Med**, Phase 2) — ✅ Fixed 2026-06-16
**Resolution:** Candidate set is now the Nifty 50, pinned in
`data/static/nifty50.txt` (50 symbols, sourced from the official
niftyindices.com constituents CSV). `universe.txt` is now the *ingest* list
(50 Nifty + 8 non-Nifty holdings for health scoring). A new
`load_candidate_universe()` reads `nifty50.txt` (falls back to `universe.txt`),
and `pre_open._step_scan` scans that candidate list — so holdings are scored for
health but never auto-traded. `sector_map.csv` aligned (added `TMPV`,
`MAXHEALTH`). The `nifty200/` parquet subdir rename is **deferred** (cosmetic;
touches `store/ohlcv.py` + tests; tracked separately). Verified by tests in
`tests/test_universe.py` + `test_jobs_pre_open.py`.

*Original finding:*
Parquet lives under `data/parquet/nifty200/` but the active set is ~57 symbols
(Nifty 50 + personal holdings).
- **User requirement (2026-06-16):** the **paper-trading candidate universe
  should be the Nifty 50 (50 stocks)** — not the ad-hoc 57, not Nifty 200.
  Holdings may still be scored for portfolio health, but candidate scanning /
  ranking / auto-open should operate over the Nifty 50.
- **Fix idea (fix pass):** Set `data/static/universe.txt` to the 50 Nifty 50
  constituents; align `sector_map.csv`; ensure `ingest-history`, `scan`, the
  ranker, and all daily jobs use that set; rename the `nifty200/` subdir to match
  reality (e.g. `nifty50/` or generic `ohlcv/`).
- **Severity bumped** Low→Med because it now reflects an explicit scope
  requirement, not just a naming nit.

### F-013 — No data retention policy (`GAP`, Low, Phase 2)
`news_items` and `data/raw/<date>/` grow without bound.
- **Fix idea:** Add a prune/compaction command (e.g. keep N days of raw JSON,
  archive/rollup old news).

---

### F-014 — Only 12 symbols ingested (`GAP`, High, Phase 3) — ✅ Fixed 2026-06-16
**Resolution:** Ran `trading ingest-history` over the full ingest list — all 50
Nifty-50 constituents plus the 8 non-Nifty holdings now have parquet OHLCV
(history from 2023-01-01). Reconciled membership to the live niftyindices.com
list (Zomato→ETERNAL rename; +INDIGO/MAXHEALTH/TMPV; JIOFIN promoted in;
BPCL/BRITANNIA/HEROMOTOCO/INDUSINDBK/LTIM/TATAMOTORS dropped). With
`_step_scan` now driving off the candidate list, `pre-open` `candidates_total`
went **12 → 50** (verified 2026-06-16). Unblocks [[F-012]] and gives the ranker
enough labelled symbols to train/promote.

*Original finding:*
`data/parquet/nifty200/` holds 12 `.parquet` files; `universe.txt` lists 60 and
`sector_map.csv` 57. The scanner iterates the parquet dir, so the candidate
universe is 12. Blocks the Nifty-50 paper-trade requirement ([[F-012]]).

### F-015 — Sentiment attribution covers 12 symbols (`GAP`, Med, Phase 3)
`news.DEFAULT_ALIASES` has 12 entries. Most Nifty-50 names' news is never
attributed → `sentiment_daily` sparse → sentiment + critical-news inputs empty
for most candidates (compounds [[F-011]]).
- **Fix idea:** Build an alias map for all 50 (ticker + common names); consider
  fuzzy/company-name matching from a maintained CSV.

### F-016 — Duplicate news rows across runs (`DEBT`, Med, Phase 3)
`fetch_all_news` dedups by URL within one call only; daily re-fetch of RSS + NSE
events re-inserts the same headlines/events. No DB uniqueness constraint.
- **Fix idea:** Unique index on `news_items(url)` (or `(date,url)`), or
  insert-or-ignore; dedupe NSE events by `(symbol, purpose, event_date)`.

### F-017 — Macro column labels misleading (`INACC`, Low, Phase 3)
`dow_fut`/`nasdaq_fut` hold spot index closes (`^DJI`/`^IXIC`); `sgx_nifty`
always NULL. Logic unaffected (regime uses values by key), naming misleads.
- **Fix idea:** Rename columns to `dow`/`nasdaq` (schema v3) or document inline.

### F-018 — No OHLCV freshness / refresh (`GAP`, High, Phase 3) — ✅ Fixed 2026-06-16
**Resolution (implements the approved Phase 12.7 spec):**
- New `src/trading/data/ohlcv_refresh.py`:
  - `refresh_ohlcv(paths, as_of, symbols=None)` pulls only the missing tail per
    symbol (incremental from the last bar; full 3y backfill when absent),
    excludes the forming `as_of` bar, dedupes (new wins), and isolates
    per-symbol errors. Returns a `RefreshResult` (refreshed/failed/bars_added +
    warnings).
  - `cross_check_closes(paths, as_of, holdings)` flags any holding whose parquet
    last close diverges >0.5% from the broker `close_price`.
- `pre_open._step_ohlcv` runs **before** `_step_scan`; `_step_cross_check` runs
  after the portfolio step. Both degrade to warnings. `PreOpenResult` gains
  `ohlcv_bars_added` (rendered in the CLI table).
- `strategy/rules.py`: `MAX_BAR_AGE_DAYS = 5` staleness guard in `scan()` —
  symbols whose last bar is older are skipped with a warning (scan gained a
  `warnings` accumulator). `Candidate.bar_date` records the data basis and is
  rendered on the candidate bullet (`close … (bar YYYY-MM-DD)`).
- Partial-bar hygiene moved into `data/yfinance.py::fetch_ohlcv` (drops NaN-OHLC
  rows) so the ingest path benefits too.
- New `trading refresh-ohlcv [--date] [--symbols]` CLI for manual runs.

The failure mode changed from "silent stale brief" to "visible degraded run".
*Still open:* centralising the IST clock for quote staleness ([[F-004]]).

*Original finding:*
History is refreshed only via manual `ingest-history`; no daily re-pull, no
read-time staleness check beyond the trailing-NaN drop. A skipped refresh means
the scan runs on stale prices with no signal. Quote staleness additionally
assumes the host clock is IST (naive `datetime.now()`).

---

### F-019 — Four Layer-A gates are no-ops in production (`VULN`, High, Phase 4)
`jobs/pre_open.py::_step_scan` and `cli.py::scan_cmd` both construct
`ScanContext(scan_date=...)` with every context field at its empty default.
Therefore rules 7–10 always pass:
- `regime` → "no macro data — passing by default" (despite macro snapshot being
  fetched in the same job);
- `not_fno_banned`, `not_t2t` → empty sets;
- `no_critical_event` → empty set, so the FinBERT critical veto never fires even
  though `is_critical`/`has_critical` are computed and stored.
Effect: only the 6 indicator rules filter; 3 risk vetoes + the regime gate are
dead. Supersedes [[F-011]].
- **Fix idea:** Populate `ScanContext` in `_step_scan` from data already on hand —
  `india_vix`/drawdown from the macro snapshot, `critical_event_symbols` from
  `sentiment_daily.has_critical` (and/or `news_items.is_critical`), `fno_ban_symbols`
  from an NSE ban-list fetch (F-010), `t2t_symbols` from a maintained list. Add a
  test asserting a banned/critical symbol is filtered.
- **Doc to revisit:** `04-analysis-strategy.md` §3.4; `07-jobs-workflows.md`.

### F-020 — "regime" name collision (`INACC`, Med, Phase 4)
`features.regime.classify_regime` (4-axis voter, VIX 14/20 thresholds, feeds
sizing multiplier) vs `strategy.rules.passes_regime` (gate: VIX<25 AND Nifty-200
5d dd>−5%). Same word, different logic; the rule one is also unused (F-019).
- **Fix idea:** Rename the Layer-A rule (e.g. `passes_market_filter`) and, when
  wiring F-019, decide whether the gate should reuse the macro voter's RISK_OFF
  classification instead of its own thresholds.

---

### F-021 — Backtest total_costs omits buy side (`INACC`, Med, Phase 5)
`engine._evaluate_exits` returns only sell-side charges; `run_backtest` adds just
that to `total_costs`. Buy-side charges (in `_OpenPosition.buy_costs_paid`) reach
`Trade.costs_paid` but not the aggregate.
- **Fix idea:** Return/accumulate buy charges from `_execute_pending`; or compute
  `total_costs = sum(t.costs_paid)` at the end. Optionally report slippage drag.

### F-022 — Health scorer TRIM-biased (`VULN`, Med, Phase 5)
Two compounding issues: (a) no fundamentals fetcher, so `HoldingContext.fundamentals`
is always empty in production; (b) docstring claims thresholds scale by votes_cast
but `score_holding` uses fixed `net ≥ 3 / ≤ −3`. Result: almost all holdings →
TRIM / "insufficient evidence". Also starves the SIP TOPUP bucket (needs HOLD).
- **Fix idea:** Wire a fundamentals source (yfinance `Ticker.info` or a static
  CSV), and/or implement the documented votes_cast scaling. Add tests for a clear
  HOLD and a clear EXIT with technicals-only evidence.

### F-023 — Paper equity never compounds realised P&L (`VULN`, High, Phase 5)
`reconcile.compute_portfolio_snapshot` uses a caller-constant `cash`; opens don't
debit, closes don't credit. `portfolio_snapshots.equity` = constant cash +
unrealised MTM of open positions only. Realised gains/losses disappear from the
equity curve and drawdown — the headline track record is wrong (closed-trade
table stats are still fine).
- **Fix idea:** Maintain a paper-cash ledger: debit `qty×entry + buy_costs` on
  open, credit `qty×exit − sell_costs` on close (mirror the backtest engine's cash
  handling). Snapshot equity = cash + open MTM.

### F-024 — `days_held` double-counts (`VULN`, Med, Phase 5)
`mtm.mtm_open_trades` bumps `days_held` per call; mid-day + post-close = 2/day.
- **Fix idea:** Derive `days_held` from `ts_entry` to `as_of` (calendar/trading
  days), or only bump on the post-close pass.

### F-025 — Backtest/paper cost asymmetry (`INACC`, Med, Phase 5)
Backtest applies full Zerodha costs + slippage; live paper MTM applies none.
Paper P&L is systematically rosier than the backtest that justifies the strategy.
- **Fix idea:** Apply `apply_slippage` + buy/sell charges in `mtm`/`ledger` close
  (tie to the F-023 cash ledger so both land together).

---

### F-026 — Narrative unverified + advisory staleness (`GAP`, Med, Phase 6)
`/analyst` is told to be evidence-first and to refuse a >12h-old bundle, but both
are instructions, not code. A hallucinated number/event in `brief.md`, or a
narrative written off a stale bundle, would pass through silently.
- **Fix idea:** Post-compile validator: assert figures quoted in `macro_brief.md`
  match the macro row; warn on symbols mentioned that aren't bundle candidates;
  move the 12h freshness check into `compile_brief` (compare `_Assembled at_` vs
  now) so it's deterministic.

### F-027 — Candidate-heading 3-way coupling (`DEBT`, Low, Phase 6)
`context._render_candidates` writes `### SYM — passes N/M rules`,
`pre_open_iep` rewrites it, `briefing._CANDIDATE_HEADING` parses it. No single
test spans all three; a format tweak breaks symbol parsing silently.
- **Fix idea:** Centralise the heading format in one constant/util imported by
  all three; add a round-trip test (render → IEP rewrite → parse).

### F-028 — Stale `assemble_context` docstring (`INACC`, Low, Phase 6)
Lists header/macro/candidates/health/open-trades/predictions; omits the sector
and Layer-B ranker sections actually rendered.
- **Fix idea:** Update the docstring.

---

### F-029 — Constant predictions break calibration (`VULN`, Med, Phase 7)
`pre_open._step_auto_open` sets `predicted_return_pct=20.0` and `target =
close×1.20` for every signal; `ml_score` is stored but unused as the prediction.
Reconcile → weekly-review calibration buckets therefore collapse to one (+20%)
bucket; `signal.target` also disagrees with the exit engine's `min(+20%, 2.5R)`.
- **Fix idea:** Set `predicted_return_pct` from the actual target vs entry (or the
  ranker probability mapped to an expected return), and set `signal.target` to the
  same `min(+20%, 2.5R)` the exit logic uses.

### F-030 — Visibility signals duplicate on re-run (`DEBT`, Low, Phase 7)
Non-selected candidates' `insert_signal` has no idempotency guard; re-running
pre_open the same day re-inserts them.
- **Fix idea:** Guard on `(symbol, date, created_by)` or UPSERT a daily signal row.

### F-031 — Train/serve feature skew (`INACC`, Low, Phase 7)
`weekly_train._step_retrain` passes `negative_news_lookup={}`, so
`negative_news_count_7d` is always NaN/empty in training but populated at
inference.
- **Fix idea:** Build the negative-news lookup for the training window too (or
  drop the feature until both paths feed it).

---

### F-032 — Live-run continuity depends on the human (`GAP`, Med, Phase 8)
Only `weekly_train` runs unattended; all daily steps are reminder-driven and
human-executed. A missed day leaves no Kite snapshot, no bundle, no MTM (open
trades unmanaged that day), and a gap in `portfolio_snapshots` — undermining the
3–6 month continuous track record the live run is meant to produce.
- **Fix idea:** Split the daily flow into broker-free steps (macro, sector, news,
  scan, OHLCV refresh) that can run unattended on a schedule like weekly_train,
  and Kite-dependent steps (snapshot, MTM) that still need the interactive session.
  Keeps the macro/scan/equity spine continuous on missed days.

---

_Counts: 31 open · 1 superseded · 0 fixed. Updated through Phase 8._
