# Architecture Review — Findings Log

> Running log of **vulnerabilities, gaps, inaccuracies, missing tests/fixtures,
> and tech debt** discovered while writing the [`docs/architecture/`](./PROGRESS.md)
> design set. The layer docs describe *what the code does today*; anything that
> *should change* lands here instead, so it can be triaged and fixed in a
> dedicated pass — after which we revisit and update the docs.

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
| F-012 | INACC | Med | 2 | Universe scope: paper-trading candidate set should be **Nifty 50 (50 stocks)** per user req; currently ~57 (Nifty 50 + holdings) under a `nifty200/` subdir | Open |
| F-013 | GAP | Low | 2 | No retention/compaction policy for `news_items` (append-only) or `data/raw/<date>/` JSON — unbounded growth | Open |
| F-014 | GAP | High | 3 | Only 12 symbols have parquet OHLCV on disk → live candidate universe is 12, not the configured ~57 nor the required Nifty 50 | Open |
| F-015 | GAP | Med | 3 | News symbol-attribution alias map covers only 12 symbols → sparse `sentiment_daily`, near-empty per-symbol sentiment/critical inputs | Open |
| F-016 | DEBT | Med | 3 | News dedup is URL-only and single-run; daily event/headline re-fetch creates duplicate `news_items` rows (no DB-level uniqueness) | Open |
| F-017 | INACC | Low | 3 | `macro_snapshot.dow_fut`/`nasdaq_fut` store spot index closes not futures; `sgx_nifty` always NULL | Open |
| F-018 | GAP | High | 3 | No automated daily OHLCV refresh and no read-time freshness guard; scan can silently run on stale parquet. Quote staleness also assumes host clock == IST | Open |
| F-019 | VULN | High | 4 | 4 of 10 Layer-A rules (regime, fno_banned, t2t, critical_event) are unconditional passes — `pre_open`/`scan` build `ScanContext` with all defaults; risk vetoes + regime gate are dead despite the data being available | Open |
| F-020 | INACC | Med | 4 | Two different "regime" concepts share the name: `features.regime` 4-axis voter (feeds sizing) vs Layer-A `passes_regime` rule (VIX<25/dd gate, unused) — different thresholds/inputs | Open |

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

### F-012 — Universe scope vs. naming (`INACC`, Low→**Med**, Phase 2)
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

### F-014 — Only 12 symbols ingested (`GAP`, High, Phase 3)
`data/parquet/nifty200/` holds 12 `.parquet` files; `universe.txt` lists 60 and
`sector_map.csv` 57. The scanner iterates the parquet dir, so the candidate
universe is 12. Blocks the Nifty-50 paper-trade requirement ([[F-012]]).
- **Fix idea (fix pass):** `trading ingest-history` for the 50 Nifty-50
  constituents; verify `list_symbols()` returns 50; re-run `pre-open` and
  confirm `candidates_total` ≈ 50.

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

### F-018 — No OHLCV freshness / refresh (`GAP`, High, Phase 3)
History is refreshed only via manual `ingest-history`; no daily re-pull, no
read-time staleness check beyond the trailing-NaN drop. A skipped refresh means
the scan runs on stale prices with no signal. Quote staleness additionally
assumes the host clock is IST (naive `datetime.now()`).
- **Verify in:** Phase 7 (does any job refresh OHLCV?). Phase 12.7 spec
  (`2026-06-11-phase-12-7-ohlcv-freshness-design.md`) exists — confirm whether
  implemented.
- **Fix idea:** Add an OHLCV refresh step to `pre_open` (or a freshness guard
  that warns/fails when the latest bar is older than the last trading day);
  centralise the IST clock ([[F-004]]).

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

_Counts: 19 open · 1 superseded · 0 fixed. Updated through Phase 4._
