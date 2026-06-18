# Architecture Review — Findings Log

> Running log of **vulnerabilities, gaps, inaccuracies, missing tests/fixtures,
> and tech debt** discovered while writing the [`docs/architecture/`](./PROGRESS.md)
> design set. The layer docs describe *what the code does today*; anything that
> *should change* lands here instead, so it can be triaged and fixed in a
> dedicated pass — after which we revisit and update the docs.

## Executive summary

The architecture review (docs 00–08) produced **35 active findings** (1 earlier
finding superseded; F-035/F-036 spun off from F-026's self-healing follow-up).
**21 are now fixed** (F-002, F-003, F-012, F-013, F-014, F-015,
F-016, F-018, F-019, F-021, F-022, F-023, F-024, F-025, F-026, F-029, F-031, F-032, F-033, F-034, F-035), leaving **14 open**. The system is **well-engineered at the
seams** — graceful degradation, idempotency, pure-function cores, clean
job/CLI/UI layers — but two themes undermine its current goal of proving itself
in a live paper-trade run:

1. **Data coverage.** ~~It trades **12 stocks, not the intended Nifty 50**~~
   (✅ fixed — now scans all 50 Nifty constituents) and ~~never refreshes
   prices~~ (✅ fixed — F-018: `pre_open` refreshes OHLCV + a staleness guard
   skips stale symbols). ~~News attribution still covers only 12 symbols~~
   (✅ fixed — F-015: `data/static/aliases.csv` now spans all 58 ingest symbols,
   so the sentiment signals are no longer starved).
2. **Measurement integrity.** ~~Four of ten risk rules are silently disabled~~
   (✅ fixed — F-019: the regime/VIX + critical-news gates are now wired from
   live macro/sentiment data), and the paper ~~equity curve never compounds
   realised P&L~~ (✅ fixed — F-023: cash is now derived from the trade ledger,
   so closing a trade compounds its P&L into equity — the metric the Phase 18.5
   go/no-go gate "OOS Sharpe > 1.0" depends on). Predictions ~~are a constant
   +20%~~ (✅ fixed — F-029: `signal.target` now uses the exit engine's
   `min(+20%, 2.5R)` and `predicted_return_pct` derives from that target, so
   calibration buckets vary per signal).

**Bottom line:** the build is solid; the gaps are in *what data flows through it*
and *how its results are measured*. Both are fixable with localized changes.

### Breakdown

| Severity | Count | IDs |
|---|---:|---|
| **High** | 1 | F-005† |
| Med | 7 | F-001, F-006, F-007, F-008, F-010, F-020, F-036 |
| Low | 6 | F-004, F-009, F-017, F-027, F-028, F-030 |
| ✅ Fixed | 21 | F-002, F-003, F-012, F-013, F-014, F-015, F-016, F-018, F-019, F-021, F-022, F-023, F-024, F-025, F-026, F-029, F-031, F-032, F-033, F-034, F-035 |

† F-005 (real-money execution / kill-switch) is `Needs decision`, gated to a
future Phase 19 — out of scope for hardening the paper run.

| Category | Count |
|---|---:|
| VULN (correctness/data-integrity) | 7 (F-019 ✅, F-022 ✅, F-023 ✅, F-024 ✅, F-029 ✅, F-033 ✅, F-034 ✅) |
| GAP (missing functionality/guardrail) | 11 (F-003 ✅, F-013 ✅, F-015 ✅, F-032 ✅) |
| INACC (code ≠ spec/docstring) | 7 (F-021 ✅, F-031 ✅) |
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
| ~~**F-019**~~ ✅ | High | **Done (2026-06-16).** `build_scan_context` populates `india_vix` (from `macro_snapshot`) + `critical_event_symbols` (from `sentiment_daily.has_critical`); `_step_scan`/CLI `scan` use it. Re-enables the regime/VIX gate + critical-news veto. `fno_ban`/`t2t` still need NSE feeds (F-010) | Re-enables 3 risk vetoes + the regime gate |
| ~~**F-023**~~ ✅ | High | **Done (2026-06-16).** New `reconcile.compute_paper_cash` derives cash from the trade ledger (debit `entry×qty` on open, credit `exit×qty` on close); `compute_portfolio_snapshot`/`reconcile_day`/`run_post_close` now take `initial_capital` (not a constant `cash`); CLI `--cash` → `--capital`. Equity = derived cash + open MTM, so realised P&L compounds. Round-trip costs now netted in too (F-025) | Makes the Phase-18.5 Sharpe metric trustworthy |
| ~~**F-029**~~ ✅ | Med | **Done (2026-06-16).** `strategy.exits.target_price` made public; `pre_open._step_auto_open` sets `signal.target = target_price(close, stop)` (the exit engine's `min(+20%, 2.5R)`) and drops the hardcoded `predicted_return_pct=20.0`, so the prediction defaults to the signal's implied target % and varies per signal | Makes calibration meaningful |

### Wave 2 — Correctness & accounting hygiene
| ID | Sev | Fix |
|---|---|---|
| ~~F-024~~ ✅ | Med | **Done (2026-06-16).** `mtm._days_held` derives `days_held = np.busday_count(ts_entry, as_of)` instead of `+1` per MTM call, so the twice-daily mid-day + post-close passes no longer double-count; mirrors the backtest's per-trading-day count (weekends excluded; holidays ignored to avoid a network call) |
| ~~F-025~~ ✅ | Med | **Done (2026-06-16).** `ledger.buy_side_cost`/`sell_side_cost` reuse `backtest.costs`; `compute_trade_pnl` returns gross−round-trip costs (mirrors backtest `net_pnl`) and `reconcile.compute_paper_cash` debits `entry+buy_cost` / credits `exit−sell_cost`, so paper P&L + the F-023 equity curve carry the backtest's friction. Entry/exit price columns kept clean |
| ~~F-022~~ ✅ | Med | **Done (2026-06-16).** `score_holding` scales the ±3 cut by votes_cast (`REFERENCE_BALLOT_SIZE=8`) so a technicals-only ballot reaches HOLD/EXIT not just TRIM; new `holding_context.build_holding_context` wires the latest `sentiment_daily` rollup (reviving the critical-news EXIT veto) + a static `data/static/fundamentals.csv` (`load_fundamentals_map`) into both `pre_open` and `monthly_sip` |
| ~~F-015~~ ✅ | Med | **Done (2026-06-17).** `data/static/aliases.csv` (58 symbols = Nifty 50 + 8 holdings) + `load_aliases_map`/`default_aliases`; `fetch_all_news` + rollup watch-list read it; bare-token disambiguation (`SBILIFE` before `SBIN`) |
| ~~F-016~~ ✅ | Med | **Done (2026-06-17).** v3 migration: `idx_news_dedup` UNIQUE(`source, headline, COALESCE(url,'')`) + `INSERT OR IGNORE`; NSE events get per-event URLs (`event_url`) so distinct events no longer collapse |
| ~~F-002~~ ✅ | High | **Done (2026-06-17).** `data/snapshot_schema.py` validates each broker/quote row against its dataclass at the read boundary (missing/extra field, wrong type, null-in-non-optional, invalid `exchange`); `kite_snapshot` + `quotes_snapshot` readers raise `SnapshotSchemaError` with file + index + remediation instead of splatting `Dataclass(**row)` |
| ~~F-021~~ ✅ | Med | **Done (2026-06-17).** `run_backtest` returns `total_costs = sum(t.costs_paid)`, so the aggregate carries buy + sell (was sell-only); accumulator + `_evaluate_exits` cost return removed |

### Wave 3 — Robustness & ops
~~F-003 (half-run/run-state detection)~~ ✅ **Done (2026-06-18)** — `trading
status` infers per-step completion from on-disk artifacts (time-aware
done/missing/pending; exit 1 on a due-step miss). ~~F-032 (run broker-free steps
unattended)~~ ✅ **Done (2026-06-18)** — `daily_unattended` runs the broker-free
pre-open spine on operator-absent days (gap-filler; skips days already covered).
~~F-013 (retention policy)~~ ✅ **Done (2026-06-18)** — `ops/retention.py` +
`trading prune` cap `raw/<date>/` (30d) and `news_items` (365d) growth, auto-run
in weekly_train. ~~F-031 (train/serve skew)~~ ✅ **Done (2026-06-18)** — shared
`news_store.negative_news_count_7d` feeds both inference and the training lookup
(`build_negative_news_lookup`). ~~F-026 (narrative validation)~~ ✅ **Done
(2026-06-18)** — `compile_brief` now refuses a >12h-old bundle
(`StaleBundleError`, deterministic) and warns when a VIX/USDINR figure cited in
`macro_brief.md` disagrees with the bundle. Its self-healing follow-up was split
into **F-035** (auto re-pull stale/missing macro) and **F-036** (multi-source
macro reconciliation). Remaining: F-010 (decide each dormant table: implement or
mark reserved).

### Wave 4 — Structural & cleanup (low-risk, alongside)
F-001 (prune unused deps), F-004 (canonical IST clock), F-006/F-007/F-008/F-009
(layering: domain module, fetch/classify split, break cycle, import-linter),
F-017 (macro column labels), F-020 (regime name collision), F-027 (heading
constant + spanning test), F-028 (docstring), F-030 (guard visibility signals).

### Separate track — needs a decision
F-005: real-money execution path + kill-switch/risk-halt. Gated behind the Phase
18.5 outcome; needs its own spec before any code.

> **Suggested first PR:** ~~F-014 + F-012 (Nifty-50 ingest).~~ ✅ **Shipped
> 2026-06-16.** F-018 (OHLCV freshness guard), F-019 (ScanContext wiring),
> F-023 (paper-cash ledger) and F-029 (real predictions) also shipped
> 2026-06-16 — **Wave 1 complete.** Wave 2 in progress: F-024 (days_held) +
> F-025 (paper costs) + F-022 (health TRIM-bias) done 2026-06-16; F-021 (backtest
> `total_costs` buy side) + F-015 (50-name alias map) + F-016 (news dedup) +
> F-002 (broker-JSON validation) done 2026-06-17. **Wave 2 complete.** Wave 3
> in progress: F-003 (`trading status` half-run detection) + F-032
> (`daily_unattended` broker-free gap-filler) + F-013 (`trading prune` data
> retention) + F-031 (train/serve feature parity) + F-026 (deterministic
> narrative guardrails) done 2026-06-18; F-035 (macro self-healing phase 1 —
> `trading macro refresh` + Kite cross-source gap-fill + `macro_reconciliation`
> table) done 2026-06-19 — next is F-036 (`/macro-doctor` skill + cross-verify)
> then F-010.

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
| ~~F-002~~ | GAP | High | 0 | ~~No schema validation on broker/quote JSON contract between `/kite-*` skills and `data/*_snapshot.py`~~ | ✅ Fixed 2026-06-17 — `data/snapshot_schema.py` validates each row at the read boundary (type/exchange/missing/extra), readers raise `SnapshotSchemaError` |
| ~~F-003~~ | GAP | Med | 0 | ~~Daily flow is ~13 manually-sequenced commands with no half-run/missed-step detection~~ | ✅ Fixed 2026-06-18 — `ops/run_status.py` + `trading status` infer per-step completion from on-disk artifacts (time-aware; exit 1 on a due-step miss) |
| F-004 | DEBT | Low | 0 | Each job re-derives "today" in IST independently; no single canonical clock | Open |
| F-005 | GAP | High | 0 | No real-money execution path, kill-switch, or risk-halt design (gated to future Phase 19, tracked here so it isn't forgotten) | Needs decision |
| F-006 | DEBT | Med | 1 | Domain DTOs live in `data/` so `store` depends "up" into the ingestion layer | Open |
| F-007 | DEBT | Med | 1 | `data.macro.snapshot_and_classify` puts a decision concern (regime classify) in the data layer (upward back-edge into `features`) | Open |
| F-008 | DEBT | Med | 1 | `strategy ⇄ backtest` package-level import cycle, broken only by lazy/TYPE_CHECKING imports | Open |
| F-009 | GAP | Low | 1 | No automated dependency-layering enforcement (e.g. import-linter); layering is convention-only | Open |
| F-010 | GAP | Med | 2 | 8 of 16 SQLite domain tables are defined but have zero writers (dormant schema reservations) | Open |
| F-011 | VULN | High | 2 | Rule gates depend on empty tables — **superseded by F-019** (root cause is unpopulated `ScanContext`, not the tables) | Superseded |
| F-012 | INACC | Med | 2 | Universe scope: paper-trading candidate set should be **Nifty 50 (50 stocks)** per user req; currently ~57 (Nifty 50 + holdings) under a `nifty200/` subdir | ✅ Fixed (2026-06-16) — candidate set pinned to Nifty 50; subdir rename deferred (cosmetic) |
| ~~F-013~~ | GAP | Low | 2 | ~~No retention/compaction policy for `news_items` (append-only) or `data/raw/<date>/` JSON — unbounded growth~~ | ✅ Fixed 2026-06-18 — `ops/retention.py` + `trading prune` (dry-run by default) cap `raw/<date>/` (30d) and `news_items` (365d); auto-run in weekly_train; `sentiment_daily` rollups kept |
| F-014 | GAP | High | 3 | Only 12 symbols have parquet OHLCV on disk → live candidate universe is 12, not the configured ~57 nor the required Nifty 50 | ✅ Fixed (2026-06-16) — all 50 Nifty + 8 holdings ingested; `candidates_total` 12→50 |
| ~~F-015~~ | GAP | Med | 3 | ~~News symbol-attribution alias map covers only 12 symbols → sparse `sentiment_daily`, near-empty per-symbol sentiment/critical inputs~~ | ✅ Fixed 2026-06-17 — `data/static/aliases.csv` covers all 58 ingest symbols; attribution + rollup watch-list both read it |
| ~~F-016~~ | DEBT | Med | 3 | ~~News dedup is URL-only and single-run; daily event/headline re-fetch creates duplicate `news_items` rows (no DB-level uniqueness)~~ | ✅ Fixed 2026-06-17 — v3 `idx_news_dedup` UNIQUE + `INSERT OR IGNORE`; NSE events get per-event URLs so distinct events stop colliding |
| F-017 | INACC | Low | 3 | `macro_snapshot.dow_fut`/`nasdaq_fut` store spot index closes not futures; `sgx_nifty` always NULL | Open |
| F-018 | GAP | High | 3 | No automated daily OHLCV refresh and no read-time freshness guard; scan can silently run on stale parquet. Quote staleness also assumes host clock == IST | ✅ Fixed (2026-06-16) — refresh step + scan staleness guard + Kite close cross-check; IST-clock centralisation ([[F-004]]) still open |
| F-019 | VULN | High | 4 | 4 of 10 Layer-A rules (regime, fno_banned, t2t, critical_event) are unconditional passes — `pre_open`/`scan` build `ScanContext` with all defaults; risk vetoes + regime gate are dead despite the data being available | ✅ Fixed (2026-06-16) — `build_scan_context` wires regime/VIX + critical-news gates; `fno_banned`/`t2t` still await NSE feeds ([[F-010]]) |
| F-020 | INACC | Med | 4 | Two different "regime" concepts share the name: `features.regime` 4-axis voter (feeds sizing) vs Layer-A `passes_regime` rule (VIX<25/dd gate) — different thresholds/inputs (the rule is now live as of F-019, sharpening the naming-collision risk) | Open |
| ~~F-021~~ | INACC | Med | 5 | ~~`BacktestResult.total_costs` omits buy-side charges (only sell-side accumulated); aggregate cost-drag understated (per-trade `costs_paid` is correct)~~ | ✅ Fixed 2026-06-17 — `total_costs = sum(t.costs_paid)` (buy + sell) |
| ~~F-022~~ | VULN | Med | 5 | ~~Health scorer structurally TRIM-biased: fundamentals AND sentiment never wired (pre_open/monthly_sip pass empty snapshots) → technicals-only + critical-news EXIT veto dead; docstring claims vote-count scaling not implemented (fixed ±3)~~ | ✅ Fixed 2026-06-16 — votes_cast scaling in `score_holding`; sentiment + static-CSV fundamentals wired into both jobs via `build_holding_context`; critical-news EXIT veto live |
| ~~F-023~~ | VULN | High | 5 | ~~Paper equity curve never compounds realised P&L — cash is a constant; closing a winner drops its gain from `portfolio_snapshots.equity`. Equity/drawdown are not a true track record~~ | ✅ Fixed 2026-06-16 — `compute_paper_cash` derives cash from the ledger; equity = derived cash + open MTM, so realised P&L compounds |
| ~~F-024~~ | VULN | Med | 5 | ~~`days_held` bumped per MTM call, so mid-day + post-close double-count → 25-day time stop fires at ~12 calendar days~~ | ✅ Fixed 2026-06-16 — `days_held = np.busday_count(ts_entry, as_of)`, derived not incremented, so same-day passes don't double-count |
| ~~F-025~~ | INACC | Med | 5 | ~~Cost asymmetry: backtest applies full Zerodha costs but live paper MTM applies none (raw-price fills) → paper results flatter than backtest~~ | ✅ Fixed 2026-06-16 — `ledger.buy_side_cost`/`sell_side_cost` reuse the backtest cost model; `compute_trade_pnl` nets round-trip costs into pnl and `compute_paper_cash` debits/credits them, so paper P&L + equity carry the same friction as the backtest |
| ~~F-026~~ | GAP | Med | 6 | ~~Analyst narrative is unvalidated against the bundle — "evidence-first" is an LLM instruction, not a code check; wrong/invented numbers in brief.md pass through. Refuse-stale (12h) is also advisory-only~~ | ✅ Fixed 2026-06-18 — `compile_brief` raises `StaleBundleError` on a >12h-old bundle (deterministic, `--allow-stale` override) and warns when a VIX/USDINR figure in `macro_brief.md` disagrees with the bundle. Self-healing follow-up → F-035/F-036 |
| F-027 | DEBT | Low | 6 | Brittle 3-way coupling on the `### SYM — passes N/M rules` heading (context renderer / pre_open_iep rewrite / briefing regex), no spanning test | Open |
| F-028 | INACC | Low | 6 | `assemble_context` docstring omits the sector + Layer-B ranker sections (added Phases 12.6/16) | Open |
| ~~F-029~~ | VULN | Med | 7 | ~~`pre_open._step_auto_open` hardcodes `predicted_return_pct=20.0` + target=+20% for every signal → prediction calibration is a single meaningless bucket; signal.target disagrees with exit engine's min(+20%,2.5R)~~ | ✅ Fixed 2026-06-16 — `signal.target = target_price(close, stop)` (exit engine's `min(+20%, 2.5R)`); prediction defaults to that target's implied %, so buckets vary per signal |
| F-030 | DEBT | Low | 7 | Visibility-only (non-selected) signals inserted unconditionally each pre_open run → duplicate `signals` rows on re-run | Open |
| ~~F-031~~ | INACC | Low | 7 | ~~Train/serve skew: `negative_news_count_7d` empty during weekly retrain (`negative_news_lookup={}`) but populated at inference~~ | ✅ Fixed 2026-06-18 — shared `news_store.negative_news_count_7d` feeds both paths; `ranker_io.build_negative_news_lookup` precomputes the training lookup |
| ~~F-032~~ | GAP | Med | 8 | ~~Daily pipeline is human-run reminders only (sole unattended job is weekly_train); a missed day = no snapshot/bundle/MTM, open trades unmanaged, track-record holes~~ | ✅ Fixed 2026-06-18 — `jobs/daily_unattended.py` + `trading daily-unattended` run the broker-free pre-open spine unattended (gap-filler); afternoon MTM still interactive |
| F-033 | VULN | Med | 6 | Candidate-symbol regex `[A-Z0-9_]+` excludes `-`/`&`, so hyphen/ampersand tickers (BAJAJ-AUTO, M&M) are silently dropped from `brief.md` and deleted from `_context.md` by `pre_open_iep` | ✅ Fixed (2026-06-17) |
| ~~F-035~~ | GAP | Med | 6 | ~~No self-healing: a stale/missing macro snapshot only refuses (F-026) or degrades — nothing auto re-pulls it. The remedy is a manual `assemble-context` re-run. Belongs in the data layer (re-ingestion), not the LLM~~ | ✅ Fixed 2026-06-19 — `trading macro refresh` deterministically re-pulls + upserts the snapshot; `--cross <kite file>` gap-fills still-missing VIX/USDINR from a validated Kite MCP second source with `macro_reconciliation` provenance (migration v4). `/macro-doctor` skill orchestration + cross-verify → F-036 |
| F-036 | GAP | Med | 6 | Macro figures (VIX/USDINR/FII/DII) come from a single provider with no cross-source reconciliation; a wrong upstream value flows through unflagged. F-026 can only check the brief against the bundle, not the bundle against reality | Open |

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

### F-002 — No validation of the broker JSON contract (`GAP`, High, Phase 0) — ✅ Fixed 2026-06-17
**Was:** `/kite-snapshot` and `/kite-quotes-snapshot` write JSON that
`data/kite_snapshot.py` and `data/quotes_snapshot.py` read back into dataclasses
via a bare `Dataclass(**row)`. The contract was enforced only by the skill
obeying its `SKILL.md`; a malformed write (wrong `exchange`, missing field, wrong
types) either crashed with a cryptic `TypeError`/`KeyError` or — since frozen
dataclasses don't type-check — passed silently and corrupted downstream logic
(e.g. a wrong-exchange quote driving an MTM exit).

**Resolution:**
- New `data/snapshot_schema.py`: `validate_row`/`validate_rows` drive off
  `dataclasses.fields` + resolved type hints (so the validator can't drift from
  the dataclass) and reject: non-object/array shapes, missing required fields,
  unexpected fields (contract drift / wrong resource file), wrong scalar types
  (`bool` rejected as `int`; `int`→`float` widening allowed), null in a
  non-optional field, bad list-element types, and any `exchange` outside the Kite
  set `{NSE,BSE,NFO,BFO,CDS,BCD,MCX,NCO}`. Every `SnapshotSchemaError` carries the
  file, row index, field, and a "re-run the skill" remediation.
- `kite_snapshot.read_holdings/gtts/positions` and
  `quotes_snapshot.read_latest_quotes` now route rows through the validator;
  the quotes reader also validates `tradingsymbol` presence/type before popping
  it (was a bare `KeyError` mid-loop).
- TDD: `tests/test_snapshot_schema.py` (15 unit cases) + 2 integration tests in
  `test_kite_snapshot.py` (invalid exchange, missing field) + 2 in
  `test_quotes_snapshot.py` (wrong type, missing `tradingsymbol`). Full suite
  green, ruff + mypy clean.

### F-003 — No half-run detection in the daily flow (`GAP`, Med, Phase 0) — ✅ Fixed 2026-06-18
**Was:** the operator runs the blocks manually; skipping IEP (or running blocks
out of order) silently left the bundle un-reranked or stale. Nothing reconciled
"which steps ran today".

**Resolution (artifact-inference, no stamping):**
- New `ops/run_status.py`: `compute_status(paths, as_of, *, now=None, conn=None)`
  resolves **8 checkpoints across the 4 IST blocks** by probing the durable
  artifact each step leaves — `holdings.json` (+`_meta.snapshot_at`),
  `_context.md` (scan), `macro_brief.md` (analyst), `brief.md` (compile), an
  IEP-band `quotes_*.json`, `_context.md` re-touched after those quotes (IEP
  filter), `mid_day_update.md`, and `post_close_summary.md` **or** a
  `portfolio_snapshots` row. No job or skill stamps anything (the two `prepare`
  steps share one overwritten `_quote_symbols.txt`, so a block's "apply" output
  is the meaningful signal — hence 8 checkpoints, not all 13 reminder slots).
- **Time-aware:** an un-run step is `missing` only once its IST slot time has
  passed (or for any past date), else `pending`; non-trading days are `n/a`.
  `has_due_failure` is true iff some checkpoint is `missing`.
- New `trading status [--date]` (default today IST) renders a `rich` table
  grouped by block + a per-block summary, and **exits 1 on a real half-run** so
  cron/scripts can gate on it. Verified live on 2026-06-17 (IEP + post-close
  correctly flagged ❌, exit 1).
- TDD: `tests/test_run_status.py` (17 tests) — probe truth-tables, the
  done/missing/pending state machine with an injected `now`, past-date
  all-missing, future-date all-pending, non-trading-day n/a, IEP quote
  time-band + the IEP-filter mtime heuristic, DB-row post-close detection, and
  two CLI smoke tests (exit 1 half-run / exit 0 nothing-due). Full suite green
  (864 passed), ruff + mypy clean.

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

### F-013 — No data retention policy (`GAP`, Low, Phase 2) — ✅ Fixed 2026-06-18
**Resolution:** New `ops/retention.py` prunes the stale tail of both unbounded
stores. `prune_raw_dirs` deletes only `raw_dir/` children whose name parses as an
ISO date older than the cutoff (non-date entries untouched); `prune_news` deletes
`news_items` rows older than the cutoff while **keeping** the derived
`sentiment_daily` rollups (tiny, and feed the live 30-day health scorer), so the
prune is lossless for the live path. Defaults: **raw 30d, news 365d**. Everything
is **dry-run unless `apply=True`**. Exposed as `trading prune`
(`--apply`/`--raw-days`/`--news-days`) and auto-run as a housekeeping step inside
the Sunday `weekly_train` (`WeeklyTrainResult.retention`). Verified live: the
dry-run flagged 3 stale May `raw/<date>/` dirs and deleted nothing.

*Original finding:* `news_items` and `data/raw/<date>/` grow without bound.

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

### F-015 — Sentiment attribution covers 12 symbols (`GAP`, Med, Phase 3) — ✅ Fixed 2026-06-17
`news.DEFAULT_ALIASES` had 12 entries (holdings + smoke universe). Most Nifty-50
names' news was never attributed → `sentiment_daily` sparse → sentiment +
critical-news inputs empty for most candidates (compounded [[F-011]]/[[F-019]]).
- **Fixed:** new maintained `data/static/aliases.csv` (mirrors the `sector_map`/
  `fundamentals` static-CSV convention) covers the **full 58-symbol ingest
  universe** — all 50 Nifty constituents + 8 holdings — with `|`-separated
  company-name variants. New `news.load_aliases_map()` parses it and
  `news.default_aliases()` prefers it (falling back to the built-in
  `DEFAULT_ALIASES` only on a fresh checkout without the CSV). `fetch_all_news`
  defaults to it, and `pre_open._step_news` / `cli news-pull` now derive the
  sentiment-rollup watch-list from `default_aliases().keys()` too — so both
  attribution **and** the rollup span all 58.
- **Disambiguation:** ambiguous bare tokens ("Bajaj", "Adani", "Tata", "HDFC")
  are deliberately excluded in favour of full distinguishing names; more-specific
  symbols precede the generic ticker they share a prefix with (`SBILIFE` before
  `SBIN`, so "SBI Life" doesn't fall through to `SBIN`). Hyphen/ampersand tickers
  (`BAJAJ-AUTO`, `M&M`) attribute via both ticker and name.
- **Tests:** `test_news_aliases.py` — loader (pipe-split, blank cell, comments,
  missing-file → `{}`), `default_aliases` CSV-preferred + built-in fallback, the
  shipped CSV covering all of `universe.txt`, hyphen/ampersand attribution, and
  the SBI/SBI-Life disambiguation. Watched the import fail first (RED), then green
  (9 tests); news + pre_open suites green, ruff + mypy clean.

### F-016 — Duplicate news rows across runs (`DEBT`, Med, Phase 3) — ✅ Fixed 2026-06-17
**Was:** `fetch_all_news` deduped by URL within one call only; daily re-fetch of
RSS + NSE events re-inserted the same rows with no DB uniqueness constraint. Worse,
every NSE event for a symbol shared one landing-page URL, so even *within* a run
distinct events (Board Meeting vs. Dividend) collapsed to a single row.

**Resolution:**
- `news.event_url(symbol, purpose, date_str)` pins each NSE event's identity into
  a deterministic URL fragment, so distinct events get distinct URLs (and the same
  event re-fetched later yields the same URL → dedupes cleanly).
- Migration **v3** (`SCHEMA_V3`): `idx_news_dedup` UNIQUE on
  `(source, headline, COALESCE(url,''))`. The `COALESCE` keeps null-URL macro
  headlines deduping on `(source, headline)` instead of collapsing into one row.
  Any pre-existing duplicate rows are collapsed (keep lowest `id`) before the
  index is created.
- `news_store.insert_news_items` now uses `INSERT OR IGNORE` and returns the count
  *actually written* (via `conn.total_changes`), so a re-run of an identical batch
  is a no-op returning 0.
- TDD: 3 store tests (cross-run ignore, distinct-headlines-sharing-URL, null-URL
  dedup) + 2 migration tests (unique-index rejects raw dup, v3 collapses
  pre-existing dups) + 2 news tests (`event_url` uniqueness, orchestrator keeps
  distinct NSE events). Full suite green, ruff + mypy clean.

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

### F-019 — Four Layer-A gates are no-ops in production (`VULN`, High, Phase 4) — ✅ Fixed 2026-06-16
**Resolution:** New `jobs/pre_open.build_scan_context(conn, as_of)` assembles the
`ScanContext` from data this run already persisted:
- `india_vix` ← `macro_snapshot.vix` → re-enables the regime/VIX gate
  (`passes_regime`);
- `critical_event_symbols` ← new `store/news_store.list_critical_symbols`
  (`sentiment_daily.has_critical` for `as_of`) → the FinBERT critical-news veto
  now fires.

`pre_open._step_scan` (signature gained `conn`) and `cli.py::scan_cmd` both call
it — the CLI builds it best-effort from the DB, degrading to the indicator-only
preview when no snapshot exists. `nifty200_drawdown_5d_pct` stays `None` (not yet
stored; the rule degrades gracefully). `fno_ban_symbols`/`t2t_symbols` remain
empty until the NSE ban-list/T2T feeds land ([[F-010]]). Tests assert a critical
symbol is vetoed and that an empty DB degrades to passing. Supersedes [[F-011]].
*Naming caveat:* `passes_regime` is now live, which makes the [[F-020]] name
collision worth resolving next.

*Original finding:*
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
- **Doc to revisit:** `04-analysis-strategy.md` §3.4; `07-jobs-workflows.md`.

### F-020 — "regime" name collision (`INACC`, Med, Phase 4)
`features.regime.classify_regime` (4-axis voter, VIX 14/20 thresholds, feeds
sizing multiplier) vs `strategy.rules.passes_regime` (gate: VIX<25 AND Nifty-200
5d dd>−5%). Same word, different logic; the rule one is also unused (F-019).
- **Fix idea:** Rename the Layer-A rule (e.g. `passes_market_filter`) and, when
  wiring F-019, decide whether the gate should reuse the macro voter's RISK_OFF
  classification instead of its own thresholds.

---

### F-021 — Backtest total_costs omits buy side (`INACC`, Med, Phase 5) — ✅ Fixed 2026-06-17
`engine._evaluate_exits` returned only sell-side charges; `run_backtest` added
just that to `total_costs`. Buy-side charges (in `_OpenPosition.buy_costs_paid`)
reached `Trade.costs_paid` but not the aggregate, so the headline cost-drag
understated friction by ~the buy-side half (per-trade `costs_paid` was correct).
- **Fixed:** `run_backtest` now computes `total_costs = sum(t.costs_paid for t in
  completed_trades)` after the loop — each `Trade.costs_paid` already carries buy
  + sell (and buy-only for `OPEN_AT_END`), so the aggregate is exact. The running
  accumulator + `_evaluate_exits`' separate `costs_total` return (and the stale
  "clean approach below" comment) were removed. Slippage stays out of `costs` (a
  price shift captured in gross P&L). Test:
  `test_total_costs_includes_buy_and_sell_side` asserts
  `total_costs == sum(costs_paid)` (watched fail at sell-only 34.03 vs 65.48, then
  pass); full engine + walkforward suites green, ruff + mypy clean.

### F-022 — Health scorer TRIM-biased (`VULN`, Med, Phase 5) — ✅ Fixed 2026-06-16
Two compounding issues: (a) fundamentals AND sentiment were never wired —
`pre_open`/`monthly_sip` built `HoldingContext` with empty Fundamentals/Sentiment,
so the 30d-sentiment vote and the critical-news EXIT veto were dead; (b) the
docstring claimed thresholds scale by votes_cast but `score_holding` used a fixed
`net ≥ 3 / ≤ −3`. Result: a technicals-only ballot (≈4 axes) could almost never
reach ±3 → almost everything → TRIM / "insufficient evidence", starving the SIP
TOPUP bucket (which needs HOLD).
- **Fixed (b) — votes_cast scaling:** `score_holding` now compares
  `net/votes_cast` against `±HOLD_NET_THRESHOLD/REFERENCE_BALLOT_SIZE` (±3/8 =
  ±0.375). On a full 8-axis ballot this reduces to the original net ≥ 3 / ≤ −3
  (backward-compatible), but a 4-axis technicals-only ballot with net ±2 now
  classifies HOLD/EXIT. The `votes_cast < 3` "insufficient evidence" guard stays.
- **Fixed (a) — sentiment wired:** new `store.news_store.get_latest_sentiment_daily`
  (freshest rollup ≤ as_of) feeds `SentimentSnapshot(score_30d, has_critical)` via
  the new `portfolio.holding_context.build_holding_context`, used by *both* jobs —
  so the critical-news EXIT veto is live on holdings, not just the scan gate.
- **Fixed (a) — fundamentals seam:** new `portfolio.fundamentals.load_fundamentals_map`
  reads an offline `data/static/fundamentals.csv` (mirrors `sector_map.csv`;
  blank/absent → empty snapshot, no network in the hot path) into the same builder.
  Ships empty (header + format docs) — populate it, or later swap a fetcher behind
  the same seam, with no scorer change.
- **Tests:** technicals-only net ±2 → HOLD/EXIT + full-ballot backward-compat
  (test_health); latest-sentiment selection + critical flag (test_news_store);
  CSV parse/blank/missing (test_portfolio_fundamentals); context wiring + veto
  (test_portfolio_holding_context); and a `pre_open` job-level critical→EXIT
  integration test.

### F-023 — Paper equity never compounds realised P&L (`VULN`, High, Phase 5) — ✅ Fixed 2026-06-16
~~`reconcile.compute_portfolio_snapshot` uses a caller-constant `cash`; opens don't
debit, closes don't credit. `portfolio_snapshots.equity` = constant cash +
unrealised MTM of open positions only. Realised gains/losses disappear from the
equity curve and drawdown — the headline track record is wrong (closed-trade
table stats are still fine).~~
- **Fixed:** New `reconcile.compute_paper_cash(conn, *, as_of, initial_capital)`
  derives cash purely from `paper_trades` — debit `entry_price×qty` on open,
  credit `exit_price×qty` on close (mirrors the backtest engine), filtered by
  `date(ts_entry|ts_exit) ≤ as_of` so re-running an old date is reproducible.
  `compute_portfolio_snapshot`/`reconcile_day`/`run_post_close` now take
  `initial_capital` (the t=0 seed) instead of a constant `cash`; equity = derived
  cash + open MTM, so closing a winner raises the curve and a loser lowers it.
  CLI option `--cash` renamed to `--capital`. Tests assert a closed winner lifts
  equity above seed and a loser drops it below.
- **✅ Costs now applied (F-025, 2026-06-16):** buy/sell costs plug into the same
  debit/credit seam (`qty×entry + buy_side_cost` / `qty×exit − sell_side_cost`) and
  net into `compute_trade_pnl`, so the equity curve carries the backtest's friction.

### F-024 — `days_held` double-counts (`VULN`, Med, Phase 5) — ✅ Fixed 2026-06-16
`mtm.mtm_open_trades` bumped `days_held` per call; mid-day + post-close = 2/day,
so the 25-day time stop fired at ~12 calendar days.
- **Fixed:** new `mtm._days_held(ts_entry, as_of)` returns
  `np.busday_count(entry_date, as_of.date())` — `days_held` is now a pure
  function of (entry, as_of), so the two same-day MTM passes yield the same
  value instead of accumulating. Business days (weekends excluded) mirror the
  backtest engine's per-trading-day count (entry day = 0, +1 per subsequent
  trading day); NSE holidays are deliberately ignored to keep the MTM hot path
  off the network holiday calendar (`is_trading_day` costs a ~10s nsepython
  fetch). Tests: same-day double-count regression + updated time-stop fixture
  (entry 2026-04-13 → 25 business days at 2026-05-16).

### F-025 — Backtest/paper cost asymmetry (`INACC`, Med, Phase 5) — ✅ Fixed 2026-06-16
Backtest applies full Zerodha costs + slippage; live paper MTM applied none.
Paper P&L was systematically rosier than the backtest that justifies the strategy.
- **Fixed:** new `ledger.buy_side_cost` / `ledger.sell_side_cost` reuse the backtest
  cost model (`backtest.costs.buy_charges`/`sell_charges` + per-side slippage as a
  cash drag) — no import cycle (`backtest.costs` is stdlib-only). Both close paths
  now net these in: `compute_trade_pnl` returns `pnl_abs = gross − (buy_side_cost +
  sell_side_cost)` (and `pnl_pct` on that net), mirroring the backtest's `net_pnl`;
  and `reconcile.compute_paper_cash` debits `entry_value + buy_side_cost` on open and
  credits `exit_value − sell_side_cost` on close, so the F-023 equity curve carries
  the same friction. Entry/exit price columns stay clean (decision prices) so
  predictions/targets/display are unaffected. Costs are per-row (₹20 brokerage cap +
  GST make them non-linear), so cash iterates rows rather than SUMming in SQL.
  Tests: ledger pnl (win/loss/zero-entry), mtm stop/target pnl, reconcile cash/equity
  + reconcile_day, post_close summary — all assert gross-minus-cost expecteds.

---

### F-026 — Narrative unverified + advisory staleness (`GAP`, Med, Phase 6) — ✅ Fixed 2026-06-18
**Was:** `/analyst` is told to be evidence-first and to refuse a >12h-old bundle,
but both were instructions, not code. A hallucinated number in `brief.md`, or a
narrative written off a stale bundle, would pass through silently.

**Resolution (deterministic guardrails in `compile_brief`):**
- **Staleness is now code, not advice.** `compile_brief` parses the bundle's
  `_Assembled at_` stamp and raises `StaleBundleError` when it is older than
  `max_age` (default 12h) relative to `now` (both injectable for tests). A missing
  stamp skips the check (age unknowable); `allow_stale=True` / the CLI
  `--allow-stale` flag is the deliberate override. `trading brief compile`
  surfaces it as a clean exit-1 message pointing at `assemble-context`.
- **Macro figure cross-check (warn-only).** The bundle's `## Macro snapshot` table
  is parsed from `_context.md` (so `compile_brief` stays a pure file op — the
  bundle, not the DB, is the source of truth at compile time). For the
  unambiguous, sign-stable fields VIX and USDINR, if `macro_brief.md` cites the
  field by keyword but the bundle's value isn't found (rounding-tolerant), a
  `warning:` is printed to stderr — same channel as the existing orphan-candidate
  warning. Non-fatal, so a correctly-rounded figure never breaks the build but an
  invented/transposed one is surfaced. FII/DII flows (₹/comma/sign formatting)
  are deliberately left to the human to avoid false positives.

Tests: `test_llm_briefing` (stale raises, allow_stale bypasses, fresh compiles,
absent-stamp skips, figure mismatch warns, match/absent-keyword quiet) +
`test_cli` (refuses stale exit-1, `--allow-stale` overrides).

**Deliberately out of scope (→ F-035, F-036):** auto re-pulling a stale/missing
macro snapshot, and cross-source verification of the bundle's figures against a
second provider. Both belong in the **data/ingestion layer**, not the
narrative-compile step — keeping ingestion decoupled from analysis preserves the
bundle's reproducibility. F-026's guardrail remains the safety net for when a
self-healing refresh itself returns nothing.

*Original fix idea:* post-compile validator asserting `macro_brief.md` figures
match the macro row + deterministic 12h freshness check in `compile_brief`.

### F-027 — Candidate-heading 3-way coupling (`DEBT`, Low, Phase 6)
`context._render_candidates` writes `### SYM — passes N/M rules`,
`pre_open_iep` rewrites it, `briefing._CANDIDATE_HEADING` parses it. No single
test spans all three; a format tweak breaks symbol parsing silently. **[[F-033]]
is the concrete bite** — the symbol char-class desynced and silently dropped
hyphen/ampersand tickers from two of the three parsers (fixed 2026-06-17), but
the structural coupling + missing spanning test this finding names remain.
- **Fix idea:** Centralise the heading format in one constant/util imported by
  all three; add a round-trip test (render → IEP rewrite → parse).

### F-028 — Stale `assemble_context` docstring (`INACC`, Low, Phase 6)
Lists header/macro/candidates/health/open-trades/predictions; omits the sector
and Layer-B ranker sections actually rendered.
- **Fix idea:** Update the docstring.

---

### F-029 — Constant predictions break calibration (`VULN`, Med, Phase 7) — ✅ Fixed 2026-06-16
`pre_open._step_auto_open` set `predicted_return_pct=20.0` and `target =
close×1.20` for every signal; reconcile → weekly-review calibration buckets
collapsed to one (+20%) bucket and `signal.target` disagreed with the exit
engine's `min(+20%, 2.5R)`.
- **Fixed:** `strategy.exits._target_price` → public `target_price`;
  `_step_auto_open` now sets `signal.target = target_price(cand.close, stop_price)`
  (the exact price the exit engine aims for) and drops the hardcoded
  `predicted_return_pct=20.0`, so `log_signal_and_open_trade` defaults the
  prediction to the signal's implied target % `((target - entry)/entry)`. The
  prediction now varies per signal with stop distance (R), so calibration buckets
  are meaningful and `signal.target` agrees with the exit logic. Test:
  `test_step_auto_open_target_and_prediction_track_exit_engine` (close=100, atr=2
  → target 107.5, predicted 7.5%).

### F-030 — Visibility signals duplicate on re-run (`DEBT`, Low, Phase 7)
Non-selected candidates' `insert_signal` has no idempotency guard; re-running
pre_open the same day re-inserts them.
- **Fix idea:** Guard on `(symbol, date, created_by)` or UPSERT a daily signal row.

### F-031 — Train/serve feature skew (`INACC`, Low, Phase 7) — ✅ Fixed 2026-06-18
**Was:** `weekly_train._step_retrain` passed `negative_news_lookup={}`, so
`negative_news_count_7d` was always NaN/empty in training but populated at
inference — the model never trained on real values of a feature it sees at serve
time.

**Resolution (one source of truth, parity-guaranteed):**
- Extracted the trailing-7d negative-news query into
  `store.news_store.negative_news_count_7d(conn, symbol, as_of)` (count of
  `news_items` with `sentiment < -0.20` in `[as_of-7d, as_of]`; `None` when no
  news in window, `0` when news but none negative). Inference (`strategy.ranker`,
  both call sites) now calls it instead of its own private copy.
- `strategy.ranker_io.build_negative_news_lookup(conn, enriched)` precomputes the
  `(date_iso, symbol) → int` training lookup with that **same** function over each
  symbol's trading-date index (work bounded to the news-active span), exposed as
  `TrainingInputs.negative_news_lookup`.
- `weekly_train._step_retrain` passes `inputs.negative_news_lookup` instead of
  `{}`. A missing key resolves to NaN in `_build_xy_for_window`, exactly as
  inference's `None` does — so historical dates with no news (news is young /
  pruned to 365d by F-013) match on both paths, and recent folds carry the real
  values the model meets at serve time.

