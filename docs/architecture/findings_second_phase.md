# Architecture Review — Findings Log (Second Phase)

> Second-pass review opened **2026-06-30**, re-evaluating the codebase after the
> large body of changes since the first review (docs 00–08 + [`FINDINGS.md`](./FINDINGS.md),
> which closed 43 of 48 findings). This file continues the same numbering
> (**F-049+**) so IDs never collide across the two logs, and follows the same
> entry format: `F-0xx — Title (CATEGORY, Severity, Area) — Status`, categories
> `VULN | GAP | INACC | DEBT | RISK`.
>
> **Goal lens:** these findings are prioritised by what moves the project's actual
> objective — *proving real, honest profitability in paper before risking money*
> and *being able to trust a backtest when comparing strategies*. Measurement
> integrity ranks above cosmetics.

## How this pass was run

- Read-only audit. Each finding cites exact `file:line` and a concrete failure
  scenario; fixes and a regression-test idea are suggested but **not yet applied**.
- The **profit-critical decision core** (F-049–F-054) was audited directly in the
  main session. The remaining four layers were then swept by **four parallel
  read-only subagents** (data/store · backtest-stats & ranking-ML · jobs/ops ·
  llm/config/UI), each handed a precise file list and pre-formed hypotheses and
  told to return distilled candidates only.
- **Every subagent candidate was re-verified against source in the main session
  before filing** — no finding here rests on an agent's word alone. Two agents
  independently flagged the same IST-clock bug (filed once, F-058); one candidate
  that duplicated an existing finding was folded into it (F-052) rather than
  re-filed. That second wave produced **F-055–F-066**.

## Health baseline (2026-06-29 run)

Objective starting state, so every finding below is understood as a *correctness
gap the current tests do not catch* — not a red suite:

| Check | Result |
|---|---|
| `ruff check .` | ✅ clean |
| `mypy src/trading` | ✅ clean |
| `pytest` (91 test files) | ✅ all pass; 6 snapshots OK; only benign warnings (sklearn feature-names, transformers tokenizer deprecation) |

## Executive summary

The decision core is, as the first review found, carefully built — pure
functions, conservative-by-intent fills, cost parity between backtest and paper
(F-025 holds), causal indicators (no look-ahead in `add_indicators` → slice).
This pass surfaced **18 findings**, three of them High. All three High items
attack the same target: *the honesty of the number the go/no-go rests on.*

1. **A measurement-integrity bug (F-049, High).** A stop that *gaps
   through* its level fills at the **stop price**, not the gap-open — so on
   gap-down days the simulated exit is a price the stock never traded at. It is
   the one place the "conservative fills" claim breaks, it is **asymmetric**
   (only flatters results), and it contaminates the backtest equity curve, the
   paper equity curve / OOS Sharpe (the Phase-18.5 go/no-go metric), **and** the
   prediction-error → calibration learning loop at once.
2. **A data-integrity bug (F-054, High).** The daily OHLCV refresh appends only
   the new tail and never re-adjusts history, so the first split/bonus in a name
   during the paper run leaves a price discontinuity in its stored series —
   corrupting every SMA/RSI/ATR and injecting a phantom return that the scanner
   and backtest both read as real. The lone guard is holdings-only and
   warn-only, so the candidate universe is unprotected.
3. **A leakage bug in the ML gate (F-055, High).** The walk-forward split leaves
   **zero embargo** (`test_start = train_end`) while training labels look **25
   trading days forward**, so every fold trains on labels whose outcome window
   reaches into the first ~25 days of its own OOS test fold. The
   `oos_sharpe_pooled` that decides whether the ranker is trusted live is
   inflated by a systematic, repeating amount whenever returns are serially
   correlated.
4. **An edge-quality bug (F-050, Med).** The Layer-A "pullback" gate is
   symmetric around the SMA, so a name extended *above* its average passes as a
   "pullback" — contradicting the documented dip thesis and the operator's
   stated buy-the-dip bias.
5. **The medium band (F-051, F-056–F-062)** clusters into *controls that silently
   don't work* and *numbers the operator can't trust*: the `--capital`/`--daily-cap`/
   `--risk-pct` CLI flags are wired to nothing (F-056); five Nifty-50 candidates
   are exempt from the sector concentration cap because they're missing from the
   sector map (F-057); the quote-staleness gate uses the host clock instead of the
   IST clock (F-058); the Paper-Portfolio "Total P&L" tile hides *all* realised
   P&L (F-059); the Slack webhook URL is logged in cleartext on any post failure
   (F-060); and three different "Sharpe" figures are shown, none of which is the
   daily-annualised number the go-live bar is actually defined on (F-061).
6. **The low band (F-052, F-053, F-063–F-066)** is hygiene, defence-in-depth and
   reporting-integrity: entry-price MTM fallback, a dead calendar-day helper, an
   unchecked stale macro-cross file, a hallucination-check that switches off on
   flagged figures, a tz-skewed news window, and a dormant equity-stitching bug
   that must not be wired up as-is.

**Bottom line:** fix **F-049, F-054 and F-055** before trusting any paper or
backtest Sharpe for a go/no-go — one flatters exits, one can corrupt the price
history underneath everything, and one leaks the test period into the model that
ranks tomorrow's trades. F-050/F-057/F-058 are the highest-leverage
edge-and-safety fixes; F-059/F-061 mean the operator currently *cannot read the
true P&L or the gate Sharpe off the system*; the rest are hygiene.

