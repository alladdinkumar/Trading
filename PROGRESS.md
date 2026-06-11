# Build Progress

> **Source of truth:** [docs/superpowers/specs/2026-05-11-trading-system-design.md](docs/superpowers/specs/2026-05-11-trading-system-design.md)
> **Session bootstrap:** [working-prompt.md](working-prompt.md)

Granular task tracker for the trading system build. Update as work completes.

## Legend

- `[ ]` — pending
- `[~]` — in progress
- `[x]` — done
- `[!]` — blocked (note the blocker inline)

## Status snapshot

| | Phase | State |
|---|---|---|
| 0 | Project setup | `[x]` |
| 1 | Config + SQLite schema | `[x]` |
| 2 | Historical OHLCV (yfinance) | `[x]` |
| 3 | Kite MCP wrapper | `[x]` |
| 4 | Technical indicators | `[x]` |
| 5 | Rule scanner (Layer A) | `[x]` |
| 6 | Sizing + exits | `[x]` |
| 7 | Backtest engine | `[x]` |
| 8 | News + sentiment | `[x]` |
| 9 | Macro + regime | `[x]` |
| 10 | Portfolio analyzer | `[x]` |
| 11 | Paper-trade ledger | `[x]` |
| 12 | LLM analyst | `[x]` |
| 12.5 | Data quality cleanup | `[x]` |
| 12.6 | Sector data | `[x]` |
| 13 | pre_open job (MVP ⭐) | `[x]` |
| 13.5 | Kite MCP pivot | `[x]` |
| 14 | mid_day + post_close jobs | `[x]` |
| 14.A | mid_day MVP | `[x]` |
| 14.B | post_close MVP | `[x]` |
| 14.C | pre-open IEP gap filter | `[x]` |
| 15 | Streamlit dashboard | `[x]` |
| 16 | LightGBM ranker (Layer B) | `[x]` |
| 17 | Task Scheduler + logging | `[x]` |
| 18 | Live paper-trading (ongoing) | `[~]` |

**Currently working on:** _Phase 18 — live paper-trade run started 2026-06-11 (day 1: pre-open + IEP + brief done; 3 candidates, 0 all-pass, 0 trades opened — RISK_OFF day)_
**Next up:** _Daily run cadence per docs/daily-workflow.md; weekly_train + monthly_sip tooling (deferred from Phase 17) needed for 18.3/18.4_

---

## Phase 0 — Project Setup

- [x] 0.1 `uv init` in repo root; pin Python 3.11 via `.python-version`
- [x] 0.2 Author `pyproject.toml` with prod + dev dependency groups (see spec Section 12)
- [x] 0.3 Configure `ruff` rules in `pyproject.toml`
- [x] 0.4 Configure `mypy` (strict on `src/trading/`)
- [x] 0.5 Configure `pytest` (markers: `live`, `integration`, `slow`)
- [x] 0.6 Create `src/trading/` module tree per spec Section 11 (empty `__init__.py` files)
- [x] 0.7 Create `tests/` with `conftest.py` and `fixtures/` placeholder
- [x] 0.8 Author `.env.example` with all keys from spec Section 12
- [x] 0.9 Extend `.gitignore` for `data/app.db`, `data/parquet/`, `data/cache/`, `.env`, `models/*.pkl`
- [x] 0.10 Verify clean: `pytest -q` · `ruff check .` · `mypy src/`
- [x] 0.11 Update PROGRESS.md → commit `chore: scaffold project`

## Phase 1 — Config + SQLite schema

- [x] 1.1 `src/trading/config.py`: load `.env`, expose `Paths` dataclass + constants (TZ=`Asia/Kolkata`)
- [x] 1.2 Tests: env loading, path resolution
- [x] 1.3 `src/trading/store/db.py`: `get_conn()` context manager, foreign keys ON
- [x] 1.4 `src/trading/store/migrations.py`: v1 schema for all 16 tables (signals, paper_trades, predictions, portfolio_snapshots, news_items, sentiment_daily, sector_daily, macro_snapshot, oi_daily, fno_ban_list, bulk_block_deals, corp_actions, account_events, preopen_snapshot, live_quotes, event_calendar)
- [x] 1.5 `src/trading/store/repo.py`: typed CRUD helpers for signals, paper_trades, predictions
- [x] 1.6 Tests: round-trip insert/select per table; foreign-key integrity
- [x] 1.7 Lint + type-check + tests green
- [x] 1.8 Update PROGRESS.md → commit `feat(store): SQLite schema v1`

## Phase 2 — Historical OHLCV (yfinance)

- [x] 2.1 `src/trading/data/yfinance.py`: `fetch_ohlcv(symbol, start, end)` returning a typed DataFrame
- [x] 2.2 `src/trading/store/ohlcv.py`: parquet read/write per symbol (`data/parquet/nifty200/SYMBOL.parquet`)
- [x] 2.3 `src/trading/data/cache.py`: `requests-cache` setup for HTTP fetchers
- [x] 2.4 Bulk script `src/trading/cli.py::ingest_history`: fetch 3+y for universe (initial scope: Nifty 50 + holdings ~59 symbols; expandable via `data/static/universe.txt`)
- [x] 2.5 Tests: fixture round-trip; schema validation; CLI integration
- [x] 2.6 Smoke-test ingest: 3 symbols (RVNL, RELIANCE, NTPC) for Jan-Feb 2025, 129 bars written to parquet
- [x] 2.7 Update PROGRESS.md → commit `feat(data): historical OHLCV ingestion`

## Phase 3 — Kite MCP / Connect wrapper

- [x] 3.1 `src/trading/data/kite.py`: thin wrapper over `kiteconnect` SDK — `get_holdings`, `get_positions`, `get_gtts`, `get_quotes`, `get_ltp`, `get_margins`
- [x] 3.2 Daily token-rotation handler: `KiteAuthError` raised on `TokenException`; `trading kite-login` CLI command writes fresh token to `.env`
- [x] 3.3 Typed return shapes — frozen `@dataclass` per resource (Holding, Position, GttOrder, Quote, Margin)
- [x] 3.4 Tests: 15 mocked-SDK tests verify shape mapping + auth-error propagation
- [x] 3.5 `@pytest.mark.live` integration: `test_live_get_holdings_against_real_kite` (self-skips when token missing)
- [x] 3.6 Update PROGRESS.md → commit `feat(data): Kite Connect wrapper`

## Phase 4 — Technical indicators

- [x] 4.1 `src/trading/features/technicals.py`: wrappers over the `ta` library (Phase 0 swap) for RSI, MACD, ATR, EMA, SMA, BB, ADX, VWAP, OBV, returns
- [x] 4.2 `add_indicators(df)` enriches an OHLCV DataFrame with the full default suite
- [x] 4.3 Tests: 22 covering exact reference values (SMA, returns), NaN-warmup patterns, monotonic RSI bounds, MACD identity (signal = EMA of line), BB ordering, ATR non-negative, ADX bounds, OBV directionality, add_indicators preserves originals
- [x] 4.4 Smoke test on RELIANCE parquet: indicators computed cleanly; PROGRESS.md updated → commit `feat(features): technical indicators`