*Original finding:* `weekly_train._step_retrain` passes `negative_news_lookup={}`,
so `negative_news_count_7d` is always NaN/empty in training but populated at
inference. *Fix idea:* build the negative-news lookup for the training window too.

---

### F-032 — Live-run continuity depends on the human (`GAP`, Med, Phase 8) — ✅ Fixed 2026-06-18
**Was:** only `weekly_train` ran unattended; all daily steps were reminder-driven
and human-executed. A missed day left no Kite snapshot, no bundle, no MTM (open
trades unmanaged that day), and a gap in `portfolio_snapshots` — undermining the
3–6 month continuous track record the live run is meant to produce.

**Resolution (broker-free unattended gap-filler):**
- `run_pre_open` gained `require_snapshot: bool = True`. When `False`,
  `_step_portfolio` degrades a missing/stale Kite snapshot to a warning + `[]`
  (no holdings health) instead of raising `PreOpenAborted`; `_step_cross_check`
  already silently skips. The whole broker-free spine — macro, sector, news,
  OHLCV refresh, scan, rank, **auto-open**, bundle — runs unchanged.
- New `jobs/daily_unattended.py`: `run_daily_unattended(as_of, *, force=False)`
  is holiday-gated, **skips when the operator already produced today's bundle**
  (reuses the `run_status` `pre_open_scan` artifact probe so it and `trading
  status` agree on "ran"), else calls `run_pre_open(require_snapshot=False)` and
  posts an info notification. `--force` overrides the skip.