### Breakdown

| Severity | Count | IDs |
|---|---:|---|
| **High** | 3 | F-049, F-054, F-055 |
| Med | 9 | F-050, F-051, F-056, F-057, F-058, F-059, F-060, F-061, F-062 |
| Low | 6 | F-052, F-053, F-063, F-064, F-065, F-066 |

| Category | IDs |
|---|---|
| VULN (correctness/measurement/data-integrity/security) | F-049, F-054, F-055, F-060 |
| RISK (strategy/market/operational) | F-050, F-058 |
| INACC (misleading/mis-scaled number) | F-059, F-061, F-065 |
| GAP (missing guardrail) | F-051, F-052, F-056, F-057, F-062, F-063, F-064 |
| DEBT (cleanup/dormant) | F-053, F-066 |

---

## Findings

### F-049 — Gap-through-stop fills at the stop price, not the gap-open (`VULN`, High, strategy/exits) — ✅ Fixed 2026-07-02 (`0217669`)

**Where**
- Root cause: `src/trading/strategy/exits.py:80-88` (`evaluate_exit`, stop branch).
- Backtest path: `src/trading/backtest/engine.py:317-330` (`_evaluate_exits`).
- Paper path: `src/trading/paper/mtm.py:135,169-178` (`mtm_open_trades`).
- Propagates into learning: `src/trading/paper/reconcile.py:103-117`
  (`evaluate_matured_predictions` reads the optimistic `exit_price`).

**What**
`evaluate_exit` fires a stop whenever `bar.low <= current_stop` and returns
`exit_price = trade.current_stop` (exits.py:82-88). It never looks at `bar.open`.
When a bar **gaps down through** the stop (`bar.open < current_stop`), the stock
opened — and could only have been sold — *below* the stop, yet the engine books
the exit at the stop level. The backtest then applies only a fixed 0.1% slippage
(`engine.py:325`), and the paper path applies none to the price at all
(`mtm.py:175` closes at `decision.exit_price`); neither models the gap.

Compare the entry side, which is modelled correctly: pending buys fill at
`apply_slippage(bar_open, "buy", …)` (`engine.py:267-268`). So the simulation is
**asymmetric** — realistic entries, optimistic stop-exits.

**Impact**
- Worked example: stop ₹98, prior close ₹100, stock gaps to ₹95 open on bad news
  and never trades back to ₹98. The engine/paper book the exit at ₹98 (−2%),
  reality is ≈ ₹95 (−5%). The 3% gap is pure phantom P&L, and it always favours
  the result (a gap *up* through a target is conservatively capped at the target,
  so the bias is one-directional).
- It corrupts three things simultaneously: (a) backtest `net_pnl`/equity/Sharpe
  used to compare strategies; (b) paper equity curve and the **OOS Sharpe the
  Phase-18.5 go/no-go to real money depends on**; (c) the `predictions.actual_*`
  values (`reconcile.py:104`) that feed the score→win-rate calibration (F-041),
  so the learner is trained on rosy exits too.
- Gap-downs through stops are common in single NSE names (earnings, sector/global
  risk-off opens) and cluster exactly in the regimes where honest measurement
  matters most.

**Fix**
Make the stop fill gap-aware in one place so both paths inherit it. In
`evaluate_exit`, when the stop triggers, set
`exit_price = min(current_stop, bar.open)` (for a long: if the bar opened below
the stop, you are filled at the open, not the stop). Keep the existing
stop-wins-tie precedence. Leave the target branch as-is (capping a gap-up at the
target is genuinely conservative).

**Test**
Add a case to the exits/engine suite with a bar where `open < current_stop <=`
prior close and assert the booked exit equals `bar.open` (× sell slippage), not
`current_stop`. Add a same-bar gap-down-through-both-stop-and-target case to
confirm stop still wins and fills at the gap-open.

---

### F-050 — Layer-A "pullback" gate is symmetric: an extended-above name passes as a pullback (`RISK`, Med, strategy/rules) — ✅ Fixed 2026-07-02 (`8181633`)

**Where** `src/trading/strategy/rules.py:99-115` (`passes_pullback`).

**What**
The rule passes when `abs(close - sma)/sma <= max_pct` (3%) for `sma_20` *or*
`sma_50`. The distance is unsigned, so a stock trading **2.9% above** its 20-DMA
satisfies "pullback" identically to one 2.9% below it. Combined with
`passes_uptrend` (close > sma_200, sma_50 > sma_200), the gate will green-light
names that are *extended above* their short averages — the opposite of a pullback
entry.

**Impact**
- Contradicts the system's own documented thesis ("uptrend **+ pullback**",
  overview §1) and the operator's explicit preference for dip/pullback entries
  within an established uptrend. Buying 2.9% above the 20-DMA is buying strength,
  not a dip: entries land further from support, with worse reward-to-risk and a
  higher chance of immediate mean-reversion against the position — degrading the
  win-rate and average entry quality of the Layer-A gate.
- Partially masked by `passes_rsi_band` (RSI 30–45 usually implies price at/below
  short-term support), so not every extended name slips through — but RSI 45 with
  price just above the 20-DMA in a choppy uptrend does. Hence Med, not High.

