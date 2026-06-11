# Phase 18 support tooling — weekly_train + monthly_sip

**Date:** 2026-06-11
**Status:** Approved
**Unblocks:** PROGRESS.md 18.2 (weekly performance review), 18.3 (scheduled retrain), 18.4 (monthly SIP dry-run)

Both jobs were deferred from Phase 17.2 (spec §12 out-of-scope list). This
mini-phase delivers them as two thin job modules in the established
`src/trading/jobs/` pattern. Neither contains a new engine — all heavy
lifting delegates to Phase 7 (metrics), Phase 10 (allocator, health),
Phase 16 (train_walkforward + model registry), and Phase 17 (notify,
calendar, logging).

## 1. Decisions made during brainstorm

| Question | Decision |
|---|---|
| Execution model | **Hybrid.** weekly_train runs unattended via Task Scheduler (fully local — parquet + SQLite + LightGBM, no broker data). monthly_sip stays reminder-driven like every other Kite-dependent job. |
| Retrain cadence | **Weekly**, every Sunday, per original spec §10. The registry's 0.05-Sharpe soft-promotion deadband decides activation; quarterly promotion emerges naturally. PROGRESS 18.3's "quarterly" wording is superseded. |
| Review artifact | **Markdown + Slack ping.** Stats tables in `data/research/weekly/`; plots stay on the Streamlit dashboard (no matplotlib dependency). |
| SIP candidate source | **Trailing 10 trading-day signals** + HOLD-rated holdings. The plan is a menu the user executes manually over the month (spec §7.3 staggering). |
| Structure | **Approach A:** two job modules + thin CLI wrappers, mirroring pre_open/mid_day/post_close. |
| SIP budget | Default ₹1,00,000, overridable via `--budget`. |

## 2. `src/trading/jobs/weekly_train.py`

### API

```python
@dataclass(frozen=True)
class WeeklyTrainResult:
    as_of: date
    window_start: date          # as_of − 3 years
    window_end: date            # as_of
    retrain_ran: bool           # False when guard or InsufficientDataError skipped it
    retrain_skip_reason: str | None
    examples: int | None        # final-window training examples (None if skipped)
    oos_sharpe: float | None
    promoted: bool
    model_path: str | None
    review_path: Path           # always written

def run_weekly_train(
    as_of: date | None = None,  # default: today IST
    *,
    paths: Paths,
    conn: sqlite3.Connection,
    skip_train: bool = False,
) -> WeeklyTrainResult: ...
```

### Step 1 — retrain (graceful)

- Window: `start = as_of − 3 years`, `end = as_of` (rolling, spec §10).
- **Idempotency guard:** if `models/registry.csv` already has a row whose
  train window end equals this window end, skip retraining
  (`retrain_skip_reason = "already trained for <end>"`). Sunday re-runs
  are therefore safe.
- Otherwise call `train_walkforward(...)` with macro_history +
  sentiment_lookup + negative_news_lookup pulled from SQLite — the exact
  same wiring `trading train-ranker` uses today (extract that wiring from
  `cli.py` into a shared helper rather than duplicating it).
- Save pickle `models/ranker_<end>.pkl`; `register(paths, row=...,
  promote=True)` — the soft-promotion gate (0.05 Sharpe deadband, NaN
  never promotes) decides activation.
- `InsufficientDataError` → log warning, set `retrain_ran=False`, and
  **continue to the review step**. A retrain failure must never block
  the weekly review.

### Step 2 — weekly review markdown

Written to `data/research/weekly/YYYY-MM-DD_review.md` (directory created
on demand). Sections, each rendering `_(no data)_` when its source is
empty:

1. **Header** — review date, covered window (`as_of − 7 days` → `as_of`).
2. **Week's closed trades** — table from `paper_trades` where
   `ts_exit` ∈ window: symbol, entry/exit price, P&L, pnl_pct, exit
   reason, days held.
3. **Week stats vs cumulative** — hit rate, profit factor, expectancy,
   avg R-multiple via Phase 7 `backtest/metrics.py` over (a) the week's
   closed trades, (b) all closed trades to date. Sharpe from the
   `portfolio_snapshots` equity series (cumulative only).
4. **Open positions** — open `paper_trades` with current stop and days
   held.
5. **Prediction calibration** — text table from matured `predictions`:
   bucketed predicted conviction vs realized hit rate, plus a pointer to
   the dashboard's calibration scatter.
6. **Retrain outcome** — trained/skipped (reason), examples, fold OOS
   Sharpe, promoted or held back by the deadband, active model name.

### Step 3 — Slack summary

One `ops.notify.notify("info", "📊 Weekly review YYYY-MM-DD", body)` with
week P&L, hit rate, PF, open-trade count, and the retrain outcome line.
Best-effort (notify never raises).

### CLI + scheduling

- `trading weekly-train [--date YYYY-MM-DD] [--skip-train]` — Rich
  summary table of `WeeklyTrainResult`; exit 0 even when retrain skipped
  (review written is the success criterion).
- `configure_logging("weekly_train")` at entry — the Phase 17 ERROR sink
  posts traceback + log tail to Slack on any crash.
- `docs/scheduler/weekly_train.xml` — **Sunday 10:00 IST, runs
  `uv run trading weekly-train` directly** (not `trading remind`). No
  trading-day gate: Sunday is never a trading day, and the job is fully
  local. `scripts/weekly_train.bat` launcher for parity with other jobs.

## 3. `src/trading/jobs/monthly_sip.py`

### API

