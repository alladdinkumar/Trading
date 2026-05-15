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
| 12.6 | Sector data | `[ ]` |
| 13 | pre_open job (MVP ⭐) | `[ ]` |
| 14 | mid_day + post_close jobs | `[ ]` |
| 15 | Streamlit dashboard | `[ ]` |
| 16 | LightGBM ranker (Layer B) | `[ ]` |
| 17 | Task Scheduler + logging | `[ ]` |
| 18 | Live paper-trading (ongoing) | `[ ]` |

**Currently working on:** _Phase 13 — pre_open job (MVP ⭐)_
**Next up:** _Phase 12.6 — Sector data (deferred)_

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

## Phase 12.6 — Sector data (deferred)

- [ ] 12.6.1 Spec the `data/sector.py` module (NSE sectoral indices via
       nsepython/yfinance, 5d/20d/60d relative strength vs Nifty 200).
- [ ] 12.6.2 Implement + persist into `sector_daily` table.
- [ ] 12.6.3 Wire into `assemble_context` (replace placeholder).
- [ ] 12.6.4 CLI: `trading sector --date YYYY-MM-DD`.
- [ ] 12.6.5 Tests + smoke + commit.

## Phase 13 — pre_open job (E2E) ⭐ MVP milestone

- [ ] 13.1 `src/trading/jobs/pre_open.py`: orchestrate phases 1-12 in correct dependency order
- [ ] 13.2 Generate full daily markdown bundle in `data/research/YYYY-MM-DD/`
- [ ] 13.3 Auto-log fired signals as paper-trades opened at next-day open price
- [ ] 13.4 `scripts/pre_open.bat`: Windows launcher invoking `uv run python -m trading.jobs.pre_open`
- [ ] 13.5 Integration test: full pre_open run on cached fixtures
- [ ] 13.6 Manual smoke run on real data
- [ ] 13.7 Update PROGRESS.md → commit `feat(jobs): pre_open end-to-end (MVP)`

## Phase 14 — mid_day + post_close jobs

- [ ] 14.1 `src/trading/jobs/mid_day.py`: live quotes, MTM, kill-switch checks, intraday update
- [ ] 14.2 `src/trading/jobs/post_close.py`: final MTM, prediction eval, reconciliation, post-close brief
- [ ] 14.3 `src/trading/jobs/pre_open_iep.py`: 08:55 pre-open IEP gap filter sub-job
- [ ] 14.4 `scripts/{mid_day,post_close,pre_open_iep}.bat`
- [ ] 14.5 Integration tests for each job with cached fixtures
- [ ] 14.6 Update PROGRESS.md → commit `feat(jobs): mid_day + post_close + IEP`

## Phase 15 — Streamlit dashboard

- [ ] 15.1 `src/trading/ui/app.py`: multipage entry, sidebar nav
- [ ] 15.2 `ui/pages/1_Portfolio.py`: current holdings, health scores, GTT viability
- [ ] 15.3 `ui/pages/2_Today_Signals.py`: top candidates with rationale, paper-trade status
- [ ] 15.4 `ui/pages/3_Backtest.py`: backtest report viewer, equity curve, metrics table
- [ ] 15.5 `ui/pages/4_Paper_Journal.py`: paper-trade history, P&L curve, prediction accuracy
- [ ] 15.6 Smoke tests via `streamlit.testing.v1`
- [ ] 15.7 Update PROGRESS.md → commit `feat(ui): streamlit dashboard`

## Phase 16 — LightGBM ranker (Layer B)

- [ ] 16.1 `src/trading/strategy/ranker.py`: feature builder pulling from features + paper-trade labels
- [ ] 16.2 Training script: walk-forward LightGBM training, hyperparameter defaults
- [ ] 16.3 Model persistence: pickle to `models/ranker_YYYY-MM-DD.pkl` + update `registry.csv`
- [ ] 16.4 Integration: scanner pipeline now calls ranker after rules
- [ ] 16.5 Backtest comparison: rules-only vs rules+ranker; promote only if Sharpe improves
- [ ] 16.6 Tests: training pipeline with synthetic labels; frozen-input ranker output
- [ ] 16.7 Update PROGRESS.md → commit `feat(strategy): LightGBM ranker`

## Phase 17 — Task Scheduler + logging

- [ ] 17.1 `loguru` configuration: rotating logs to `data/logs/{job}_YYYY-MM-DD.log`
- [ ] 17.2 Windows Task Scheduler entries for pre_open, pre_open_iep, mid_day, post_close, weekly_train, monthly_sip
- [ ] 17.3 Error notification: Windows toast (winrt) or simple SMTP on job failure
- [ ] 17.4 Manual verification: leave laptop on overnight, verify jobs fire
- [ ] 17.5 Document scheduler setup in `docs/operations.md`
- [ ] 17.6 Update PROGRESS.md → commit `feat(ops): scheduling + logging`

## Phase 18 — Live paper-trading + iteration (ongoing)

- [ ] 18.1 3-6 month live paper-trade run
- [ ] 18.2 Weekly performance review: hit rate, profit factor, calibration plot
- [ ] 18.3 Quarterly LightGBM retrain (auto via `weekly_train.py` on Sundays)
- [ ] 18.4 Monthly SIP allocator dry-run vs actual investment decisions
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