**Fix**
Make the band directional: only count a leg as a pullback when `close <= sma`
(or allow a small upside tolerance, e.g. `close <= sma * 1.005`), while keeping
the 3% floor below. I.e. accept `sma*(1-max_pct) <= close <= sma*(1+small_tol)`.
Tie the thresholds to the dip intent and re-run the backtest to confirm the
entry-quality/expectancy change before/after.

**Test** Parametrise `passes_pullback` over close = sma×{0.98, 1.0, 1.02, 1.029}
and assert the above-SMA-by->tol cases now fail.

---

### F-051 — Prediction "actual return" and win labelling are gross of costs (`GAP`, Med, paper/reconcile) — ✅ Fixed 2026-07-03

**Where** `src/trading/paper/reconcile.py:102-117` (`evaluate_matured_predictions`);
interacts with the calibration consumer in `strategy/calibration.py` +
`jobs/weekly_train.py` (win/loss derivation — **to confirm**).

**What**
`actual_pct` is computed as the raw price return `(exit_price - entry_price)/entry_price*100`
with no cost deduction (lines 104-117), while `compute_trade_pnl` (ledger.py:115-134)
*does* net round-trip costs (~0.4%). If the score→win-rate calibration treats a
prediction as a "win" on `actual_pct > 0` (gross), a trade that closes e.g. +0.3%
gross — a **net loss** after costs — is counted as a win.

**Impact**
Biases the realised hit-rate per score band upward, which inflates
`calibrated_p_win` → inflates the EV ranking in `daily_budget._ev`, i.e. the
self-correcting edge (F-041) is calibrated against a slightly optimistic truth.
Small per trade, but systematic and self-reinforcing.

**Fix** Compute the matured "actual" as net-of-cost return (reuse
`compute_trade_pnl`/`pnl_pct`), and define the calibration "won" as net-positive,
so prediction error and the win label use the same costed yardstick as the P&L.

**To confirm** Read `jobs/weekly_train.py` + `ranking/ranker_labels.py` to verify
exactly how "won" is derived before sizing the fix (hence "Likely", not
"Confirmed").

---

### F-052 — MTM marks a missing-quote symbol at entry price, overstating losers (`GAP`, Low, paper/reconcile) — ✅ Fixed 2026-07-03

**Where** `src/trading/paper/reconcile.py:232` (`compute_portfolio_snapshot`);
analogous fallback in `backtest/engine.py:362-363` (`_mark_to_market`).

**What**
When a held symbol is absent from today's `bars`, equity marks it at
`trade.entry_price` (cost basis), i.e. flat P&L for the day, rather than its last
known price. A position currently down (or up) is snapped back to break-even for
any day its quote is missing.

**Impact**
On days with partial quote coverage (a Kite quote-fetch gap, a halted/suspended
name), the equity curve and drawdown are distorted — optimistically for an
open loser, pessimistically for an open winner. Low because the snapshot is
written at post-close from official closes, which are usually complete; the risk
is the occasional missing/halted symbol.

**2nd-pass note (jobs/ops audit).** Two things sharpen this: (a) the fallback is
**silent** — unlike the rest of `reconcile.py`, `compute_portfolio_snapshot`
appends **no warning** when it snaps a symbol to entry price, so nothing reaches
`post_close_summary.md`; and (b) the marked row is written to **`portfolio_snapshots`**,
which is the persisted equity/drawdown series the go-live Sharpe (F-061) reads
back. `mtm_open_trades` does emit a visible `SKIP — no bar` row for the same
trade, but nothing cross-references that to the equity figure below it. So a
missing quote for a name that is up double-digits intraday understates that day's
equity with no trace — feeding exactly the gate metric. Stays Low (needs a missing
quote at post-close) but the *silence* is the real defect.

**Fix** Fall back to the **previous day's `portfolio_snapshots` per-share mark**
(as `positions._marks` already does) or the **last available close from parquet**
(the source `mtm.build_bars_from_history` reads) rather than entry price; and
**append a warning naming the affected symbol** so it surfaces in
`post_close_summary.md` and can be excluded from Sharpe/drawdown math. Only if no
prior mark exists, mark at entry and flag the row estimated.

---

### F-053 — Dead `days_between` calendar-day helper can silently reintroduce F-024 (`DEBT`, Low, paper/ledger) — ✅ Fixed 2026-07-03

**Where** `src/trading/paper/ledger.py:182-193` (`days_between`).

**What**
`days_between` returns **calendar** days (`(exit_dt - entry_dt).days`). A grep
shows it is unused anywhere in `src/trading` — MTM correctly uses
`mtm._days_held` (numpy business days) for the persisted `days_held` and the
25-day time stop (the F-024 fix). The dead helper sits next to the live one with
a near-identical signature.

**Impact**
No live effect today, but it is a trap: a future caller reaching for "days held"
could import `days_between` and silently reinstate the calendar-vs-trading-day
double-count F-024 fixed (which once made the 25-day stop fire at ~12 calendar
days).

**Fix** Remove it, or if a calendar-day count is genuinely wanted for a summary
stat, rename to `calendar_days_between` and document that it must **not** feed
`days_held`/the time stop. Add a one-line note pointing at `mtm._days_held` as the
canonical trading-day count.

---

### F-054 — Incremental OHLCV refresh never re-adjusts history → a split/bonus corrupts the series (`VULN`, High, data/ohlcv_refresh) — ✅ Fixed 2026-07-02 (`4514b42`)

