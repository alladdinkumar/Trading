# Daily Capital Budget for the Paper Book — Design

**Date:** 2026-06-19
**Status:** Approved (design); pending spec review before implementation plan.

## Goal

Stop the paper book from over-deploying. Today the daily `pre_open` job sizes
each day's top-K candidates against a **fixed ₹1L** with **no portfolio cash
gate**, so positions accumulate across days far beyond the funded capital
(currently 12 open trades, ₹291,731 deployed, cash −₹192k → ~2.9× leverage).

This design adds a **cash-aware daily deployment budget**: a ₹1,00,000 standing
pool, paced so at most **₹7,000 of new buys open per day** and never more than
**available cash**, with the daily budget distributed across the day's signals
by **expected value (P(win) × upside)** so the money goes to the most profitable
ideas first. Existing trades are retained; a one-time deposit brings available
cash to ₹1L, then a snapshot regeneration refreshes the equity curve.

## Background — what exists today

- **`strategy/sizing.py::position_size(SizingInput) -> SizingResult`** — pure
  risk-based sizing: `risk_qty = floor(capital × risk_pct × regime_mult /
  (entry−stop))`, bounded by a 25%-per-stock and 30%-per-sector cap; floors at 0.
  Inputs include `deployed_in_symbol` / `deployed_in_sector` (both default 0.0).
- **`jobs/pre_open.py::_step_auto_open(conn, as_of, scored, regime, capital,
  risk_pct, warnings)`** — for each `ScoredCandidate` (`.candidate`, `.ml_score`,
  `.selected`) marked `selected` (top-K, `RANKER_TOP_K = 5`), computes
  `stop = close − 1.5·atr_14`, `target = target_price(close, stop)`, sizes via
  `position_size(capital=capital, …)`, and opens a paper trade. **`capital` is a
  fixed `capital_per_trade = 100_000.0` every day, and nothing checks remaining
  cash** — the root cause of over-deployment. Non-selected candidates are logged
  as visibility-only signals. An `_already_opened_today` guard prevents same-day
  duplicates per symbol.
- **`paper/reconcile.py::compute_paper_cash(conn, *, as_of, initial_capital)`** —
  cash derived from the trade ledger + funds top-ups (`initial_capital +
  total_funds_added − deployed − costs + proceeds`).
- **`paper/funds.py`** — `add_funds`, `total_funds_added`; the `cash_ledger` table.
- **`portfolio/allocator.py::allocate_sip`** — a *monthly* ₹1L allocator
  (TOPUP/NEW buckets, health verdicts, 60% deploy cap, 5-concurrency). Different
  semantics from a daily entry pacer — **not reused here** (see Approach C).
- **`reconcile_day` / `trading post-close --date <d> --apply`** — writes the
  daily `portfolio_snapshots` row (`equity = cash + Σ holdings value`) the equity
  curve and Home plot read.

## Background — equity vs exposure (the reported mismatch)

The equity curve plots `equity = cash + holdings value` = **funded capital +
P&L** (~₹101.5k: ₹100k seed + ~₹1.5k paper profit). The Paper Portfolio page's
"Current value" ₹293.9k is **gross position value** — larger than equity because
the book bought ₹291k of stock on ₹100k (the leverage this design removes). The
curve is correct; the gap is the symptom. Separately, the latest snapshot
(2026-06-18, 6 symbols) predates 6 of the 12 current trades, so it is stale.
After funding to ₹1L available, equity = total funded (~₹392k) + P&L ≈ ₹393.9k;
the one-time snapshot regeneration makes the curve reflect that immediately.

## Key decisions (resolved during brainstorming)

1. **Budget model = ₹1L standing pool + ₹7k/day pacing.** ₹1L is the working
   capital; each day open at most ₹7k of *new* buys **and** never more than
   available cash. Cash recycles as trades close. (Not a fresh-monthly injection.)
2. **Existing book retained.** Keep the 12 trades; a one-time deposit brings
   available cash to ₹1L (total funds ≈ ₹392k); the new policy applies to future
   entries only.
3. **Profit bias = expected value.** Rank the day's selected signals by
   `ev = p_win × implied_return`, where `p_win = ml_score` (fallback 0.5 when the
   ranker is absent) and `implied_return = (target − entry) / entry`. Greedy-fill
   the ₹7k top-down; the best-EV signal is sized first (risk-capped), leftover
   budget flows to the next.
4. **Risk sized against the stable ₹1L pool** (consistent 2%/trade), while the
   ₹7k/day + live-cash envelope does the pacing — the two don't fight.
5. **Per-stock cap honored across days.** The planner feeds live
   `deployed_in_symbol` into `position_size`, so the 25%-per-stock cap holds
   cumulatively, not just intraday. Sector cap stays at default (no sector map in
   the DB yet — pre-existing limitation, out of scope).

## Approach (chosen of three)

- **A — dedicated pure daily-budget planner (chosen).** A new
  `strategy/daily_budget.py` owns EV ordering + the daily/cash envelope and
  reuses `position_size()` for risk + caps. Clean boundary, unit-testable in
  isolation, leaves the monthly SIP and backtest untouched.