## Phase 5 — Rule scanner (Layer A)

- [x] 5.1 `src/trading/strategy/rules.py`: function per Section 4.1 filter — `passes_uptrend`, `passes_pullback`, `passes_rsi_band`, `passes_volume_exhaustion`, `passes_liquidity`, `passes_no_recent_breakdown`, `passes_regime`, `passes_not_fno_banned`, `passes_not_t2t`, `passes_no_critical_event`
- [x] 5.2 `scan()` orchestrator + `evaluate_symbol()` — loads enriched parquet, runs all rules, returns `Candidate(rules=...)`. Skips symbols with <200 bars (SMA-200 needs the history).
- [x] 5.3 CLI `trading scan --date YYYY-MM-DD [--show-all] [--json]` — Rich table and JSON output
- [x] 5.4 Tests: 32 covering pass/fail per rule, history-insufficient handling, context-empty defaults, scan orchestrator skipping behavior, CLI happy path
- [x] 5.5 Smoke test: live scan of 5 PSU/infra symbols (RVNL, RELIANCE, NTPC, TATAPOWER, IRB) over 2yr+ history — all failed `uptrend` filter (correct: late Feb 2025 was a correction). PROGRESS.md updated → commit `feat(strategy): rule scanner`

## Phase 6 — Sizing + exits

- [x] 6.1 `src/trading/strategy/sizing.py`: `position_size(SizingInput)` returns `SizingResult` with risk budget × regime multiplier (RISK_ON/NEUTRAL/RISK_OFF), per-stock cap (≤25%), per-sector cap (≤30%). Floors qty at 0, names the binding constraint in `reasons`.
- [x] 6.2 `src/trading/strategy/exits.py`: `evaluate_exit(trade, bar)` returns `ExitDecision(action, new_stop, exit_price, reason)`. Pure function — one bar at a time; caller threads `new_stop` and bumps `days_held`.
- [x] 6.3 Branches: EXIT_STOP (`low ≤ current_stop`), EXIT_TARGET (`high ≥ min(+20%, 2.5×R/R)`), EXIT_TIME (`days_held ≥ 25 ∧ close ≤ entry`), HOLD + trail (+10% → breakeven, +15% → close − 1×ATR). Stop wins same-bar tie (spec §8 conservative fills).
- [x] 6.4 Tests: 19 sizing (formula, regime, stock/sector caps, validation, edge cases) + 22 exits (each branch, both target paths, trail ratchet, never-lowers, tie-break, full-winner-sequence) = 41 new tests.
- [x] 6.5 Smoke test on RELIANCE: at ₹1195 + 1.5×ATR stop, 25%-cap binds (qty=20, capital-at-risk only ₹646), R/R target +6.8% fires on +10% bar — confirms tight-ATR-stop ⇒ small-but-frequent wins by design. PROGRESS.md updated → commit `feat(strategy): sizing + exits`

## Phase 7 — Backtest engine

- [x] 7.1 `src/trading/backtest/costs.py`: Zerodha-accurate cost model — brokerage cap ₹20, STT 0.1% × 2, exchange 0.00297%, SEBI 0.0001%, stamp duty 0.015% (buy only), GST 18%, slippage 0.1% per side as price shift. Round-trip drag ~0.4%.
- [x] 7.2 `src/trading/backtest/engine.py`: **event-loop backtester** (deviation from spec §8.1 vectorbt — see plan). Reuses Phase 5 scanner + Phase 6 sizing/exits unchanged. EOD signal → next-day open fill with slippage. `SignalProvider` extension point makes the engine testable in isolation and slots in Phase 16's ranker.
- [x] 7.3 `src/trading/backtest/walkforward.py`: rolling 3y / 6mo / 3mo windows. `run_walkforward` runs the engine per test fold, concatenates trades. Train slice unused for rules-only baseline (Phase 16 hook).
- [x] 7.4 `src/trading/backtest/metrics.py`: CAGR, Sharpe, Sortino, max DD, hit rate, profit factor, expectancy, avg R-multiple, alpha/beta vs benchmark. `MetricsBundle` aggregator; safe on empty/zero-variance inputs.
- [x] 7.5 CLI `trading backtest --start --end --capital --risk-pct --report`: enriches all parquet symbols, runs backtest, prints Rich table + writes markdown to `data/research/backtest_<ts>.md`.
- [x] 7.6 Tests: 9 cost (components, cap, slippage, round-trip), 18 metrics (each stat + edge cases), 9 engine (signal pipeline, exits, costs, cash conservation, OPEN_AT_END), 5 walk-forward (window enumeration + integration) = 41 new. All 223/223 tests green, 1 skipped live.
- [x] 7.7 Smoke run on RVNL/RELIANCE/NTPC/TATAPOWER/IRB universe (2023-10 → 2025-02, ₹5L capital, 2% risk): 22 trades, 40.9% hit rate, PF 1.91, Sharpe 0.87, max DD −5.0%, final equity ₹526,526 (+3.7% CAGR), costs ₹1,723 (0.34% drag). All trades exit cleanly via STOP/TARGET/TIME.
- [x] 7.8 PROGRESS.md updated → commit `feat(backtest): engine + walk-forward (Phase 7)`

## Phase 8 — News + sentiment

- [x] 8.1 `src/trading/data/news.py`: RSS aggregator (Moneycontrol/ET/BS) via `feedparser` + NSE event calendar via `nsepython.nse_events`. `NewsSource` Protocol; orchestrator dedupes by URL and isolates failing sources (spec §18). Symbol attribution via whole-word case-insensitive alias map (`DEFAULT_ALIASES`, holdings + smoke universe).
- [x] 8.2 `src/trading/features/sentiment.py`: lazy singleton FinBERT (`ProsusAI/finbert`, cached under `data/cache/models/`); `score_headline` returns `ScoreResult(score ∈ [-1,+1], category, is_critical)`. Score = `P(pos) − P(neg)`. Injectable `Scorer` callable keeps unit tests fast (stubbed) and one `@slow` test exercises the real ~440MB model.
- [x] 8.3 `CRITICAL_PATTERNS` regex set (SEBI/SAT/fraud/auditor/pledge/qualified opinion/NCLT/ED/CBI/default/insolvency) and `CATEGORY_KEYWORDS` (results/management/regulatory/M&A/downgrade/dividend/pledge). Critical → hard veto for Layer A §4.1.
- [x] 8.4 `aggregate_symbol(conn, sym, as_of)` + `aggregate_daily(conn, symbols, as_of)` write per-(date, symbol) rollup to `sentiment_daily`: 7d/30d mean score, news_count, negative_news_count (< -0.20), has_critical. Idempotent UPSERT; skips symbols with zero news.
- [x] 8.5 56 new tests across `test_news.py` (14), `test_sentiment.py` (34 — incl. one `@slow` real-FinBERT directional snapshot), `test_news_store.py` (8). Cached XML fixtures under `tests/fixtures/news/`. Full suite **279 passed**, 2 deselected (1 live, 1 slow).
- [x] 8.6 CLI `trading ingest-news [--date YYYY-MM-DD] [--skip-score] [--skip-aggregate]`. Smoke test against live RSS: 511 headlines pulled + deduped + persisted in ~5s with `--skip-score`. PROGRESS.md updated → commit `feat(features): news + sentiment (Phase 8)` and push to origin/main.