- New `trading daily-unattended [--date] [--force]` CLI (mirrors `weekly-train`:
  `configure_logging` + `logger.exception` → durable `failures.log` on failure),
  plus `docs/scheduler/trading_daily_unattended.xml` (UTF-16, Mon–Fri 10:00 IST,
  `StartWhenAvailable`) so it runs after the manual pre-open window.
- **Scope:** keeps the macro/scan/auto-open/track-record **spine** continuous on
  missed days. MTM / exit-management of open trades needs live Kite quotes and
  stays interactive — a missed *afternoon* is not back-filled (noted as still
  open).
- TDD: `test_jobs_daily_unattended.py` (skip on non-trading day / already-ran /
  force; runs degraded with `require_snapshot=False` when absent) + 3 pre_open
  degradation tests + 2 CLI smoke tests (offline holiday-skip; failure →
  failures.log) + the scheduler-XML encoding guard auto-covers the new XML. Full
  suite green (880 passed), ruff + mypy clean.

---

### F-033 — Hyphen/ampersand tickers dropped by candidate-symbol regex (`VULN`, Med, Phase 6) — ✅ Fixed 2026-06-17
The candidate-symbol regex `^### ([A-Z0-9_]+) — passes \d+/\d+ rules` matches
only `A–Z 0–9 _`, so any NSE ticker containing `-` or `&` fails to match. Of the
Nifty-50 candidate set this hits **BAJAJ-AUTO** and **M&M**. Two silent failures
resulted:
- `briefing._parse_candidate_symbols` returned the symbol set **without**
  `BAJAJ-AUTO`, so `compile_brief` treated `candidates/BAJAJ-AUTO.md` as an
  *orphan* and dropped it — the day's #1-ranked, 10/10, selected pick was missing
  from `brief.md` (warning emitted to stderr only).