- **B — inline the cap inside `_step_auto_open`.** Less new code, but tangles
  per-signal sizing with portfolio-level pacing/ordering inside the job; hard to
  test; the EV re-ordering forces a loop rewrite anyway.
- **C — reuse `allocate_sip`.** Its monthly bucket/health/60%-deploy semantics
  don't match a daily entry pacer and it emits ₹-amounts, not the qty/stop/target
  needed to open trades; forcing it risks regressing the monthly SIP.

## Architecture

```
selected ScoredCandidates ──► [adapt] ──► BudgetCandidate[]
                                              │
  compute_paper_cash(as_of) ─► available_cash │
  open trades ─► deployed_by_symbol{}         ▼
                                   plan_daily_entries()  ── pure, reuses position_size()
                                              │
                              DailyPlan(entries, skipped, …)
                                              │
                     _step_auto_open: open entries via log_signal_and_open_trade;
                     log the rest as visibility-only signals (with skip reasons)
```

## Component 1 — Daily-budget planner (`src/trading/strategy/daily_budget.py`, new)

Pure, no DB, no network.

```python
DEFAULT_POOL_CAPITAL = 100_000.0
DEFAULT_DAILY_DEPLOY_CAP = 7_000.0
DEFAULT_PWIN_PRIOR = 0.5   # ml_score fallback when the ranker is absent

@dataclass(frozen=True)
class BudgetCandidate:
    symbol: str
    entry: float          # entry price (D-1 close)
    stop: float
    target: float
    ml_score: float | None

@dataclass(frozen=True)
class PlannedEntry:
    symbol: str
    qty: int
    entry: float
    stop: float
    target: float
    notional: float       # qty * entry
    ev_score: float
    reason: str           # human-readable sizing note

@dataclass(frozen=True)
class DailyPlan:
    entries: list[PlannedEntry]
    skipped: list[tuple[str, str]]   # (symbol, reason)
    budget_used: float               # Σ entries.notional
    budget_cap: float                # min(daily_cap, max(0, available_cash))
    cash_before: float               # available_cash at plan time

def plan_daily_entries(
    candidates: list[BudgetCandidate],
    *,
    available_cash: float,
    deployed_by_symbol: dict[str, float],
    regime: Regime,
    pool_capital: float = DEFAULT_POOL_CAPITAL,
    daily_cap: float = DEFAULT_DAILY_DEPLOY_CAP,
    risk_pct: float = 0.02,
) -> DailyPlan: ...
```

**Algorithm**
1. `ev = p_win × implied_return`, `p_win = ml_score if not None else
   DEFAULT_PWIN_PRIOR`, `implied_return = (target − entry) / entry`.
2. Sort candidates by `ev` **descending**, tie-broken by `symbol` ascending
   (deterministic).
3. `running_budget = min(daily_cap, max(0.0, available_cash))`;
   `running_cash = available_cash`.
4. For each candidate in order:
   - If `running_budget <= 0` → `skipped += (symbol, "daily budget exhausted")`,
     continue.
   - `base = position_size(SizingInput(capital=pool_capital, entry=entry,
     stop=stop, risk_pct=risk_pct, regime=regime,
     deployed_in_symbol=deployed_by_symbol.get(symbol, 0.0)))`.
   - If `base.qty == 0` → `skipped += (symbol, "risk/stock cap → 0: " +
     "; ".join(base.reasons))`, continue.
   - `cap_notional = min(base.notional, running_budget, running_cash)`;
     `qty = floor(cap_notional / entry)`.
   - If `qty < 1` → `skipped += (symbol, "budget/cash < 1 share")`, continue
     (keep scanning — a cheaper later signal may still fit).
   - `notional = qty × entry`; emit `PlannedEntry(…, ev_score=ev,
     reason="ev {ev:.3f}; qty {qty}; budget left ₹{…}")`.
   - `running_budget −= notional`; `running_cash −= notional +
     buy_side_cost(notional)`.
5. Return `DailyPlan(entries, skipped, budget_used=Σ notional,
   budget_cap=min(daily_cap, max(0, available_cash)), cash_before=available_cash)`.

**Notes**
- The **₹7k/day cap is on notional** (capital deployed in shares); the **cash
  gate is on notional + buy-side cost** (what the ledger actually pays).
- `available_cash ≤ 0` ⇒ `running_budget = 0` ⇒ every candidate skipped
  (`"daily budget exhausted"`). This is the intended "no new buys without cash"
  behavior and matches the current over-deployed state until the deposit lands.
- `buy_side_cost` is imported from `paper.ledger` (already pure, no DB).

## Component 2 — Cash-aware auto-open (`src/trading/jobs/pre_open.py`)

Rewrite `_step_auto_open` to consult the planner.

- New signature:
  `_step_auto_open(conn, as_of, scored, regime, pool_capital, daily_cap,
  risk_pct, warnings) -> int`.