## Phase 9 — Macro + regime

- [x] 9.1 `src/trading/data/macro.py`: yfinance fetchers for S&P/Nasdaq/Dow (^GSPC/^IXIC/^DJI), USDINR (INR=X), Brent (BZ=F), US 10y (^TNX), India VIX (^INDIAVIX). `fetch_yf_quote` returns latest close + 1-day pct change; tolerates multi-index columns, rate-limits, weekends (`lookback_days=10`).
- [x] 9.2 `fetch_fii_dii()` wraps `nsepython.nse_fiidii` with graceful degradation. Tolerant of modern (`category`/`netValue`) **and** legacy (`type`/`netVal`) schemas; falls back to `(None, None)` on import error, unknown shape, or empty response.
- [x] 9.3 `src/trading/features/regime.py`: pure 4-axis voter (VIX / global-futures mean / FII flow / USDINR change). Each axis votes -1/0/+1 with named thresholds (`VIX_LOW`, `VIX_HIGH`, `FUTURES_UP/DOWN_PCT`, `FII_UP/DOWN_CR`, `USDINR_*`). Sum ≥+2 → RISK_ON, ≤-2 → RISK_OFF, else NEUTRAL. Returns per-axis votes + reasons; `position_size_multiplier` returns 1.0 / 0.75 / 0.5 per spec §4.5.
- [x] 9.4 `snapshot_and_classify(as_of)` fetches once, builds `MacroSnapshot`, runs classifier, returns row with `regime` filled in. `upsert_macro_snapshot` (`store/macro_store.py`) does idempotent INSERT ON CONFLICT. CLI `trading macro [--date YYYY-MM-DD] [--dry-run]` prints the Rich table + regime + reasoning, then UPSERTs.
- [x] 9.5 42 new tests across `test_regime.py` (25 — per-axis voters, every bucket boundary, position-size policy, adapter) and `test_macro.py` (17 — mocked-yfinance shape, multi-index handling, FII/DII modern+legacy parsing, import-failure paths, snapshot orchestration, DB round-trip + REGIME CHECK constraint). Suite **321 passed**, 2 deselected.
- [x] 9.6 Live smoke confirmed: today's snapshot persisted (VIX 19.4, USDINR 95.76, FII +Rs 187 cr → NEUTRAL, score +1). PROGRESS.md updated, commit `feat(features): macro + regime (Phase 9)`, pushed to origin/main.

## Phase 10 — Portfolio analyzer

- [x] 10.1 `src/trading/portfolio/health.py`: vote-based HOLD/TRIM/EXIT scorer over technicals (above 200-DMA, drawdown bands, RSI bands, dist to 52w high), fundamentals (profit growth/CAGR, debt/equity, ROE, P/E percentile) and sentiment. `has_critical` triggers immediate EXIT veto. Pure function over a `HoldingContext` dataclass; all fundamentals fields Optional so missing yfinance data degrades gracefully. `technicals_from_history` adapter computes the technical slice from an enriched OHLCV frame.
- [x] 10.2 `src/trading/portfolio/gtt.py`: `simulate_target_hit` runs n-path GBM with Itô-corrected drift (`µ − ½σ²`), direction-aware hit detection (above-start → ≥, below-start → ≤), deterministic with optional seed. `project_gtt_viability` uses 60-day realised log-return mean/std; gracefully notes when history < `vol_window + 1` bars or σ=0. `project_all_gtts` orchestrates across the holdings universe.
- [x] 10.3 `src/trading/portfolio/allocator.py`: pure `allocate_sip` splits ₹1L across TOPUP (HOLD-rated existing only) / NEW (non-holdings) / CASH. Caps from spec §4.4 + §7.3: 50% topup, 50% new, 60% deployed total, 25% per-stock, 30% per-sector, 5 concurrent open. Greedy priority-ordered fill; rounds to whole rupees; surfaces a `skipped` list with reasons (bucket exhausted / concurrency / sector cap).
- [x] 10.4 `trading portfolio [--horizon-days] [--n-paths] [--seed] [--report]` CLI: pulls live Kite holdings + GTTs, scores health using parquet history + `sentiment_daily`, projects GTT viability, prints Rich tables + writes markdown to `data/research/portfolio_<ts>.md`.
- [x] 10.5 64 new tests across `test_health.py` (24 — per-axis voters, critical veto, low-evidence default, threshold constants, score formula, `technicals_from_history`), `test_gtt.py` (18 — seed determinism, directional hits, vol→prob relationship, edge cases, graceful skips, orchestrator), `test_allocator.py` (22 — bucket routing, concurrency/sector/per-stock caps, priority ordering, cash reserve, whole-share rounding, determinism). Suite **385 passed**, 2 deselected.
- [x] 10.6 End-to-end synthetic smoke ran cleanly (RVNL HOLD 100/100, GTT P(hit)=31% in 32 expected days, SIP allocates 24.5k NEW + 75.5k CASH). PROGRESS.md updated → commit `feat(portfolio): health + GTT + allocator (Phase 10)` and pushed to origin/main.

## Phase 11 — Paper-trade ledger

- [x] 11.1 `src/trading/paper/ledger.py`: `log_signal`, `open_trade`, `close_trade`, `list_open`, `mark_to_market`
- [x] 11.2 `src/trading/paper/mtm.py`: pulls live LTP via Kite, applies exit logic, writes closes
- [x] 11.3 `src/trading/paper/reconcile.py`: daily P&L; predicted vs actual accuracy
- [x] 11.4 Tests: full lifecycle in-memory SQLite
- [x] 11.5 Update PROGRESS.md → commit `feat(paper): ledger + MTM + reconcile — Phase 11`

## Phase 12 — LLM analyst (Claude Code skill version)

> **Spec deviation:** the original Anthropic-SDK plan was replaced by a
> project-level Claude Code skill (`/analyst`). User has Claude Pro plan
> but no API credits — paying twice for the same model wasn't worth it.
> Full rationale and design in
> [`docs/superpowers/specs/2026-05-15-phase-12-llm-skill-design.md`](docs/superpowers/specs/2026-05-15-phase-12-llm-skill-design.md).

- [x] 12.1 `src/trading/llm/context.py`: `ContextInputs` dataclass +
       `assemble_context(conn, paths, as_of, mode, inputs)` writes
       `_context.md` from DB (`macro_snapshot` / `sentiment_daily` /
       `news_items` / `paper_trades` / `predictions`) + ephemeral inputs
       (`candidates` from scanner, `holdings_health` from portfolio analyzer).
       Pure renderer; mode-conditional matured-predictions section. Empty
       sources render as `_(no data)_` so the skill flags gaps explicitly.
- [x] 12.2 `.claude/skills/analyst/SKILL.md` + `references/output-templates.md`:
       project-level skill that reads `_context.md`, refuses if stale > 12 h,
       writes `macro_brief.md`, `sector_commentary.md`,
       `candidates/{SYMBOL}.md`, and (post_close) `post_close_recap.md`.