- `pre_open_iep._parse_candidates_from_context` parsed 4 of 5 candidates, and
  `_update_context_markdown` (which rebuilds the candidates section from the
  parsed list) **deleted the BAJAJ-AUTO block from `_context.md`** on the in-place
  rewrite.

Discovered live during the 2026-06-17 daily run (BAJAJ-AUTO ranked #1 that day).
This is the concrete data-integrity instance of the coupling flagged in [[F-027]]
— the same heading format is parsed in three places with no spanning test.
- **Fixed:** widened the character class to `[A-Z0-9_&-]+` at all three sites —
  `briefing._CANDIDATE_HEADING` (×1) and `pre_open_iep` (the candidate parser +
  the block-key `re.match`, ×2). `-` is trailing (literal) and `&` literal in the
  class. TDD: added `test_parse_candidates_from_context_hyphen_and_ampersand` +
  `test_update_context_preserves_hyphenated_symbol_block` (test_jobs_pre_open_iep)
  and `test_compile_brief_no_orphan_warning_for_hyphenated_symbol`
  (test_llm_briefing) — all three watched fail, then pass; full IEP + briefing
  suites green (42), ruff clean. Re-ran `pre-open-iep` + `brief compile` for
  2026-06-17: `candidates_input` 4→5 and all five cards present in `brief.md`.
- **Still open ([[F-027]]):** centralise the heading format in one constant and
  add a render→IEP-rewrite→parse round-trip test so a future format tweak can't
  silently desync the three parsers again.

---

### F-034 — Dashboard rule grid crashes on the signals JSON contract (`VULN`, Med, Phase 15) — ✅ Fixed 2026-06-18
**Was:** `ui/components.rule_chip_grid` (and its docstring) assumed
`signals.rules_passed_json` was a `{rule_name: bool}` map and called
`rules.items()`. The writer (`jobs/pre_open._step_auto_open`, line 399) actually
persists `json.dumps([r.name for r in cand.rules if r.passed])` — a **list of the
passed rule names only**. Opening **Today's Signals → "Rule evaluation per
signal"** therefore crashed with
`AttributeError: 'list' object has no attribute 'items'`. Two layered problems:
1. **Crash:** the page is unusable whenever a signal row carries rule data.
2. **Latent mis-report:** signals are logged for non-selected candidates too
   (`_step_auto_open`, "visibility-only"), which can pass <10 rules — and since
   only *passed* names are stored, naively rendering the list would paint a 9/10
   as ten green chips, hiding the failed rule.

Another instance of the write/read format coupling flagged in [[F-027]] /
[[F-033]]: one JSON shape produced in `pre_open`, parsed elsewhere, no spanning
contract test. Discovered live opening the Streamlit dashboard on 2026-06-18.

**Resolution:**
- New `LAYER_A_RULE_NAMES` tuple in `strategy/rules.py` — single source of truth
  for the 10 rule names in `evaluate_symbol` order, locked by
  `test_layer_a_rule_names_match_evaluate_symbol` so it can't drift from the
  orchestrator.
- New pure helper `components._rule_chip_items(parsed)` normalises **both**
  shapes: a `dict` passes through; a `list` is cross-referenced against
  `LAYER_A_RULE_NAMES` so absent (failed) rules render as ✗ instead of
  vanishing. `rule_chip_grid` parses → normalises → falls back to its existing
  captions on `JSONDecodeError`/`TypeError`.
- TDD (red→green): `tests/test_ui_components.py` (dict shape; list cross-ref with
  a 9/10 case; all-passed list; bad-type → `TypeError`) + the rules-order test.
  Verified against the live DB — the list payload that crashed now normalises to
  10/10 for the day's selected signals. Full suite green, ruff clean.

**Still open (gap):** the writer is **lossy** — it drops failed-rule names and
every rule's `reason` at write time, so the dashboard can show *which* rules
failed but never *why* (the `RuleResult.reason`, e.g. "RSI=63.5 outside
[30,45]"). Surfacing reasons needs either storing the full
`{name: {passed, reason}}` map in `rules_passed_json` or a side table — tracked
as a follow-up to [[F-027]] (centralise the rule-payload contract + a
render→store→parse round-trip test spanning `pre_open` and the UI).

---

### F-035 — No self-healing for stale/missing macro data (`GAP`, Med, Phase 6) — ✅ Fixed 2026-06-19
Spun off from F-026. When the macro snapshot is stale or missing, the system can
only **refuse** (`compile_brief` → `StaleBundleError`) or **degrade** (render
`_(no data)_`) — nothing re-pulls it. The remedy is the operator manually
re-running `trading brief assemble-context`. A self-healing system would, on
detecting stale/missing macro, re-invoke the **data layer** to refresh it and
write a new, reproducible bundle.
- **Why data-layer, not LLM:** having the analyst LLM pull/patch figures fuses
  ingestion into analysis (violates the decoupling rule), breaks bundle
  reproducibility/audit, and is unreliable for precise numbers. The refresh must
  be deterministic ingestion code.

**Resolution (Phase 1 of the macro self-healing spec).** `trading macro` is now a
command group:
- `trading macro snapshot` — the former flat `trading macro` (pull + classify +
  upsert), unchanged behaviour.
- `trading macro refresh --date <d> [--cross <file>]` — deterministically
  re-pulls (`snapshot_and_classify`) and upserts `macro_snapshot`. With `--cross`
  it reads a validated Kite MCP second-source file
  (`data/raw/<date>/macro_cross_HHMM.json` → `read_macro_cross` /
  `MacroCrossSource`, F-002-style boundary) and **gap-fills only fields still
  `None` after the re-fetch** (VIX, USDINR), recording each fill in the new
  `macro_reconciliation` provenance table (migration v4, `missing_primary`
  status). A figure the primary feed already supplied is never overwritten.
- **Decoupling preserved:** `compile_brief` stays network/DB-free; refresh lives
  in ingestion, with F-026's `StaleBundleError` as the compile-time backstop.

The `/macro-doctor` skill that orchestrates the Kite pull and the cross-source
*verification* (vs. gap-fill) of figures the primary feed did supply is **F-036**
(phase 2). See `docs/superpowers/specs/2026-06-18-macro-self-healing-reconciliation-design.md`.

### F-036 — Single-source macro figures, no cross-verification (`GAP`, Med, Phase 6)
Spun off from F-026. VIX/USDINR/FII/DII come from one provider; a wrong upstream
value enters the bundle unflagged. F-026's figure check only verifies the
**brief against the bundle** — it cannot catch a bundle that is itself wrong.
- **Fix idea:** at ingestion, fetch the headline macro figures from a second
  trusted, structured source (e.g. yfinance `^INDIAVIX` / `USDINR=X`, NSE
  provisional FII/DII) and reconcile: store the value plus a discrepancy/confidence
  flag when sources disagree beyond tolerance. `assemble_context` surfaces the
  flag in the macro snapshot; F-026's warn then upgrades from "the LLM may have
  invented this" to "the bundle itself flagged this figure as unreconciled."
- **Phase-1 infrastructure already landed (F-035):** the `macro_reconciliation`
  table (migration v4) and the validated `MacroCrossSource` reader exist. F-036
  adds the pure `reconcile_macro` tolerance core, `trading macro verify`
  (exit-1 on mismatch), `context._render_macro` annotation of flagged figures,
  and the `/macro-doctor` skill that pulls the Kite second source read-only.
- **Out of scope:** LLM web-search verification of precise figures (unreliable —
  a hallucination vector, not a fix). Verification must be against structured
  feeds.

---

_Counts: 14 open · 1 superseded · 21 fixed (F-002, F-003, F-012, F-013, F-014,
F-015, F-016, F-018, F-019, F-021, F-022, F-023, F-024, F-025, F-026, F-029,
F-031, F-032, F-033, F-034, F-035). Updated 2026-06-19 (F-035 — macro
self-healing phase 1: `trading macro refresh` re-pulls + upserts the snapshot and
`--cross` gap-fills still-missing VIX/USDINR from a validated Kite MCP second
source with `macro_reconciliation` provenance, migration v4; `/macro-doctor`
orchestration + cross-verify of supplied figures → F-036)._