- Compute `available_cash = compute_paper_cash(conn, as_of=as_of)`.
- Compute `deployed_by_symbol` from open trades:
  `SELECT s.symbol, SUM(pt.entry_price*pt.qty) FROM paper_trades pt JOIN signals s
  ON s.id=pt.signal_id WHERE pt.ts_exit IS NULL GROUP BY s.symbol`.
- Build `BudgetCandidate`s from `selected` candidates, reusing the existing
  per-candidate prep: `stop = close − 1.5·atr_14` (skip + warn if
  `close ≤ stop`), `target = target_price(close, stop)`. Skip any symbol that
  `_already_opened_today`.
- `plan = plan_daily_entries(cands, available_cash=…, deployed_by_symbol=…,
  regime=regime, pool_capital=pool_capital, daily_cap=daily_cap,
  risk_pct=risk_pct)`.
- For each `PlannedEntry`: build the `Signal` (as today, `created_by="pre_open"`,
  `horizon_days=25`, `rules_passed_json`, `ml_score`) and
  `log_signal_and_open_trade(…, qty=entry.qty, …)`. Count opened.
- For every **non-opened** candidate — `selected` ones the planner skipped **and**
  non-selected ones — `insert_signal` only (visibility), and append the planner's
  skip reason (or "not selected") to `warnings`.
- `run_pre_open`: rename param `capital_per_trade` → `pool_capital`
  (default `100_000.0`); add `daily_deploy_cap: float = 7_000.0`; thread both into
  `_step_auto_open`. `risk_pct` unchanged (default 0.02).

## Component 3 — Funding primitive (`src/trading/cli.py`)

New `funds` sub-command so "fund to ₹1L available" is one reproducible call.

```
trading funds top-up --to-available <amount> [--date YYYY-MM-DD]
```
- `target = amount`; `cash = compute_paper_cash(conn, as_of=date)`.
- If `target − cash <= 0` → print "already at/above ₹{target} available
  (cash ₹{cash})", write nothing, exit 0 (idempotent).
- Else `deposit = target − cash`; `add_funds(conn, amount=deposit, date=date,
  note=f"top-up to ₹{target:,.0f} available")`; print the deposit + new balance
  (reuse `compute_summary` for the breakdown, like `funds add`).

## Operations — one-time funding + snapshot refresh (runbook, also a plan task)

1. `trading funds top-up --to-available 100000 --date 2026-06-19` — deposits
   ~₹292k so available cash = ₹1,00,000 (total funds ≈ ₹392k).
2. `trading post-close --date 2026-06-19 --apply` — regenerates today's
   `portfolio_snapshots` row against current bars, so it captures all 12 open
   trades and positive cash; the equity curve / Home plot then show equity
   ≈ ₹393.9k (cash ₹100k + holdings ₹293.9k) instead of the stale 6-symbol point.

## Error handling

- `add_funds` already rejects `amount ≤ 0`; `top-up --to-available` no-ops when
  already funded (never inserts a non-positive deposit).
- `available_cash ≤ 0` → empty plan (all skipped), existing trades/MTM untouched.
- `ml_score is None` → `p_win = 0.5` prior (no crash, mild EV).
- `entry ≤ stop` → candidate skipped before planning (existing guard).
- Planner is pure and total: any candidate that can't be sized lands in
  `skipped` with a reason; `_step_auto_open` surfaces every skip in `warnings`.

## Testing (TDD throughout)

- **`tests/test_daily_budget.py`** —
  - EV ordering: higher `ml_score × upside` is sized first.
  - ₹7k cap binds: `Σ entries.notional ≤ daily_cap`.
  - Cash gate binds: with `available_cash < daily_cap`, `Σ notional ≤
    available_cash`.
  - Per-stock cap honored: a large `deployed_by_symbol[sym]` shrinks/zeros that
    symbol's qty (via `position_size`).
  - Qty clip: an expensive top-EV signal is clipped to the remaining budget, and
    a cheaper later signal still fills.
  - Skip when budget can't afford 1 share → `(symbol, "budget/cash < 1 share")`.
  - `ml_score=None` → 0.5 prior used (still rankable).
  - `available_cash ≤ 0` → empty `entries`, all in `skipped`.
  - Empty candidates → empty plan.
- **`tests/test_pre_open*.py`** — integration: with seeded scored candidates and
  funded cash, `_step_auto_open` opens trades whose total notional ≤ ₹7k and ≤
  cash; update existing pre-open tests that assumed unconstrained opening of all
  top-K.
- **`tests/test_cli_funds.py`** — `funds top-up --to-available` deposits the gap
  and is idempotent (second call at/above target writes nothing).
- Existing suite stays green; ruff + mypy clean.

## Out of scope (YAGNI)

- Building one position up to full size across multiple days (each day's entry is
  one-shot, clipped to the daily budget).
- A symbol→sector map (sector cap stays at the default; pre-existing gap).
- A UI "today's deploy ₹X / ₹7k" widget (can add later).
- Any change to the monthly SIP allocator or the backtest sizing path.
- Auto-closing or resizing the existing 12 over-deployed trades (retained as-is;
  they wind down naturally via exits/MTM).
