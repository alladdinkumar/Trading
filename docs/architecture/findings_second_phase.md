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
- A parallel six-subagent discovery sweep was launched first (one per layer) but
  was cut off mid-run by an account usage cap before any agent wrote its artifact.
  The verified findings below were produced by a direct main-session audit of the
  **profit-critical decision core**; the remaining layers are queued (see
  [Audit coverage](#audit-coverage)) for the parallel sweep once the cap resets.

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
This pass surfaced **6 new findings**, two of them High:

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
3. **An edge-quality bug (F-050, Med).** The Layer-A "pullback" gate is
   symmetric around the SMA, so a name extended *above* its average passes as a
   "pullback" — contradicting the documented dip thesis and the operator's
   stated buy-the-dip bias.
4. Three minor items (F-051–F-053): gross-of-cost "win" labelling, missing-quote
   MTM marked at entry price, and a dead calendar-day helper that could
   reintroduce F-024.

**Bottom line:** fix F-049 **and F-054** before trusting any paper or backtest
Sharpe for a go/no-go — one flatters exits, the other can silently corrupt the
price history underneath everything; F-050 is the highest-leverage edge
improvement; the rest are hygiene.

### Breakdown

| Severity | Count | IDs |
|---|---:|---|
| **High** | 2 | F-049, F-054 |
| Med | 2 | F-050, F-051 |
| Low | 2 | F-052, F-053 |

| Category | IDs |
|---|---|
| VULN (correctness/measurement/data-integrity) | F-049, F-054 |
| RISK (strategy/market) | F-050 |
| GAP (missing guardrail) | F-051, F-052 |
| DEBT (cleanup) | F-053 |

---

## Findings

### F-049 — Gap-through-stop fills at the stop price, not the gap-open (`VULN`, High, strategy/exits) — Open

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

### F-050 — Layer-A "pullback" gate is symmetric: an extended-above name passes as a pullback (`RISK`, Med, strategy/rules) — Open

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

### F-051 — Prediction "actual return" and win labelling are gross of costs (`GAP`, Med, paper/reconcile) — Open · confidence: Likely

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

### F-052 — MTM marks a missing-quote symbol at entry price, overstating losers (`GAP`, Low, paper/reconcile) — Open

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

**Fix** Fall back to the **last available close from parquet** (the same source
`mtm.build_bars_from_history` already reads) rather than entry price; only if that
is also unavailable, mark at entry and flag the snapshot row as estimated.

---

### F-053 — Dead `days_between` calendar-day helper can silently reintroduce F-024 (`DEBT`, Low, paper/ledger) — Open

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

### F-054 — Incremental OHLCV refresh never re-adjusts history → a split/bonus corrupts the series (`VULN`, High, data/ohlcv_refresh) — Open

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

## Audit coverage

This second pass is **in progress**. What has been audited directly (verified
against code) vs. what is queued for the parallel-subagent sweep after the cap
reset:

| Domain | Modules | Status |
|---|---|---|
| Features & strategy core | `features/technicals,regime,sentiment`*, `strategy/rules,exits,sizing,daily_budget,calibration,trajectory`* | ✅ audited (F-049, F-050; `regime`/`sentiment`/`trajectory` only lightly) |
| Backtest engine + costs | `backtest/engine,costs`, `costs.py` | ✅ audited (F-049; cost parity confirmed OK) |
| Paper trading & P&L | `paper/ledger,mtm,reconcile` | ✅ audited (F-049, F-051, F-052, F-053) |
| Data ingestion & storage | `data/*`, `store/*` | 🔶 partial — OHLCV adjust/refresh path audited (F-054); **queued:** IST/date handling, snapshot-schema coverage, news dedup, macro cross-verify, cache TTL |
| Backtest stats & ranking ML | `backtest/walkforward,metrics,forward_return,factor_eval`, `ranking/*` | ⏳ queued — lens: train/test purging vs label overlap (adjacent to open F-047), Sharpe annualisation, promotion gate, label construction |
| Jobs orchestration & ops | `jobs/*`, `ops/*`, `clock.py`, `cli.py` | ⏳ queued — lens: IST/date boundaries, half-run/idempotency, the IEP `_quote_symbols.txt` gap, `open-fills` live-open fill wiring, graceful-degradation scope |
| LLM/brief, config, domain, UI | `llm/*`, `config.py`, `domain.py`, `ui/*`, `.claude/skills/*` | ⏳ queued — lens: skill↔python JSON contract drift, config/secret exposure, UI crash/mislead on empty data |

*Lightly read for cross-references; not yet a full audit.*

> When the queued domains are audited, new findings continue at **F-054+** and
> this coverage table + the breakdown are updated. After findings stabilise, the
> `docs/architecture/` 00–08 set is refreshed for current flows and diagrams.
