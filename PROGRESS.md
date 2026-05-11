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
| 4 | Technical indicators | `[ ]` |
| 5 | Rule scanner (Layer A) | `[ ]` |
| 6 | Sizing + exits | `[ ]` |
| 7 | Backtest engine | `[ ]` |
| 8 | News + sentiment | `[ ]` |
| 9 | Macro + regime | `[ ]` |
| 10 | Portfolio analyzer | `[ ]` |
| 11 | Paper-trade ledger | `[ ]` |
| 12 | LLM analyst | `[ ]` |
| 13 | pre_open job (MVP ⭐) | `[ ]` |
| 14 | mid_day + post_close jobs | `[ ]` |
| 15 | Streamlit dashboard | `[ ]` |
| 16 | LightGBM ranker (Layer B) | `[ ]` |
| 17 | Task Scheduler + logging | `[ ]` |
| 18 | Live paper-trading (ongoing) | `[ ]` |

**Currently working on:** _Phase 4 — Technical indicators_
**Next up:** _Phase 5 — Rule scanner (Layer A)_

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

- [ ] 4.1 `src/trading/features/technicals.py`: pandas-ta wrappers for RSI, MACD, ATR, EMA, SMA, BB, ADX, VWAP, OBV, returns
- [ ] 4.2 Add helpers: `add_indicators(df)` enriches an OHLCV DataFrame with all default indicators
- [ ] 4.3 Tests: known-input/known-output for each indicator
- [ ] 4.4 Update PROGRESS.md → commit `feat(features): technical indicators`

## Phase 5 — Rule scanner (Layer A)

- [ ] 5.1 `src/trading/strategy/rules.py`: function per filter from spec Section 4.1
- [ ] 5.2 `scan(date)` orchestrator: loads parquet for universe, applies all filters, returns candidates
- [ ] 5.3 CLI `trading scan --date YYYY-MM-DD`: outputs ranked candidates as table + JSON
- [ ] 5.4 Tests: synthetic DataFrames triggering and failing each filter
- [ ] 5.5 Update PROGRESS.md → commit `feat(strategy): rule scanner`

## Phase 6 — Sizing + exits

- [ ] 6.1 `src/trading/strategy/sizing.py`: `position_size(capital, risk_pct, entry, stop)` with concurrency caps
- [ ] 6.2 `src/trading/strategy/exits.py`: state machine returning `(action, new_stop)` given (trade, today's bar)
- [ ] 6.3 Implement: stop hit, target hit, time stop (25 trading days), trailing (+10% → breakeven, +15% → 1×ATR trail)
- [ ] 6.4 Tests: scenario per branch (target, stop, time, trail-up, trail-protect)
- [ ] 6.5 Update PROGRESS.md → commit `feat(strategy): sizing + exits`

## Phase 7 — Backtest engine

- [ ] 7.1 `src/trading/backtest/costs.py`: Zerodha-accurate cost model (STT 0.1% × 2, slippage 0.1% × 2, transaction + SEBI charges, stamp duty, GST)
- [ ] 7.2 `src/trading/backtest/engine.py`: vectorbt wrapper that takes rules + sizing + exits → trade log
- [ ] 7.3 `src/trading/backtest/walkforward.py`: rolling 3y train / 6mo test, step 3mo
- [ ] 7.4 `src/trading/backtest/metrics.py`: CAGR, Sharpe, Sortino, max DD, hit rate, profit factor, expectancy, alpha/beta vs Nifty 200
- [ ] 7.5 CLI `trading backtest --years 3`: runs full backtest, writes report markdown
- [ ] 7.6 Tests: cost model units; regression test on frozen period (Sharpe ±5%)
- [ ] 7.7 Run baseline rule-only backtest; sanity-check vs Nifty 200 buy-hold
- [ ] 7.8 Update PROGRESS.md → commit `feat(backtest): engine + walk-forward`

## Phase 8 — News + sentiment

- [ ] 8.1 `src/trading/data/news.py`: RSS aggregator (Moneycontrol, ET, BS); NSE corp announcements scraper
- [ ] 8.2 `src/trading/features/sentiment.py`: FinBERT loader (cached locally), `score_headline(text)` returning [-1, +1] + category
- [ ] 8.3 Critical-event keyword classifier (SEBI/SAT/fraud/auditor/pledge) → `is_critical=True`
- [ ] 8.4 Aggregator: per-stock 7d/30d sentiment → writes `sentiment_daily`
- [ ] 8.5 Tests: cached HTML fixtures; FinBERT snapshot on a fixed sentence; critical-keyword tests
- [ ] 8.6 Update PROGRESS.md → commit `feat(features): news + sentiment`

## Phase 9 — Macro + regime

- [ ] 9.1 `src/trading/data/macro.py`: yfinance fetchers for SGX/GIFT, S&P/Nasdaq/Dow, USDINR, BZ=F, ^TNX, India VIX
- [ ] 9.2 NSE daily FII/DII flow scraper
- [ ] 9.3 `src/trading/features/regime.py`: weighted composite → RISK_ON / NEUTRAL / RISK_OFF
- [ ] 9.4 Pre-open snapshot job writes `macro_snapshot`
- [ ] 9.5 Tests: regime classifier rules with synthetic inputs
- [ ] 9.6 Update PROGRESS.md → commit `feat(features): macro + regime`

## Phase 10 — Portfolio analyzer

- [ ] 10.1 `src/trading/portfolio/health.py`: per-holding HOLD/TRIM/EXIT scorer using fundamentals + technicals + sentiment
- [ ] 10.2 `src/trading/portfolio/gtt.py`: Monte Carlo viability (1,000 ATR-based paths, 60-day vol)
- [ ] 10.3 `src/trading/portfolio/allocator.py`: ₹1L SIP splitter (topup vs new vs cash; ≤60% deployed per batch)
- [ ] 10.4 CLI `trading portfolio`: produces markdown report
- [ ] 10.5 Tests: synthetic Kite fixtures; allocator concurrency caps
- [ ] 10.6 Update PROGRESS.md → commit `feat(portfolio): health + GTT + allocator`

## Phase 11 — Paper-trade ledger

- [ ] 11.1 `src/trading/paper/ledger.py`: `log_signal`, `open_trade`, `close_trade`, `list_open`, `mark_to_market`
- [ ] 11.2 `src/trading/paper/mtm.py`: pulls live LTP via Kite, applies exit logic, writes closes
- [ ] 11.3 `src/trading/paper/reconcile.py`: daily P&L; predicted vs actual accuracy
- [ ] 11.4 Tests: full lifecycle in-memory SQLite
- [ ] 11.5 Update PROGRESS.md → commit `feat(paper): ledger + MTM`

## Phase 12 — LLM analyst

- [ ] 12.1 `src/trading/llm/client.py`: anthropic SDK wrapper with retry, prompt caching, model selection (Haiku 4.5 / Sonnet 4.6)
- [ ] 12.2 `src/trading/llm/prompts.py`: prompt builders for macro brief, per-stock narrative, post-close recap, sector commentary
- [ ] 12.3 `src/trading/llm/briefing.py`: assembles full daily markdown brief from prompts + outputs
- [ ] 12.4 Tests: mocked anthropic client; prompt-template snapshot tests (syrupy)
- [ ] 12.5 Cost tracking: log token usage per session for budget visibility
- [ ] 12.6 Update PROGRESS.md → commit `feat(llm): analyst pipeline`

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