**Where** `src/trading/data/ohlcv_refresh.py:81-115` (`_refresh_one`); partial
guard `cross_check_closes` (`ohlcv_refresh.py:118-147`); fetch
`data/yfinance.py:30-72` (`auto_adjust=True`).

**What**
`fetch_ohlcv` returns split/dividend-**adjusted** OHLC (`auto_adjust=True`,
yfinance.py:35) — correct in isolation. But `_refresh_one` fetches only the
missing tail (`start = last_bar + 1 day`, lines 89-98) and
`pd.concat([existing, fetched])` (line 106) onto the **existing** parquet, whose
historical bars were written at the adjustment scale of an *earlier* fetch.
yfinance re-scales the entire series to the latest corporate action, but the
refresh only ever sees the new tail, so the historical rows are never
re-adjusted. The day a name splits or issues a bonus, the stored series gets a
**step discontinuity** at the action date — old bars on the pre-action scale, new
bars on the post-action scale.

**Impact** (alpha engine + backtest)
- A 1:2 split halves price: stored history stays ~₹2000 while new bars arrive
  ~₹1000, so the series shows a **phantom −50% bar** on the split day.
- Every trailing indicator blends the two scales: `sma_20/50/200`, `rsi_14`,
  `atr_14`, `returns_1d` are wrong for up to ~200 bars after the action — so the
  Layer-A gates (uptrend/pullback/RSI/liquidity) and the Layer-B ranker features
  read garbage, and any backtest replaying that parquet books the phantom move as
  a real return (and a likely stop-out). One split silently poisons months of
  signals.
- NSE large-caps split / issue bonuses regularly, so over a multi-month paper run
  this is a *when*, not *if*.
- The only guard, `cross_check_closes`, is **holdings-only** (≤8 symbols),
  **last-close-only**, and **warn-only** (lines 130-147): the 42 candidate-only
  Nifty-50 names have no check at all, and even for holdings it fires *after* the
  tail is already polluted and repairs nothing.

**Fix** Make the refresh adjustment-aware so it self-heals:
- Fetch with a small **overlap** (`start = last_bar − ~5 trading days`), compare
  the re-fetched overlap closes to the stored ones; on divergence beyond a
  tolerance (history was re-scaled), **re-fetch full history and overwrite** that
  symbol rather than appending.
- Or, simplest to add: a periodic **full re-backfill** of the whole universe
  (`auto_adjust=True`, overwrite) in `weekly_train`, bounding drift to ≤1 week.
- Promote `cross_check_closes` to run over the full candidate universe and
  *trigger* the re-fetch on a breach instead of only warning.

**Test** Simulate a split: write a parquet at the old scale, stub `fetch_ohlcv`
to return post-split tail bars, run `refresh_ohlcv`, and assert the stored series
has no >20% single-bar seam (i.e. the overlap-mismatch path re-backfilled).

**To confirm** Whether `jobs/weekly_train.py` already does a full re-ingest
(which would bound exposure); the default daily path (`pre_open → refresh_ohlcv`)
does not.

---

### F-055 — Walk-forward leaves zero embargo: 25-day training labels leak into the next OOS test fold (`VULN`, High, backtest/walkforward) — ✅ Fixed 2026-07-02 (`87b05f5`, `da590db`)

**Where**
- No-gap split: `src/trading/backtest/walkforward.py:60-61` (`windows()`: `train_end = train_start + train_delta; test_start = train_end`).
- Label horizon: `src/trading/backtest/forward_return.py:32,78-97` (`realized_return`, `max_days=25`, walks bars `pos+1 … pos+25`).
- Live consumer: `src/trading/ranking/ranker_train.py:133` (train mask `df.index < train_end`), `:141` (`realized_return(df, sd)`, no `max_days` override), pooled into `oos_sharpe_pooled` at `:315`.

**What**
`test_start` is *defined* to equal `train_end` — there is no purge/embargo between the windows. But every training row's **label** is the net return of a trade opened at `sd+1` and held up to 25 trading days, so any training signal dated in `[train_end − 25 trading days, train_end)` has its outcome computed from bars on/after `test_start` — **inside the immediately-following OOS test fold**. Features are correctly `≤ sd` only, so the leak is label-only, but that is enough. It recurs identically in every fold and every weekly retrain (`weekly_train` reuses the same `windows()`).

**Impact**
The model is fit on `(X, y)` pairs whose `y`, for the boundary rows, is realised from the same price bars that later grade the model's own early-fold OOS trades. Where returns are serially correlated across the boundary (common in Indian-market momentum/regime runs), this **inflates `oos_sharpe_pooled`** — the sole quantity gating whether the ranker's `ml_score` is served live vs. falling back to `p_win = prior`. The promotion gate's t-stat / majority-fold / 2-week-persistence filters defend against *random* noise, not a *systematic, repeating* bias of this shape. Distinct from open **F-047** (which is the i.i.d./effective-N assumption *within* the pooled trades, not label bleed across the split).

**Fix**
Add an embargo of `max_days` (25) trading days on the training side: either advance `test_start = train_end + embargo` in `windows()`, or mask training signals to `sd < train_end − 25 trading days` in `_build_xy_for_window`, so no training label's outcome window can reach a bar inside its own fold's test period.

**Test** Build a fold with a training signal 10 trading days before `train_end`; assert it is excluded from the training set once the embargo is applied.

---