- [x] 12.3 `src/trading/llm/briefing.py`: `compile_brief(date_dir, mode)` +
       `expected_parts(mode, candidate_symbols)` + `MissingNarrativeError`.
       Concatenates parts into `brief.md` in fixed order; orphan candidate
       files printed to stderr as warnings.
- [x] 12.4 21 new tests across `test_llm_context.py` (12 — per-section
       populated/empty + 2 syrupy bundle snapshots), `test_llm_briefing.py`
       (6 — `expected_parts` for both modes, missing-parts raise, orphan
       warning, 2 syrupy compile snapshots), `test_cli.py` (2 happy-path).
       Suite **442 passed**, 1 skipped, 1 warning.
- [x] 12.5 N/A — cost tracking dropped (Claude Pro plan, no per-call cost).
- [x] 12.6 PROGRESS.md updated → commit `feat(llm): analyst skill + briefing
       pipeline (Phase 12)` and pushed to origin/main.

## Phase 12.5 — Data quality cleanup (pre-Phase-13 prep)

> Surfaced by the Phase 12 real-data smoke (memory:
> `project_data_quality_gaps_2026_05_15`). Spec at
> [`docs/superpowers/specs/2026-05-15-phase-12-5-data-quality-design.md`](docs/superpowers/specs/2026-05-15-phase-12-5-data-quality-design.md).

- [x] 12.5.1 `src/trading/store/ohlcv.py`: `_drop_trailing_nan_close` strips
       yfinance's current-day NaN-OHLC stub row at the storage boundary;
       interior NaN preserved. 4 new tests in `test_ohlcv_store.py`.
       Smoke impact: COALINDIA jumped from 5/10-with-NaN to 8/10-clean
       (now actually passes uptrend); RECLTD 7/10→9/10; IDFCFIRSTB and
       MAZDOCK now appear in top 5.
- [x] 12.5.2 `src/trading/llm/context.py`: `_render_news_for_symbol` SQL
       caps `ts <= as_of end-of-day` so NSE event-calendar entries with
       future event dates don't leak into "Recent headlines". 1 new test;
       snapshots stayed valid (seeded data still in window).
- [x] 12.5.3 Ran `trading ingest-news --date 2026-05-15` (without
       `--skip-score`); 556 headlines scored. **Acceptance partial:**
       only 1 sentiment_daily row written (JIOFIN), because alias-map
       attribution covers only 6 of 1067 news_items, and event entries
       are duplicated (ingest dedupe gap). Both flagged as Phase 13 prep
       items (out of scope for 12.5 per spec §4).
- [x] 12.5.4 `src/trading/llm/briefing.py`: split `expected_parts` into
       `required_parts` + `optional_parts`; `compile_brief` substitutes
       a hardcoded placeholder body for missing optional parts. SKILL.md
       updated to mark sector_commentary as optional. 4 new tests +
       1 updated; snapshots unchanged (parts_count arithmetic identical).
- [x] 12.5.5 Real-data smoke confirms candidate section now shows real
       closes/SMAs (no NaN), no future-dated headline leakage, and the
       sector_commentary placeholder substitutes cleanly when absent.
       Suite **449 passed**, 1 skipped (live), ruff + mypy clean.
       Commit `feat(data): Phase 12.5 quality fixes` pushed to origin/main.

## Phase 12.6 — Sector data

> Spec at [`docs/superpowers/specs/2026-05-26-phase-12-6-sector-data-design.md`](docs/superpowers/specs/2026-05-26-phase-12-6-sector-data-design.md).
> Plan at [`docs/superpowers/plans/2026-05-26-phase-12-6-sector-data.md`](docs/superpowers/plans/2026-05-26-phase-12-6-sector-data.md).
> 11 NSE sectoral indices via yfinance, RS vs Nifty 50, wired into pre_open + pre_open_iep + assemble_context.

- [x] 12.6.1 `src/trading/data/sector.py`: 11-sector ticker dict +
       `^NSEI` benchmark + simple-difference `compute_rs` + 5d/20d/60d
       windows + LEADING/NEUTRAL/LAGGING regime thresholds on rs_20d
       (±2%). Defensive yfinance wrapper (`fetch_sector_history`) +
       `fetch_all_sectors(as_of)` orchestrator that skips failed
       tickers + `load_sector_map(paths)` CSV reader. 12 tests in
       `test_data_sector.py`.
- [x] 12.6.2 `src/trading/store/sector_store.py`: `upsert_sector_daily`
       (INSERT ON CONFLICT(date,sector) DO UPDATE, executemany) +
       `get_sector_daily(conn, as_of)` reader. 3 tests in
       `test_store_sector.py`.
- [x] 12.6.3 `data/static/sector_map.csv`: symbol→sector map for 57
       universe symbols (Nifty 50 + holdings). Comment lines tolerated;
       symbols not listed treated as no-sector by pre_open_iep.
- [x] 12.6.4 `src/trading/jobs/pre_open.py`: `_step_sector` inserted
       between `_step_macro` and `_step_news`; graceful degradation
       (warning, returns False) on fetch failure. `PreOpenResult.sector_written`
       added; CLI table renders it. 3 tests in `test_jobs_pre_open.py`.
- [x] 12.6.5 `src/trading/jobs/pre_open_iep.py`: when `sector_map=None
       and sector_momentum=None`, auto-load via `load_sector_map` +
       `get_sector_daily(as_of)` with D-1 fallback. Passing `{}`
       explicitly suppresses auto-load. 4 tests in
       `test_jobs_pre_open_iep.py`.
- [x] 12.6.6 `src/trading/llm/context.py`: `_render_sector_snapshot`
       section between macro and candidates; per-candidate `sector: CODE
       — 20d RS …` bullet rendered when symbol is in sector_map AND
       sector_daily. 4 new tests + snapshot re-record.
- [x] 12.6.7 `briefing.py` SECTOR_COMMENTARY_PLACEHOLDER reworded to
       "analyst did not write a sector commentary for this run".
       `.claude/skills/analyst/SKILL.md` updated: section is optional;
       write when bundle's `## Sector momentum` is non-empty.
- [x] 12.6.8 `trading sector --date YYYY-MM-DD [--dry-run]` CLI: live
       fetch + Rich table + upsert. Exit 1 if zero rows fetched.
       `trading pre-open` table extended with `sector_written` row.
       3 tests in `test_cli.py`.