```python
class MonthlySipAborted(Exception): ...   # missing kite snapshot

@dataclass(frozen=True)
class MonthlySipResult:
    as_of: date
    budget: float
    holdings_count: int
    candidates_considered: int
    deployed: float
    cash_reserve: float
    allocations: int            # count of non-CASH allocation lines
    plan_path: Path | None      # None on --dry-run

def run_monthly_sip(
    as_of: date,
    *,
    paths: Paths,
    conn: sqlite3.Connection,
    budget: float = 100_000.0,
    dry_run: bool = False,
) -> MonthlySipResult: ...
```

### Steps

1. **Holdings** — `kite_snapshot.read_holdings(as_of)`. Missing/stale
   snapshot → `MonthlySipAborted`; CLI exits 2 with the same
   "run /kite-snapshot first" remediation as pre-open. Map to
   `HoldingSnapshot(symbol, sector, current_value=qty × last_price)`;
   sector from `data/static/sector_map.csv` via `load_sector_map`,
   unmapped symbols → sector `"UNKNOWN"` (still counted toward caps).
2. **Health** — score each holding HOLD/TRIM/EXIT by the same path
   pre_open's `_step_portfolio` uses (enriched parquet history +
   `sentiment_daily`). Holdings without parquet history get
   `health=None` → excluded from the TOPUP bucket (allocator already
   requires HOLD).
3. **Candidates** — distinct symbols with a `signals` row in the
   trailing **10 trading days** ending at and including `as_of`
   (trading-day window from `ops.calendar.is_trading_day`). Per symbol:
   `priority = max(ml_score)` over the window (NULL → 0.0),
   `entry_price` = latest parquet close ≤ `as_of`, `sector` from
   sector_map, `health` attached when the symbol is also a holding.
   Symbols without parquet history are dropped with a logged warning.
4. **Allocate** — `allocate_sip(candidates, holdings, budget=budget)`
   with library-default caps (50% topup / 50% new / 60% deployed /
   25% stock / 30% sector / 5 concurrent).
5. **Render** — `data/research/YYYY-MM-DD/sip_plan.md`:
   - Allocation table: action, symbol, ₹ amount, rationale.
   - Skipped table: symbol + reason.
   - **Sector weights table** (spec §7.4 concentration warning):
     post-plan portfolio weight per sector, rows over 30% flagged.
   - Inputs footnote: budget, holdings count, candidate window.
   Skipped entirely on `--dry-run` (print-only).
6. **Notify** — Slack summary: deployed vs cash, top allocations.
   Skipped on `--dry-run`.

### CLI + scheduling

- `trading sip --date YYYY-MM-DD [--budget N] [--dry-run]`.
- `configure_logging("monthly_sip")` at entry.
- New reminder slot in `ops/runner.py` `SCHEDULE`:
  `"monthly_sip": ReminderSlot("09:30", "🔔 Monthly SIP",
  "Run /kite-snapshot, then `trading sip --date <date>`")`.
- **`ReminderSlot` grows a `gate_holidays: bool = True` field**;
  `fire_reminder` checks it before the trading-day gate. `monthly_sip`
  sets `gate_holidays=False` — a 1st falling on a weekend/holiday must
  not silently skip the month's reminder (planning needs no open
  market). All twelve existing slots keep the default and behave
  unchanged.
- `docs/scheduler/monthly_sip.xml` — monthly trigger, day 1, 09:30 IST,
  runs `uv run trading remind --slot monthly_sip`.

## 4. Error handling

- Retrain failure (insufficient data, LightGBM error) → warning +
  review still generated. Only an unhandled crash (bug) reaches the
  ERROR Slack sink.
- `notify` is best-effort by Phase 17 contract — Slack/toast failure
  never crashes either job.
- Empty DB sources (no trades, no predictions, no signals) render
  placeholders; both jobs succeed on a fresh ledger.
- monthly_sip aborts loudly (exit 2 + remediation) only for the missing
  kite snapshot — its single hard dependency.
- Both jobs are idempotent: file writes overwrite; the registry guard
  prevents duplicate training rows; allocate_sip is pure.

## 5. Testing

Established job-test pattern: in-memory SQLite + synthetic parquet +
`seed_kite_snapshot` from `conftest.py`. All offline/deterministic.

- `test_jobs_weekly_train.py`: window math; registry guard skips
  duplicate window; `InsufficientDataError` continues to review;
  review with zero trades (all placeholders); review with seeded
  closed + open trades + matured predictions (snapshot test);
  `skip_train=True`; Slack payload assembled (notify stubbed).
- `test_jobs_monthly_sip.py`: abort on missing snapshot; trailing
  10-trading-day window (signals just inside/outside); priority =
  max ml_score, NULL → 0; unmapped sector → UNKNOWN; held symbol gets
  health attached; dry-run writes nothing; sip_plan.md sections render
  (snapshot test); sector-weight flag at >30%.
- `test_runner.py` additions: `gate_holidays=False` slot fires on a
  non-trading day; default slots still gated.
- `test_cli.py` additions: `weekly-train` happy path; `sip` happy path
  + exit-2 abort.
- Gate: `ruff check .` + `mypy src/` + full `pytest -q` green before
  commit; push to origin/main at phase end.

## 6. Out of scope

- Real order placement or GTT modification from the SIP plan — the plan
  is a markdown menu, execution stays manual (v1 is paper/planning).
- PNG/matplotlib plots in the review — dashboard owns visualization.
- Unattended monthly_sip — requires fresh holdings via MCP, user stays
  in the loop.
- Backfill of missed Sundays (laptop off) — next Sunday's run covers a
  rolling window anyway; the gap is visible in the registry.
- nsepython holiday refresh for 2027+ — unchanged from Phase 17.