### F-056 — Pre-open risk-control flags (`--capital` / `--daily-cap` / `--risk-pct`) are wired to nothing (`GAP`, Med, jobs/pre_open) — ✅ Fixed 2026-07-02 (`adc7ba8`)

**Where** `src/trading/jobs/pre_open.py:103-105` (`run_pre_open` params `pool_capital`/`daily_deploy_cap`/`risk_pct`); the real deploy is `src/trading/jobs/open_fills.py:152` (`plan_daily_entries(...)`), which never receives them; defaults in `src/trading/strategy/daily_budget.py` (`DEFAULT_POOL_CAPITAL=100_000`, `DEFAULT_DAILY_DEPLOY_CAP=7_000`, `risk_pct=0.02`).

**What**
`run_pre_open` accepts the three risk parameters (forwarded from the CLI's `--capital`/`--daily-cap`/`--risk-pct`) but the body never reads them again — a full-`jobs/` grep finds the names only in that signature. Capital is deployed later, in `open_fills.py`'s apply mode, which calls `plan_daily_entries` with none of them, so the hardcoded `daily_budget` defaults always apply.

**Impact**
`trading pre-open --capital 20000 --daily-cap 2000 --risk-pct 0.01` prints a normal success table and has **zero effect** — sizing still runs at ₹100k / ₹7k / 2%. Since the project's purpose is to *test different configurations* in paper, a control that silently lies is a real gap: the OOS track record is generated under parameters the operator neither chose nor can tell were ignored.

**Fix** Thread `pool_capital`/`daily_deploy_cap`/`risk_pct` from `run_pre_open` through `_pending_entries.json` into `open_fills.plan_daily_entries`, **or** delete the dead parameters and CLI flags so the interface can't imply a control that doesn't exist.

**Test** End-to-end, assert `plan_daily_entries` is invoked with the operator-supplied capital/cap/risk (not the defaults) when the flags are passed.

---

### F-057 — Five Nifty-50 candidates are missing from `sector_map.csv`, exempting them from the sector concentration cap (`GAP`, Med, data/sector) — ✅ Fixed 2026-07-03

**Where** `data/static/sector_map.csv` (no rows for `BHARTIARTL`, `ETERNAL`, `INDIGO`, `TITAN`, `TRENT`); consumed via `src/trading/data/sector.py` (`load_sector_map`) → `src/trading/jobs/open_fills.py:137,147` → `src/trading/strategy/daily_budget.py:105-138` (a `sector=None` candidate is **never** gated by the max-2-lots-per-sector cap, F-048).

**What**
The live candidate universe is `data/static/nifty50.txt` (loaded by `load_candidate_universe`, scanned at `pre_open.py:327`; scan is "restricted to the Nifty 50"). Five of those 50 names have no row in `sector_map.csv`, so `sector_map.get(symbol)` returns `None` and the F-048 per-sector control skips them entirely — the map fell out of sync with the last index rebalance (every other Nifty-50 name, including recent entrant `TMPV`, is mapped).

**Impact**
On a broad risk-on day, `BHARTIARTL`, `INDIGO` and `TITAN` (and `ETERNAL`/`TRENT`) can all be opened the same session with **no concentration limit**, while an equivalent cluster of correctly-mapped names (e.g. three IT stocks) is hard-capped at 2 concurrent lots. The concentration guardrail is silently unenforced for 10% of the book.

**Fix** Add the five symbols to `sector_map.csv` with their NSE sectoral index; add a startup assertion / test that **every** `nifty50.txt` symbol resolves to a sector, so a future rebalance can't silently reopen the gap.

**Test** `assert set(load_candidate_universe()) <= set(load_sector_map())`.

---

### F-058 — Quote-staleness gate (and several date defaults) use the host's local clock, not the canonical IST clock (`RISK`, Med, data/quotes_snapshot) — ✅ Fixed 2026-07-03

**Where** `src/trading/data/quotes_snapshot.py:68-72` (`capture_ts` naive from filename; `age = datetime.now() - capture_ts`); same anti-pattern in `src/trading/ops/logging_setup.py:116` and CLI ad-hoc date defaults (`cli.py` funds-add/top-up, refresh-ohlcv, ingest-news, macro-*). `clock.py` exists precisely so nothing re-derives the IST offset locally — the original **F-004**.

**What**
`read_latest_quotes` builds `capture_ts` as a naive IST wall-clock from the `quotes_HHMM.json` filename, then compares it to `datetime.now()` — the **host** clock, not `trading.clock.now_ist()`. It works only because the two agree when the OS timezone is Asia/Kolkata. This staleness check gates whether `mid_day`/`open_fills` apply mode may trade/mark on a snapshot at all; the same `date.today()` drift hits the log filename and, more consequentially, `funds add` without `--date`, which stamps the deposit date `compute_paper_cash` depends on. *(Two agents independently flagged `quotes_snapshot.py:72`.)*

**Impact**
Run the workflow on a non-IST host (a UTC-defaulted sandbox/container): depending on drift direction, a genuinely fresh capture is rejected as `QuoteSnapshotStaleError` (the day's fills/MTM abort for no reason) or an hours-stale capture is accepted as fresh (stale prices feed fills/MTM). A `funds add` near the UTC/IST midnight boundary lands in the wrong day's `cash_ledger`, shifting that day's equity. Latent on an IST desktop; real the moment execution moves to a non-IST environment. The green suite can't catch it — `test_quotes_snapshot.py` uses `freeze_time`, patching `datetime.now()` to the same naive value that seeds the fixture.

**Fix** Use `trading.clock.now_ist()` (with a tz-aware `capture_ts`) at `quotes_snapshot.py:72`, and `trading.clock.today_ist()` at `logging_setup.py:116` and the CLI's ad-hoc `date.today()` defaults.

**Test** With the process TZ = UTC and `now_ist` mocked, assert a quote captured 2 min ago (IST) reads fresh and one captured 90 min ago reads stale.

---

### F-059 — Paper-Portfolio "Total P&L" / "Today's P&L" tiles exclude all realised P&L (`INACC`, Med, ui/paper) — ✅ Fixed 2026-07-03

**Where** `src/trading/paper/positions.py:117` (`compute_positions` filters `pt.ts_exit IS NULL` — open lots only) → `:162-166` (`compute_summary`: `total_pnl = current_value − invested`, `today_pnl = Σ p.today_pnl`, both over that open-only list) → `src/trading/ui/pages/4_Paper_Portfolio.py:54,60` (tiles labelled plainly "Total P&L" / "Today's P&L"). Realised P&L already lives correctly in `cash`/`account_value` (`reconcile.compute_paper_cash` credits every closed trade's proceeds).

**What**
`compute_summary` derives the P&L tiles purely from currently-open positions, so the instant a trade closes — win or loss — its entire contribution vanishes from every tile labelled "P&L" and survives only inside the differently-named "Account value" tile.

**Impact**
Close one paper trade for +₹5,000 with nothing else open: the page shows **Total P&L ₹0 (0.00%)** and Today's P&L ₹0 while Account value correctly reads ₹1,05,000. The one headline the operator reads to judge whether paper trading is *working* is misleading exactly after trades resolve — the moment realised performance is the whole point. *(Untested: `test_paper_positions.py` never closes a trade before asserting `total_pnl`.)*

**Fix** Derive `total_pnl = account_value − (initial_capital + funds_added)` (realised + unrealised), or add an explicit realised term summed over closed trades; if the open-only figure is kept, relabel it "Unrealised P&L".

**Test** Open then close a winning paper trade; assert `compute_summary.total_pnl` equals the net realised gain, not 0.

---

### F-060 — Slack webhook URL logged in cleartext on any post failure (`VULN`, Med, ops/notify) — ✅ Fixed 2026-07-03

**Where** `src/trading/ops/notify.py:44-49` (`post_slack`: `resp.raise_for_status()` inside `try`; `except requests.RequestException as e: logger.warning(f"Slack post failed: {e}")`); the WARN is persisted by the INFO-level rotating-file + stderr sinks in `ops/logging_setup.py`.

**What**
For a Slack incoming webhook the URL **is** the credential (`https://hooks.slack.com/services/T…/B…/<token>`). `raise_for_status()` raises `HTTPError` whose message is `… for url: <full url>`; a `ConnectionError`/timeout likewise stringifies to `… url: /services/…/<token>`. `logger.warning(f"… {e}")` writes that verbatim to `data/logs/<job>_<date>.log` and stderr.

**Impact**
Any non-2xx or network hiccup (rotated/revoked hook, rate-limit, transient DNS) writes the full, still-live webhook token to a plaintext on-disk log (60-day retention) and into any captured console/session transcript. `data/logs/` is gitignored, but a post-only credential is now at rest in cleartext and may travel in shared transcripts — contrary to the project rule that secrets are never logged/surfaced. Med, not High: low-privilege (post-only to one channel), local, gitignored — but a trivial-to-fix real leak.

**Fix** In the `except`, log only `type(e).__name__` and, if present, `e.response.status_code` — never `str(e)`/`e`.

**Test** Stub `requests.post` to raise `HTTPError("404 … for url: https://hooks.slack.com/services/T/B/secret")`; assert the emitted record contains neither `hooks.slack.com` nor the token.

---

### F-061 — Three inconsistent "Sharpe" definitions; the daily-annualised gate Sharpe is never computed on the live paper equity (`INACC`, Med, backtest/ui) — ✅ Fixed 2026-07-03

**Where**
- Ranker/factor: `src/trading/ranking/ranker_train.py:212,292,315` and `backtest/factor_eval.py:106` call `metrics.sharpe(..., periods_per_year=12)` on **per-trade** returns → stored as `RegistryRow.oos_sharpe`, printed "OOS Sharpe (pooled)".
- UI: `src/trading/ui/pages/3_Paper_Journal.py:78-85,126` — `mean/std` of per-trade `pnl_pct`, **no annualisation**, labelled "Sharpe (per-trade)".
- The only daily-annualised Sharpe (`metrics.sharpe(daily_returns, periods_per_year=252)`, `metrics.py:74-83`) is wired **only** to the historical `backtest` CLI (`cli.py:530`) — never to the live `portfolio_snapshots.equity` series.

**What**
The go-live bar is "≥3 months OOS **Sharpe > 1.0**" on the daily-return convention (overview). But the live paper book's own equity curve is never run through `metrics.sharpe`; the two figures the operator actually sees are a per-trade ratio annualised at a fictitious 12 trades/yr and a non-annualised per-trade ratio. The `12` algebraically cancels inside the promotion gate's t-stat (so the sign/significance gate isn't broken), but the **reported number** and the `SHARPE_PROMOTION_DEADBAND=0.05` sit on a scale not comparable to the ">1.0 daily" criterion.

**Impact**
An operator eyeballing "OOS Sharpe (pooled): 1.3" or the Journal's "Sharpe 1.1" against the ">1.0" gate is comparing differently-scaled statistics; the real go/no-go metric — the daily-annualised Sharpe of the paper equity curve — is **not computed anywhere**, so the single most important number for the money decision cannot be read off the system.

**Fix** Compute and surface the daily-annualised Sharpe of `portfolio_snapshots.equity`'s daily returns via `backtest.metrics.sharpe`, labelled as the go-live-gate metric; and either derive a real trades-per-year for the ranker figure or stop labelling per-trade ratios "Sharpe".

**Test** Assert a live-equity Sharpe returns `mean/std·√252` on a known series, and that the Journal/ranker figures are labelled distinctly from it.

---

### F-062 — IEP health checkpoints are permanently unsatisfiable, so `trading status` reports a false "half-run" every day (`GAP`, Med, ops/run_status) — ✅ Fixed 2026-07-03

**Where** `src/trading/ops/run_status.py` (`_probe_iep_quotes`/`_probe_iep_filter` hard-depend on `_iep_quote_file` finding a pre-10:30-IST `quotes_HHMM.json`); `cli.py:1931-1933` (`status` exits 1 on `has_due_failure()`).

**What**
Nothing in the pipeline writes an IEP-band quotes file (the known `_quote_symbols.txt` gap), so `_iep_quote_file` returns `None` unconditionally and both IEP probes resolve to `state="missing"` once 08:55/09:00 IST pass — every trading day, regardless of whether `pre_open_iep` actually ran (it does, degrading to a benign no-op filter). `run_status` can't distinguish "ran benignly" from "didn't run", so `has_due_failure()` is `True` daily.

**Impact**
From ~09:00 IST every trading day, `trading status` prints `iep 0/2 ❌` and exits 1 "Half-run detected" even on a fully-correct day. Any cron/operator habit built on "red = investigate" is trained to ignore it — masking the day a *real* checkpoint (`mid_day`/`post_close`) is genuinely missing. A health gate that always cries wolf is worse than none.

**Fix** Either treat "IEP ran with no overnight quotes" as `done`/`n-a` (detect the job's own "quotes unavailable" marker in `_context.md`, or a sentinel it writes), or drop the two IEP checkpoints from `_CHECKS`/`has_due_failure` until the `_quote_symbols.txt` gap is closed.

---

### F-063 — `read_macro_cross` has no freshness check, so a stale cross-file can gap-fill today's VIX (`GAP`, Low, data/macro_cross) — ✅ Fixed 2026-07-03

**Where** `src/trading/data/macro_cross.py:38-46` (`read_macro_cross` validates shape only, takes no `as_of`); callers `cli.py` `macro_refresh_cmd`/`macro_verify_cmd` add no date check. Contrast `kite_snapshot._validate_meta`, which raises `KiteSnapshotStaleError` on a date mismatch.

**What**
`read_macro_cross(path)` never compares the file's `captured_at` (or its path date) to the date being refreshed. Its docstring claims "the same F-002 boundary as the broker/quote snapshots" but inherited only the schema half, not the freshness half.

**Impact**
If `/macro-doctor` runs with (or a shell retry reuses) an off-date `--cross` path — `trading macro refresh --date 2026-07-01 --cross data/raw/2026-06-28/macro_cross_1010.json` — **and** today's yfinance VIX came back `None`, the gap-fill silently writes the 3-day-stale VIX into today's `macro_snapshot` under provenance `kite_mcp`, no warning. That VIX drives `classify_regime`'s RISK_ON/NEUTRAL/RISK_OFF multiplier for every trade sized that day. Low — needs two coincident conditions — but silent when it hits.

**Fix** Add an `as_of` date check to `read_macro_cross` (or its callers) that raises when `captured_at`'s date ≠ the refresh date, mirroring `KiteSnapshotStaleError`.

---

### F-064 — Analyst hallucination-check silently skips any macro figure carrying a reconciliation flag (`GAP`, Low, llm/briefing) — ✅ Fixed 2026-07-03

**Where** `src/trading/llm/context.py:112-115,138-141` (annotates a mismatched macro cell as e.g. `"19.40 ⚠ kite 22.5"`); `src/trading/llm/briefing.py:93-100,132-135` (`_macro_figure_warnings`: `try: float(val_str) except ValueError: continue`).

**What**
When `macro_reconciliation` flags a VIX/USDINR mismatch (exactly what F-035/F-036 exist to surface), the bundle cell is no longer a bare number, so `float(val_str)` raises and the field's hallucination check is skipped via `continue` — the safety net switches **off** precisely when the figure is already known unreliable.

**Impact**
Bundle shows `"19.40 ⚠ kite 22.5"`; the analyst brief cites a hallucinated "VIX at 30.0"; `compile_brief` emits no warning at all. Niche (needs a macro mismatch *and* a hallucinated citation together), hence Low — but it defeats the one guard meant for that case.

**Fix** In `_macro_figure_warnings`, parse only the leading numeric token (`val_str.split()[0]`) before `float()`, so an annotated cell is still checked against its bare value.

---

### F-065 — `negative_news_count_7d` window bounds are naive date strings compared to UTC timestamps (`INACC`, Low, store/news_store) — ✅ Fixed 2026-07-03

**Where** `src/trading/store/news_store.py:126-149`; `ts` is always stored UTC-suffixed (`news.py:103-113,180-189`).

**What**
`negative_news_count_7d` builds `start = (as_of − 7d).isoformat()` (bare date) and `end = f"{as_of}T23:59:59"` (no offset) and string-compares them to the UTC `ts` column, while `as_of` is the IST trading date. With no IST→UTC conversion the window edges shift up to 5.5h from the intended IST day. Separately, its strict `<` threshold and span diverge from `features/sentiment.py`'s independent `<=`/midnight-anchored 7-day rollup, so the two similarly-named "negative-news-7d" measures aren't the same quantity.

**Impact**
A negative headline near the window edge (IST early morning) is stored on the previous UTC date and drops out of the count feeding `EntryAttribution.neg_news_7d` / the ranker feature — a small, boundary-only, one-directional miscount.

**Fix** Derive tz-aware UTC bounds from the IST `as_of` (IST-midnight → UTC) instead of comparing naive date strings; reconcile the two "neg-news-7d" definitions to one helper.

---

### F-066 — `run_walkforward`'s fold-stitched equity breaks CAGR / max-drawdown (`DEBT`, Low — dormant, backtest/walkforward) — Open

**Where** `src/trading/backtest/walkforward.py:94-120` (fold loop + `equity_curve[~…duplicated(keep="last")]`); each fold's `run_backtest` resets `cash = initial_capital` (`engine.py:180`); default `WalkForwardConfig` has `test_months=6 > step_months=3` → overlapping test windows.

**What**
`run_walkforward` concatenates per-fold equity **levels**, but every fold restarts at `initial_capital`, so the stitched curve is a sawtooth of resets, not a compounding series. `cagr()`/`max_drawdown()` (which read raw levels) then measure a real CAGR off only the last fold and a fake huge drawdown at every fold restart; overlapping windows also splice a fabricated day-1 `0.0` return into `daily_returns` at each boundary.

**Impact**
**Currently dormant** — production wires only the single-window `run_backtest` (`cli.py:530`); `ranker_train` runs its own fold loop rather than calling `run_walkforward`. It would silently produce a wrong CAGR/max-drawdown headline the moment someone wires the multi-fold report the module's docstring anticipates. Filed so that wiring doesn't ship the bug.

**Fix** Rebase each fold to a cumulative-return series (start 1.0) before concatenating, or compute CAGR/max-drawdown per-fold and aggregate; for overlapping windows, drop the earlier fold's overlapping days rather than splicing a zero return.

---

## Audit coverage

This second pass is **complete** across all seven domains. Each row lists what was
verified against code, the findings it produced, and — as a positive signal — the
sub-areas checked and found **sound** (so a future pass need not re-plough them):

| Domain | Modules | Findings | Verified sound |
|---|---|---|---|
| Features & strategy core | `features/technicals,regime,sentiment`, `strategy/rules,exits,sizing,daily_budget,calibration,trajectory` | F-049, F-050 | no look-ahead in `add_indicators`→slice; sizing caps; calibration bins |
| Backtest engine + costs | `backtest/engine,costs`, `costs.py` | F-049 | next-day-open fill, stop-wins-tie, cost parity (F-025), cash accounting |
| Paper trading & P&L | `paper/ledger,mtm,positions,reconcile,funds` | F-051, F-052, F-059 | `compute_paper_cash` (F-023/25); `_days_held` busday (F-024); re-run idempotency (`already_opened_today`, `close_with_exit` raises) |
| Data ingestion & storage | `data/*`, `store/*`, `clock.py` | F-054, F-057, F-058, F-063, F-065 | `clock.py` offset; `_validate_meta` freshness; macro `None`-propagation (no silent 0); `cache.py` (requests-cache); fno-ban freshness; migration dedup-before-unique |
| Backtest stats & ranking ML | `backtest/walkforward,metrics,forward_return,factor_eval`, `ranking/*`, `store/model_registry` | F-055, F-061, F-066 | daily-return Sharpe path; label uses `>sd` only; no `center=True` features; promotion gate (F-043/44/46) floor+deadband+t-stat+persistence; Path-A factor not wired live |
| Jobs orchestration & ops | `jobs/*`, `ops/*`, `cli.py` | F-056, F-058, F-062 | typed abort on missing/stale snapshot (no silent pass); `retention` can't reach db/parquet/models; txn rollback on mid-block crash; Slack error sink opt-in; no secret in job/ops logs |
| LLM/brief, config, UI | `llm/*`, `config.py`, `ui/*`, `.claude/skills/*` | F-059, F-060, F-061, F-064 | **skill↔python JSON contract matches on both sides** (keys/paths/filenames), backstopped by `snapshot_schema`; `context.py` is `as_of`-filtered, degrades to `_(no data)_`; no secret in config repr/UI |

> **Method footnote.** The four queued domains were swept by parallel read-only
> subagents (Sonnet) with pre-formed hypotheses; every candidate was re-verified
> against source in the main session before filing (see *How this pass was run*).
> The "Verified sound" column is distilled from the agents' coverage notes plus
> spot-checks — it records genuine negative results, not merely unaudited code.

> **Next:** with findings stable through **F-066**, the `docs/architecture/` 00–08
> set is refreshed for current flows and diagrams (Phase 4), then all artifacts are
> committed and pushed (Phase 6).