- [x] 12.6.9 Real-data smoke (2026-06-11): `trading sector` pulled
       11 sectors (FINSERV RS gracefully None); `trading pre-open`
       showed `sector_written: yes` and a populated `## Sector momentum`
       section + per-candidate sector bullets; `trading pre-open-iep`
       ran RISK_OFF filter with sector axis active (no "Sector data
       unavailable" warning). Full suite green, ruff + mypy clean.
       Commit `feat(data): sector daily + RS (Phase 12.6)` pushed to
       origin/main.

## Phase 13 — pre_open job (E2E) ⭐ MVP milestone

> Spec at [`docs/superpowers/specs/2026-05-15-phase-13-pre-open-design.md`](docs/superpowers/specs/2026-05-15-phase-13-pre-open-design.md).
> Plan at [`docs/superpowers/plans/2026-05-15-phase-13-pre-open.md`](docs/superpowers/plans/2026-05-15-phase-13-pre-open.md).

- [x] 13.1 `src/trading/jobs/pre_open.py`: `run_pre_open(as_of, ...)` orchestrator
       + `PreOpenResult` dataclass + 6 private `_step_*` helpers (`_step_macro`
       / `_step_news` / `_step_scan` / `_step_portfolio` / `_step_auto_open`
       / `_step_assemble`). In-process invocation of every upstream phase
       (1-12); each step degrades gracefully when its data source is
       unavailable (yfinance down → empty macro; Kite token absent → empty
       holdings; RSS down → no news inserted).
- [x] 13.2 Bundle written to `data/research/YYYY-MM-DD/_context.md` via
       Phase 12's `assemble_context`. CLI prints next-step instruction
       (run `/analyst`, then `trading brief compile`). Re-runnable
       safely — DB upserts + idempotent file writes.
- [x] 13.3 Auto-log: `_step_auto_open` opens one paper-trade per all-pass
       candidate at D-1's close (per spec §4.4 'limit order at close').
       Position sizing per Phase 6 with regime multiplier from `_step_macro`.
       `_already_opened_today` guard prevents duplicate paper-trades on
       re-run for the same date.
- [x] 13.4 `scripts/pre_open.bat` Windows launcher invoking
       `uv run python -m trading.jobs.pre_open <date>`. Phase 17 will
       wire this into Task Scheduler.
- [x] 13.5 13 unit tests in `test_jobs_pre_open.py` (per-step + idempotency
       + 1 end-to-end integration test on synthetic parquet) + 1 CLI
       happy-path test. All offline / deterministic.
- [x] 13.6 Manual smoke run on real data (2026-05-15): macro_written yes,
       604 news inserted, 12 candidates evaluated, 0 passing (correction
       day — every candidate fails uptrend), 0 paper-trades opened.
       Bundle written cleanly with no NaN closes (Phase 12.5.1 working).
       Idempotency confirmed on re-run.
- [x] 13.7 PROGRESS.md updated → commit `feat(jobs): pre_open end-to-end
       (MVP) (Phase 13)` and pushed to origin/main.

## Phase 13.5 — Kite MCP pivot

> Reverses Phase 13's `--skip-kite` design. Production paths read Kite data
> from JSON files written by a `/kite-snapshot` Claude Code skill. SDK
> wrapper at `src/trading/data/kite.py` is kept as a manual fallback,
> wired only into `kite-emergency-*` CLI commands.
> Spec at [`docs/superpowers/specs/2026-05-15-phase-13-5-kite-mcp-pivot-design.md`](docs/superpowers/specs/2026-05-15-phase-13-5-kite-mcp-pivot-design.md).
> Plan at [`docs/superpowers/plans/2026-05-15-phase-13-5-kite-mcp-pivot.md`](docs/superpowers/plans/2026-05-15-phase-13-5-kite-mcp-pivot.md).

- [x] 13.5.1 `src/trading/data/kite_snapshot.py`: `read_holdings/gtts/positions`
       readers + `KiteSnapshotMissingError` / `KiteSnapshotStaleError`. Reads
       `data/raw/<as_of>/<resource>.json` + `_meta.json` validation
       (date-equality). 7 new tests in `test_kite_snapshot.py`.
       `seed_kite_snapshot` helper added to `conftest.py`.
- [x] 13.5.2 `.claude/skills/kite-snapshot/SKILL.md`: project-level skill.
       Probes `mcp__kite__get_profile`; on 401 prompts user to run
       `mcp__kite__login` (no partial writes). Calls `mcp__kite__get_holdings`
       / `mcp__kite__get_gtts`; writes JSONs atomically + `_meta.json` with
       `source: "mcp"`.
- [x] 13.5.3 `src/trading/jobs/pre_open.py`: `_step_portfolio` reads from
       `kite_snapshot.read_holdings`. Drops `skip_kite` arg. New
       `PreOpenAborted` exception bubbles to CLI which exits 2 with
       remediation message. 2 new step tests; 1 new CLI abort test.
- [x] 13.5.4 `trading portfolio` CLI: drops SDK calls, reads from
       `kite_snapshot`. New required `--date` option. `KiteSnapshotMissingError`
       exits 2 with same remediation as pre-open. 2 new tests.
- [x] 13.5.5 Renamed `kite-login` → `kite-emergency-login`. Added
       `kite-emergency-snapshot --date` CLI: writes the same JSON contract
       as the skill but tags `_meta.source: "sdk-fallback"`. 2 new tests.
- [x] 13.5.6 Real-data smoke: `/kite-snapshot` invoked via MCP fetched 11
       holdings + 10 GTTs from real Zerodha account; `trading pre-open`
       scored all 11 holdings cleanly (`holdings_scored: 11`); `trading
       portfolio` rendered health table + GTT viability projections
       (COALINDIA HOLD 100/100, PFC HOLD 88/100, GTT P(hit) 88% on
       COALINDIA, 81% on MAZDOCK). Suite **474 passed**, 1 skipped
       (live), ruff + mypy clean. Commit `feat(data): Phase 13.5 Kite
       MCP pivot` pushed to origin/main.

## Phase 14.A — mid_day MVP

> Spec at [`docs/superpowers/specs/2026-05-16-phase-14-a-mid-day-design.md`](docs/superpowers/specs/2026-05-16-phase-14-a-mid-day-design.md).
> Plan at [`docs/superpowers/plans/2026-05-16-phase-14-a-mid-day.md`](docs/superpowers/plans/2026-05-16-phase-14-a-mid-day.md).
> Phase 14 split into 14.A (mid_day), 14.B (post_close), 14.C (pre_open_iep).
> 14.B and 14.C will get their own brainstorm → spec → plan cycles.

- [x] 14.A.1 `src/trading/data/quotes_snapshot.py`: `read_latest_quotes` +
       `QuoteSnapshotMissingError` / `StaleError`. Reads newest
       `quotes_HHMM.json` from `data/raw/<as_of>/`. Filename HHMM is
       single source of truth for capture time; staleness checked
       against wall-clock `datetime.now()`. Tightened regex rejects
       invalid hours/minutes (code-review fix). 7 new tests.
- [x] 14.A.2 `.claude/skills/kite-quotes-snapshot/SKILL.md`: reads
       `_quote_symbols.txt`, calls `mcp__kite__get_quotes`, writes
       `quotes_HHMM.json` atomically, updates `_meta.quotes_at`.
- [x] 14.A.3 `src/trading/jobs/mid_day.py`: `gather_quote_symbols`
       (paper-trades ∪ signals ∪ holdings); `_quotes_to_bars`
       (close=last_price, NOT yesterday's close); `run_mid_day` two-mode
       orchestrator; `_render_mid_day_update` markdown builder;
       `MidDayAborted` + `MidDayResult`. 7 new tests including
       end-to-end EXIT_STOP closure + idempotency.
- [x] 14.A.4 `src/trading/cli.py`: `trading mid-day --date YYYY-MM-DD
       [--apply]` subcommand with Rich summary table + remediation on
       abort. 3 new tests.
- [x] 14.A.5 `src/trading/llm/briefing.py` + `context.py`: `Mode`
       extended to include `"mid_day"`; `compile_brief` includes
       `mid_day_update.md` after candidates section when present
       (additive across all modes). 2 new tests.
- [x] 14.A.6 `scripts/mid_day.bat`: two-step Windows launcher
       (prepare/apply).
- [x] 14.A.7 Real-data smoke: `trading mid-day` prepare → MCP
       `get_quotes` for 11 holdings → `trading mid-day --apply`
       built 11 bars (real intraday OHLC), 0 paper-trades to
       evaluate today (correction days haven't opened any).
       Markdown table rendered cleanly with summary line.
       Suite **493 passed**, 1 skipped (live), ruff + mypy clean.
       Commit `feat(jobs): mid_day MVP (Phase 14.A)` pushed to
       origin/main.

## Phase 14.B — post_close MVP

> Spec at [`docs/superpowers/specs/2026-05-16-phase-14-b-post-close-design.md`](docs/superpowers/specs/2026-05-16-phase-14-b-post-close-design.md).
> Plan at [`docs/superpowers/plans/2026-05-16-phase-14-b-post-close.md`](docs/superpowers/plans/2026-05-16-phase-14-b-post-close.md).
> Reuses 14.A `/kite-quotes-snapshot` skill, `paper.mtm.mtm_open_trades`,
> and `paper.reconcile.reconcile_day` unchanged.

- [x] 14.B.1 `src/trading/jobs/post_close.py`: `PostCloseAborted` +
       `PostCloseResult` + `run_post_close(prepare/apply)` orchestrator;
       `_render_post_close_summary` markdown builder; reuses
       `gather_quote_symbols` + `_quotes_to_bars` from `mid_day`. Calls
       `paper.mtm.mtm_open_trades` for final MTM and
       `paper.reconcile.reconcile_day` for matured predictions +
       portfolio snapshot. 5 new tests including TIME-stop closure +
       idempotent re-run + quiet-day case.
- [x] 14.B.2 `src/trading/cli.py`: `trading post-close --date YYYY-MM-DD
       [--apply] [--cash N]` subcommand with Rich summary table +
       remediation on abort. 3 new tests.
- [x] 14.B.3 `src/trading/llm/briefing.py`: opportunistic include for
       `post_close_summary.md` after `mid_day_update.md` (additive
       across modes). 1 new test verifying ordering.
- [x] 14.B.4 `scripts/post_close.bat`: two-step Windows launcher
       (prepare/apply).
- [x] 14.B.5 Real-data smoke: `trading post-close` prepare wrote
       `_quote_symbols.txt` (11 symbols). Kite MCP session was invalid
       in this session so the smoke reused today's existing
       `quotes_0056.json` (real Kite closes) renamed to a fresh HHMM
       to clear the 30-min staleness gate. `--apply` built 11 bars,
       evaluated 0 open trades (quiet day), wrote
       `portfolio_snapshots[2026-05-16]` (equity ₹100,000, drawdown
       null), rendered `post_close_summary.md` end-to-end. Suite
       **502 passed**, 1 skipped (live), ruff + mypy clean.
       Commit `feat(jobs): post_close MVP (Phase 14.B)` pushed to
       origin/main.

## Phase 14.C — pre-open IEP gap filter

> Spec at [`docs/superpowers/specs/2026-05-16-phase-14-c-pre-open-iep-design.md`](docs/superpowers/specs/2026-05-16-phase-14-c-pre-open-iep-design.md).
> Plan at [`docs/superpowers/plans/2026-05-16-phase-14-c-pre-open-iep.md`](docs/superpowers/plans/2026-05-16-phase-14-c-pre-open-iep.md).
> Runs at 08:55 (5 min before market open) to filter + reorder
> pre_open's candidates by overnight gap and sector momentum
> alignment with today's regime; updates `_context.md` in place.

- [x] 14.C.1 `src/trading/jobs/pre_open_iep.py`: `PreOpenIepAborted` +
       `PreOpenIepResult` + `run_pre_open_iep` orchestrator. Reads
       `_context.md` + Kite quotes + parquet D-1 closes + macro_snapshot
       regime; computes gaps; applies regime + (optional) sector filter;
       reranks survivors by composite score `(gap_norm × 0.6) +
       (sector_pct × 0.4)`; writes updated `_context.md` in place. 19
       unit tests covering gap math, regime / sector filters, percentile
       ranking, rerank ordering, and context parse/rewrite; 4
       integration tests covering missing-context abort, no-candidates
       early return, end-to-end RISK_ON filter+reorder, and graceful
       degradation to NEUTRAL when regime + quotes both missing.
- [x] 14.C.2 `src/trading/cli.py`: `trading pre-open-iep --date
       YYYY-MM-DD` subcommand with Rich summary table; exports added
       to `src/trading/jobs/__init__.py`.
- [x] 14.C.3 `scripts/pre_open_iep.bat`: Windows one-step launcher.
- [x] 14.C.4 Real-data smoke (2026-05-22, mid-session 14:25 IST): MCP
       snapshot wrote 11 holdings + 10 GTTs; `trading pre-open` ran clean
       (12 candidates evaluated, 0 passing, holdings_scored 11); MCP
       quotes for 11 symbols at 14:24 fed `trading pre-open-iep` which
       reordered the 5 rules-passing candidates (NEUTRAL regime → kept
       all; gap rerank applied: JIOFIN, IDFCFIRSTB, COALINDIA, MAZDOCK,
       RECLTD). `mid-day --apply` built 11 live bars and `post-close
       --apply` recorded portfolio_snapshots[2026-05-22] (equity ₹100k,
       0 open trades). Full pipeline 13 → 13.5 → 14.A → 14.B → 14.C
       exercised end-to-end against live data.

## Phase 14 — mid_day + post_close jobs (rollup)

> Superseded by sub-phases 14.A / 14.B / 14.C above — the original
> single-phase plan was split during execution. Items kept for traceability.

- [x] 14.1 `src/trading/jobs/mid_day.py`: live quotes, MTM, kill-switch checks, intraday update — done in 14.A
- [x] 14.2 `src/trading/jobs/post_close.py`: final MTM, prediction eval, reconciliation, post-close brief — done in 14.B
- [x] 14.3 `src/trading/jobs/pre_open_iep.py`: 08:55 pre-open IEP gap filter sub-job — done in 14.C
- [x] 14.4 `scripts/{mid_day,post_close,pre_open_iep}.bat` — done across 14.A/B/C
- [x] 14.5 Integration tests for each job with cached fixtures — covered in 14.A/B/C test suites
- [x] 14.6 PROGRESS.md updates + commits — pushed `feat(jobs): mid_day MVP` (14.A), `feat(jobs): post_close MVP` (14.B), `feat(jobs): pre-open IEP gap filter MVP` (14.C)

## Phase 15 — Streamlit dashboard

> Spec at [`docs/superpowers/specs/2026-05-24-phase-15-streamlit-design.md`](docs/superpowers/specs/2026-05-24-phase-15-streamlit-design.md).
> User-authorized autonomous build on 2026-05-24: "use playwright mcp …
> make recommended choices yourself … do it on your own and test and improve".
> Backtest page deferred to a future follow-up (closed paper-trades currently
> zero — would render an empty page).

- [x] 15.1 `src/trading/ui/data.py`: cached SQLite/parquet/markdown/kite-snapshot readers behind `@st.cache_data(ttl=60)`. Every reader degrades gracefully on missing data (returns `None` / `[]` / empty DataFrame) so pages branch on truthiness instead of catching exceptions.
- [x] 15.2 `src/trading/ui/charts.py`: pure Plotly builders — `equity_curve`, `drawdown_curve`, `candlestick`, `sector_pie`, `pnl_distribution`, `win_loss_donut`, `prediction_calibration`, `regime_history`. Shared dark theme via `_apply_theme`; empty-state figures with grey annotation when input is empty.
- [x] 15.3 `src/trading/ui/components.py`: Streamlit-aware widgets — `regime_badge` (colored chip), `kpi_tile` (st.metric wrapper), `health_chip`, `rule_chip_grid` (10 Layer-A rule pass/fail icons), `empty_state`, `stale_quote_tag`, `sidebar_date_picker`, `section_header`, currency/pct formatters. Plus shared palette re-exports.
- [x] 15.4 `src/trading/ui/Home.py` + 3 sub-pages:
    - Home (Overview) — date picker, regime badge, 4 KPI tiles (Equity / Today / Drawdown / Open trades), equity curve (90 snapshots), macro snapshot table, regime-history step plot, today's brief preview.
    - `pages/1_Portfolio.py` — KPI tiles, holdings table (qty/avg/LTP/P&L/Day%/Weight%), sector pie + top-3 concentration, GTT viability table (Monte-Carlo P(hit) + expected days via `portfolio.gtt.project_all_gtts`), per-symbol candlestick drill-down.
    - `pages/2_Today_Signals.py` — funnel (candidates / signals / opened), regime banner, signals table with R:R, per-signal rule chip grid, per-candidate detail (chart + brief excerpt).
    - `pages/3_Paper_Journal.py` — 5 metric tiles (closed trades / hit rate / profit factor / Sharpe / expectancy), equity curve, open + closed trades tables, win-loss donut + P&L histogram, prediction-calibration scatter.
- [x] 15.5 `.streamlit/config.toml` — dark theme, headless server, runOnSave, gatherUsageStats off. Plus per-file ruff ignore for N999 (Streamlit's filename-based sidebar labels need capitalised + numbered names).
- [x] 15.6 64 new tests: `test_ui_charts.py` (20 unit tests on synthetic data — empty + populated paths for every builder), `test_ui_data.py` (15 tests on data layer with in-memory SQLite + `seed_kite_snapshot` helper, autouse cache-clear fixture), `test_ui_pages.py` (8 `streamlit.testing.v1.AppTest` smoke tests — render-without-exception on empty + seeded DB per page). Suite **566 passed**, 1 skipped (live), ruff + mypy clean.
- [x] 15.7 Real-data visual verification via Playwright MCP: started `streamlit run src/trading/ui/Home.py` on port 8501, navigated to all 4 pages against live data (2026-05-22 snapshot, 11 holdings, 10 GTTs, 2 portfolio snapshots, 2 macro snapshots). Iterated on cosmetic issues (sidebar label, drawdown sign, loss-color delta); all 4 pages render cleanly, holdings/GTT/candlestick all show real Kite data.
- [x] 15.8 PROGRESS.md updated → commit `feat(ui): Streamlit dashboard (Phase 15)` and pushed to origin/main.

## Phase 16 — LightGBM ranker (Layer B)

> Spec at [`docs/superpowers/specs/2026-05-24-phase-16-ranker-design.md`](docs/superpowers/specs/2026-05-24-phase-16-ranker-design.md).
> Plan at [`docs/superpowers/plans/2026-05-25-phase-16-ranker.md`](docs/superpowers/plans/2026-05-25-phase-16-ranker.md).
> 20-feature pilot (technicals + macro + sentiment); labels via Phase 6 exit
> replay; cold-start preserves all rules-passing candidates; soft promotion
> gated by 0.05 walk-forward Sharpe deadband; visibility-only signals row
> persisted for non-selected candidates so the dashboard can surface them.

- [x] 16.1 `src/trading/strategy/ranker_features.py`: `FEATURE_NAMES` (20-tuple
       — setup/trend/volume/macro/sentiment) + `LiveContext` dataclass +
       `build_feature_row(enriched_df, signal_date, live_ctx)`. Pure
       functional, NaN-safe; `_REGIME_ORD` maps RISK_OFF/NEUTRAL/RISK_ON to
       0/1/2 ordinals; 52w high/low via rolling(252, min_periods=1). 19 unit
       tests in `test_ranker_features.py`.
- [x] 16.2 `src/trading/strategy/ranker_labels.py`:
       `label_candidate(enriched_df, signal_date, *, atr_stop_multiple=1.5,
       max_days=25, cost_config=None) -> 1 | 0 | None`. Re-uses Phase 6
       `evaluate_exit` + `apply_slippage` + buy/sell charges so the model
       trains on the exact outcome distribution the live system produces.
       Returns `None` when the forward window is incomplete (truncated at
       enriched-frame end). 5 unit tests in `test_ranker_labels.py`.
- [x] 16.3 `src/trading/store/model_registry.py`: CSV at
       `models/registry.csv` (one row per training run; exactly one row may
       have `active=true`), `save_model(path, model, feature_names)` via
       joblib pickle (model + feature_names) with atomic temp-file writes,
       `register(paths, *, row, promote)` with soft-promotion gate
       (`SHARPE_PROMOTION_DEADBAND = 0.05`; NaN sharpe never promotes;
       requires `row.oos_sharpe > current.oos_sharpe + 0.05`),
       `active(paths) -> ActiveModel | None` for inference, and
       `RegistryFeatureMismatch` for stale-model detection. 7 unit tests
       in `test_model_registry.py`.
- [x] 16.4 `src/trading/strategy/ranker.py`: `ScoredCandidate(candidate,
       ml_score, selected)`; `score_and_filter(candidates, paths, conn,
       as_of, *, k=5)` — cold-start path (no model / missing pkl /
       feature-name mismatch / any IO error) returns
       `ScoredCandidate(c, None, True)` for every candidate; scored path
       loads enriched parquet + macro snapshot + sentiment_daily +
       negative-news count + macro history, builds the feature matrix,
       calls `predict_proba`, marks top-K by score. `RankerSignalProvider`
       slots into the Phase 7 backtest engine via existing
       `signal_provider` extension point. 5 tests across `test_ranker.py`.
- [x] 16.5 `src/trading/strategy/ranker_train.py`: `train_walkforward(...)`
       — iterates `walkforward.windows()` (3y train / 6mo test / 3mo step),
       builds (X, y) per fold from Layer-A all-pass candidates labelled by
       Phase 6 exit replay, fits LightGBM (small-data hyperparameters via
       `_new_lgbm()`: 15 leaves, min_data_in_leaf=10, lr=0.05, n_estimators=200,
       is_unbalance=True, random_state=42; early stopping when n≥50 with
       both classes in the val slice), evaluates OOS per fold (replays
       Phase 6 exits on test slice → fold-level Sharpe + hit rate), and
       fits the final production model on the most-recent train window.
       `InsufficientDataError` raised when the final window has <30
       examples or only one class. 4 tests in `test_ranker_train.py`.
- [x] 16.6 `src/trading/jobs/pre_open.py`: `_step_rank` inserted between
       `_step_scan` and `_step_auto_open`. `_step_auto_open` opens a
       paper-trade only when `sc.selected`; non-selected candidates still
       persist a signals row (with `ml_score`) so the dashboard can
       surface low-scoring rules-passes (visibility-only). New
       `candidates_selected` field on `PreOpenResult`; `RANKER_TOP_K = 5`.
       `_step_assemble` accepts optional `scored` to render the new Layer-B
       brief section. 3 new tests in `test_jobs_pre_open.py`.
- [x] 16.7 `src/trading/cli.py`: `trading train-ranker --start --end
       [--promote] [--report]` runs the walk-forward + soft-promotion
       gate end-to-end, pulling macro_history + sentiment_lookup from
       SQLite; saves the pickle to `models/ranker_YYYY-MM-DD.pkl`, appends
       a row to the registry, and (with `--report`) writes a markdown
       summary to `data/research/ranker_<ts>.md`. `trading ranker-status`
       renders the registry as a Rich table. 3 tests in
       `test_cli_ranker.py`. Pre-open CLI table extended with a
       `candidates_selected` row.
- [x] 16.8 `src/trading/llm/context.py`: `ContextInputs.scored_candidates`
       (optional list); `_render_ranker_section` prints a
       `## Layer B ranker` table (Rank/Symbol/Score/Selected, sorted by
       `ml_score` desc) when scored candidates exist; section omitted
       cleanly otherwise. 3 new tests in `test_llm_context.py`.
- [x] 16.9 Real-data smoke (2026-05-26): `uv run trading train-ranker
       --start 2023-05-01 --end 2026-04-01 --report` trained on 12-symbol
       universe → 203 final examples, `models/ranker_2026-04-01.pkl`
       written, registry row appended (active=false; soft-promotion gate
       declined to activate without measurable OOS Sharpe — first model,
       NaN sharpe). `uv run trading ranker-status` rendered the registry
       cleanly. `uv run trading pre-open --date 2026-05-22` ran the full
       pipeline with the inactive model → cold-start path took over →
       12 candidates evaluated, 0 passing (correction day), 0 selected,
       holdings_scored 11, bundle written with Layer-B section omitted
       (correctly absent — no scored candidates). Suite **657 passed**,
       1 skipped (live), ruff + mypy clean. Commit `feat(strategy):
       LightGBM ranker (Phase 16)` and pushed to origin/main.

## Phase 17 — Task Scheduler + logging

> Spec at [`docs/superpowers/specs/2026-05-24-phase-17-scheduler-logging-design.md`](docs/superpowers/specs/2026-05-24-phase-17-scheduler-logging-design.md).
> Plan at [`docs/superpowers/plans/2026-05-24-phase-17-scheduler-logging.md`](docs/superpowers/plans/2026-05-24-phase-17-scheduler-logging.md).
> Reminder-driven: Task Scheduler fires Slack + toast pings; user runs commands manually. weekly_train + monthly_sip deferred.

- [x] 17.1 `loguru` configuration: `src/trading/ops/logging_setup.py` adds rotating file sink (`data/logs/{job}_YYYY-MM-DD.log`, daily rotation, 60-day retention, gzip), stderr sink, and an ERROR+ Slack sink. Idempotent per job. Wrapped into all 4 job entrypoints (`pre_open`, `pre_open_iep`, `mid_day`, `post_close`).
- [x] 17.2 12 Windows Task Scheduler XML entries under `docs/scheduler/`, one per reminder slot (pre_open ×4, iep ×2, mid_day ×3, post_close ×3). Each runs `uv run trading remind --slot <name>` Mon-Fri at the slot's IST time. weekly_train and monthly_sip deferred (return with Phase 16 / future mini-phase).
- [x] 17.3 Error notification: `src/trading/ops/notify.py` posts to Slack incoming webhook (`SLACK_WEBHOOK_URL`) + Windows toast via `plyer`. Best-effort — never crashes the job. Loguru ERROR sink auto-formats traceback + last 20 log lines into the Slack post.
- [x] 17.4 Manual verification: `trading notify-test` and `trading remind --slot pre_open_scan` both run cleanly. Holiday gate correctly silent-skips on Sunday 2026-05-24; forced Monday dispatch attempted Slack (warns gracefully when webhook unset) + toast. Full automated smoke pending live Slack webhook setup per `docs/operations.md`.
- [x] 17.5 `docs/operations.md` documents Slack setup, Task Scheduler import (bulk-import PowerShell snippet), holiday-list refresh, log inspection paths, and a 5-row troubleshooting matrix.
- [x] 17.6 PROGRESS.md updated → commit `feat(ops): scheduling + logging (Phase 17)` and pushed to origin/main.

## Phase 18 — Live paper-trading + iteration (ongoing)

- [~] 18.1 3-6 month live paper-trade run — **started 2026-06-11.**
       Day 1: full morning pipeline ran clean (kite-snapshot 08:41 →
       pre-open → /analyst → brief compile 09:01 → IEP 09:00). Regime
       RISK_OFF (FII −₹2,125 cr); 3 candidates briefed (COALINDIA HIGH
       8/10, MAZDOCK MEDIUM 8/10, JIOFIN LOW 9/10) — none all-pass, so
       no signals/paper-trades opened. Daily cadence per
       [docs/daily-workflow.md](docs/daily-workflow.md).
- [ ] 18.2 Weekly performance review: hit rate, profit factor, calibration plot
- [ ] 18.3 Quarterly LightGBM retrain (auto via `weekly_train.py` on Sundays)
       — `[!]` blocked on weekly_train tooling deferred in Phase 17.2;
       needs its own brainstorm → spec → plan mini-phase.
- [ ] 18.4 Monthly SIP allocator dry-run vs actual investment decisions
       — monthly_sip reminder slot also deferred in Phase 17.2.
- [ ] 18.5 Decision gate: 3+ months of out-of-sample Sharpe > 1.0 → consider Phase 19 (real-money mode, separate spec)

---

## Commit conventions

- `feat(scope): …` — new functionality
- `fix(scope): …` — bug fix
- `chore: …` — tooling, deps, config
- `test(scope): …` — tests only
- `docs: …` — markdown / spec changes
- `refactor(scope): …` — non-functional code change

## Branch policy

- `main` — last known green state
- Work directly on `main` for solo dev (no PRs); commit after each completed sub-task
