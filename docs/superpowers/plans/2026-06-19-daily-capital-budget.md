# Daily Capital Budget — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pace the paper book to a ₹1L standing pool, opening at most ~₹7k of new buys per day, never more than available cash, distributing the daily budget across the day's signals by expected value.

**Architecture:** A new pure planner `strategy/daily_budget.py` owns expected-value ordering and the daily/cash envelope, reusing `position_size()` for risk + per-stock caps. `jobs/pre_open._step_auto_open` is rewritten to compute available cash + currently-deployed-by-symbol, call the planner, open only the planned entries, and log the rest as visibility-only signals. A CLI `funds top-up --to-available` primitive funds cash to a target; a one-time runbook deposits to ₹1L and regenerates the snapshot.

**Tech Stack:** Python 3.11, dataclasses, `math.floor`; pytest; existing `position_size`, `compute_paper_cash`, `buy_side_cost`, `log_signal_and_open_trade`, Typer CLI.

## Global Constraints

- Planner module is **pure**: no DB, no network, no I/O. (`strategy/daily_budget.py`)
- The **₹7k/day cap is on notional** (qty × entry); the **cash gate is on notional + buy-side cost** (`buy_side_cost(notional)`).
- `available_cash ≤ 0` ⇒ empty plan (every candidate skipped), existing trades/MTM untouched.
- `p_win = ml_score if ml_score is not None else 0.5` (`DEFAULT_PWIN_PRIOR`).
- `implied_return = (target − entry) / entry`; `ev = p_win × implied_return`.
- Reuse `position_size(SizingInput(...))` for risk + per-stock cap; feed live `deployed_in_symbol`. Sector cap stays default (no sector map — out of scope).
- Defaults: `DEFAULT_POOL_CAPITAL = 100_000.0`, `DEFAULT_DAILY_DEPLOY_CAP = 7_000.0`, `risk_pct = 0.02`.
- Existing 12 trades retained; no auto-resize/auto-close.
- TDD throughout: red → green → commit. Keep ruff + mypy clean. Run `uv run` for all commands.
- After all tasks: push to origin/main (user's standing workflow).
- Do NOT commit protected untracked files (`.mcp.json`, `CLAUDE.md`, `Research/`, `data/README.md`, `data/mutual_funds_holdings.md`, `docs/daily-workflow.md`, `docs/superpowers/plans/2026-06-19-f010-dormant-tables.md`). Leak-check `git diff --cached` must show 0 occurrences of the two F-005 sentinel phrases (the "real-money" and "suspended-indefinitely" wording).

---

### Task 1: Pure daily-budget planner

**Files:**
- Create: `src/trading/strategy/daily_budget.py`
- Test: `tests/test_daily_budget.py`

**Interfaces:**
- Consumes: `position_size`, `SizingInput`, `Regime` from `trading.strategy.sizing`; `buy_side_cost` from `trading.paper.ledger`.
- Produces:
  - `BudgetCandidate(symbol: str, entry: float, stop: float, target: float, ml_score: float | None)`
  - `PlannedEntry(symbol: str, qty: int, entry: float, stop: float, target: float, notional: float, ev_score: float, reason: str)`
  - `DailyPlan(entries: list[PlannedEntry], skipped: list[tuple[str, str]], budget_used: float, budget_cap: float, cash_before: float)`
  - `plan_daily_entries(candidates: list[BudgetCandidate], *, available_cash: float, deployed_by_symbol: dict[str, float], regime: Regime, pool_capital: float = 100_000.0, daily_cap: float = 7_000.0, risk_pct: float = 0.02) -> DailyPlan`
  - Module constants `DEFAULT_POOL_CAPITAL`, `DEFAULT_DAILY_DEPLOY_CAP`, `DEFAULT_PWIN_PRIOR`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_daily_budget.py`:

```python
"""Tests for the pure daily-budget planner."""

from __future__ import annotations

import math

from trading.strategy.daily_budget import (
    DEFAULT_DAILY_DEPLOY_CAP,
    BudgetCandidate,
    plan_daily_entries,
)


def _cand(symbol: str, *, entry: float, target: float, ml: float | None, stop_frac: float = 0.97):
    return BudgetCandidate(
        symbol=symbol, entry=entry, stop=entry * stop_frac, target=target, ml_score=ml
    )


def test_ev_ordering_best_first() -> None:
    # B has higher ev (0.8 * 0.20) than A (0.5 * 0.05). B must be sized first.
    a = _cand("AAA", entry=100.0, target=105.0, ml=0.5)
    b = _cand("BBB", entry=100.0, target=120.0, ml=0.8)
    plan = plan_daily_entries(
        [a, b], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert plan.entries[0].symbol == "BBB"


def test_daily_cap_binds_on_notional() -> None:
    cands = [_cand(f"S{i}", entry=100.0, target=130.0, ml=0.7) for i in range(20)]
    plan = plan_daily_entries(
        cands, available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert sum(e.notional for e in plan.entries) <= DEFAULT_DAILY_DEPLOY_CAP + 1e-6


def test_cash_gate_binds_when_cash_below_cap() -> None:
    cands = [_cand(f"S{i}", entry=100.0, target=130.0, ml=0.7) for i in range(20)]
    plan = plan_daily_entries(
        cands, available_cash=350.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert sum(e.notional for e in plan.entries) <= 350.0


def test_per_stock_cap_honored_via_deployed() -> None:
    # 25% of 100k = 25k already deployed in AAA → stock cap leaves 0 room.
    a = _cand("AAA", entry=100.0, target=130.0, ml=0.9)
    plan = plan_daily_entries(
        [a], available_cash=1_000_000.0, deployed_by_symbol={"AAA": 25_000.0}, regime="RISK_ON"
    )
    assert plan.entries == []
    assert any(sym == "AAA" for sym, _ in plan.skipped)


def test_expensive_top_ev_clipped_cheaper_later_fills() -> None:
    # Top-EV share costs 6000 (1 share fits in 7k); a 100-rupee name then fills the rest.
    pricey = _cand("PRICEY", entry=6_000.0, target=9_000.0, ml=0.9)
    cheap = _cand("CHEAP", entry=100.0, target=140.0, ml=0.6)
    plan = plan_daily_entries(
        [pricey, cheap], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    symbols = {e.symbol for e in plan.entries}
    assert "PRICEY" in symbols
    assert "CHEAP" in symbols
    assert sum(e.notional for e in plan.entries) <= DEFAULT_DAILY_DEPLOY_CAP + 1e-6


def test_skip_when_budget_cannot_afford_one_share() -> None:
    # One pricey name eats the budget; a second pricey name can't afford a share.
    a = _cand("AAA", entry=6_000.0, target=9_000.0, ml=0.9)
    b = _cand("BBB", entry=6_500.0, target=9_000.0, ml=0.8)
    plan = plan_daily_entries(
        [a, b], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert any(sym == "BBB" and "1 share" in reason for sym, reason in plan.skipped)


def test_ml_none_uses_prior() -> None:
    a = _cand("AAA", entry=100.0, target=120.0, ml=None)
    plan = plan_daily_entries(
        [a], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    # 0.5 prior * 0.20 implied = 0.10 ev — rankable, sized.
    assert math.isclose(plan.entries[0].ev_score, 0.10, rel_tol=1e-6)


def test_no_cash_means_empty_plan() -> None:
    a = _cand("AAA", entry=100.0, target=120.0, ml=0.7)
    plan = plan_daily_entries(
        [a], available_cash=0.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert plan.entries == []
    assert plan.skipped and all("budget" in r for _, r in plan.skipped)


def test_empty_candidates_empty_plan() -> None:
    plan = plan_daily_entries(
        [], available_cash=1_000_000.0, deployed_by_symbol={}, regime="RISK_ON"
    )
    assert plan.entries == []
    assert plan.skipped == []
    assert plan.budget_used == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_daily_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading.strategy.daily_budget'`

- [ ] **Step 3: Implement the planner**

Create `src/trading/strategy/daily_budget.py`:

```python
"""Daily capital-budget planner — paces new paper entries (design 2026-06-19).

Pure: no DB, no network. Ranks the day's selected signals by expected value
(`p_win × implied_return`), then greedy-fills a per-day notional budget
(default ₹7k) bounded by available cash, sizing each pick via
`strategy.sizing.position_size` so the 2%-risk and per-stock caps still apply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trading.paper.ledger import buy_side_cost
from trading.strategy.sizing import Regime, SizingInput, position_size

DEFAULT_POOL_CAPITAL = 100_000.0
DEFAULT_DAILY_DEPLOY_CAP = 7_000.0
DEFAULT_PWIN_PRIOR = 0.5  # ml_score fallback when the ranker is absent


@dataclass(frozen=True)
class BudgetCandidate:
    """A selected signal offered to the planner."""

    symbol: str
    entry: float
    stop: float
    target: float
    ml_score: float | None


@dataclass(frozen=True)
class PlannedEntry:
    """A sized entry the planner decided to open today."""

    symbol: str
    qty: int
    entry: float
    stop: float
    target: float
    notional: float
    ev_score: float
    reason: str


@dataclass(frozen=True)
class DailyPlan:
    """The day's plan: what to open, what was skipped (with reasons), budget used."""

    entries: list[PlannedEntry]
    skipped: list[tuple[str, str]]
    budget_used: float
    budget_cap: float
    cash_before: float


def _ev(c: BudgetCandidate) -> float:
    p_win = c.ml_score if c.ml_score is not None else DEFAULT_PWIN_PRIOR
    implied_return = (c.target - c.entry) / c.entry
    return p_win * implied_return


def plan_daily_entries(
    candidates: list[BudgetCandidate],
    *,
    available_cash: float,
    deployed_by_symbol: dict[str, float],
    regime: Regime,
    pool_capital: float = DEFAULT_POOL_CAPITAL,
    daily_cap: float = DEFAULT_DAILY_DEPLOY_CAP,
    risk_pct: float = 0.02,
) -> DailyPlan:
    """Plan today's entries: EV-ranked, greedy-filled within ₹daily_cap and cash."""
    budget_cap = min(daily_cap, max(0.0, available_cash))
    ranked = sorted(candidates, key=lambda c: (-_ev(c), c.symbol))

    entries: list[PlannedEntry] = []
    skipped: list[tuple[str, str]] = []
    running_budget = budget_cap
    running_cash = available_cash

    for c in ranked:
        ev = _ev(c)
        if running_budget <= 0:
            skipped.append((c.symbol, "daily budget exhausted"))
            continue
        base = position_size(
            SizingInput(
                capital=pool_capital,
                risk_pct=risk_pct,
                entry=c.entry,
                stop=c.stop,
                regime=regime,
                deployed_in_symbol=deployed_by_symbol.get(c.symbol, 0.0),
            )
        )
        if base.qty == 0:
            skipped.append((c.symbol, "risk/stock cap → 0: " + "; ".join(base.reasons)))
            continue
        cap_notional = min(base.notional, running_budget, running_cash)
        qty = math.floor(cap_notional / c.entry)
        if qty < 1:
            skipped.append((c.symbol, "budget/cash < 1 share"))
            continue
        notional = qty * c.entry
        entries.append(
            PlannedEntry(
                symbol=c.symbol,
                qty=qty,
                entry=c.entry,
                stop=c.stop,
                target=c.target,
                notional=notional,
                ev_score=ev,
                reason=f"ev {ev:.3f}; qty {qty}; budget left ₹{running_budget - notional:,.0f}",
            )
        )
        running_budget -= notional
        running_cash -= notional + buy_side_cost(notional)

    return DailyPlan(
        entries=entries,
        skipped=skipped,
        budget_used=sum(e.notional for e in entries),
        budget_cap=budget_cap,
        cash_before=available_cash,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_daily_budget.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Lint + type check**

Run: `uv run ruff check src/trading/strategy/daily_budget.py tests/test_daily_budget.py && uv run mypy src/trading/strategy/daily_budget.py`
Expected: no errors. (`uv run ruff format` if needed.)

- [ ] **Step 6: Commit**

```bash
git add src/trading/strategy/daily_budget.py tests/test_daily_budget.py
git commit -m "feat(paper): pure daily-budget planner (EV-ranked ₹7k/day pacing)"
```

---

### Task 2: Cash-aware auto-open in pre_open

**Files:**
- Modify: `src/trading/jobs/pre_open.py` (`_step_auto_open`, `run_pre_open`, the `_step_auto_open` call site)
- Modify: `src/trading/cli.py:1496-1511` (the `pre-open` command)
- Test: `tests/test_jobs_pre_open.py` (update the 4 `_step_auto_open` unit tests + the orchestrator stub signature)

**Interfaces:**
- Consumes: `plan_daily_entries`, `BudgetCandidate`, `PlannedEntry` from `trading.strategy.daily_budget`; `compute_paper_cash` from `trading.paper.reconcile`.
- Produces: new `_step_auto_open(conn, as_of, scored, regime, pool_capital, daily_cap, risk_pct, warnings) -> int`; `run_pre_open(..., pool_capital: float = 100_000.0, daily_deploy_cap: float = 7_000.0, risk_pct: float = 0.02, ...)`.

- [ ] **Step 1: Update the existing `_step_auto_open` unit tests (red)**

In `tests/test_jobs_pre_open.py`, the four `_step_auto_open(...)` calls at lines ~534, ~560, ~582/591, ~612 pass `capital=100_000.0, risk_pct=0.02`. Replace each `capital=100_000.0,` keyword with `pool_capital=100_000.0, daily_cap=100_000.0,` (a large daily cap so these existing single-candidate tests still open the trade). Concretely, each call becomes:

```python
    opened = _step_auto_open(
        conn,
        date(2026, 5, 15),
        [_sc(cand)],
        "NEUTRAL",
        pool_capital=100_000.0,
        daily_cap=100_000.0,
        risk_pct=0.02,
        warnings=warnings,
    )
```

Apply the same keyword change to all four call sites (`test_step_auto_open_creates_signal_and_paper_trade`, `test_step_auto_open_target_and_prediction_track_exit_engine`, `test_step_auto_open_idempotent_on_rerun` — both calls —, `test_step_auto_open_non_selected_logs_signal_only`).

Also update the orchestrator stub at line ~96–97:

```python
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_auto_open",
        lambda conn, as_of, scored, regime, pool_capital, daily_cap, risk_pct, warnings: 0,
    )
```

- [ ] **Step 2: Add a new failing test for the cash/budget gate**

Add to `tests/test_jobs_pre_open.py`:

```python
def test_step_auto_open_respects_daily_cap_and_cash(
    conn: sqlite3.Connection,
) -> None:
    """Two ₹100 candidates, daily_cap ₹150 → only ~1 share-worth opens;
    total opened notional stays within the cap."""
    warnings: list[str] = []
    cands = [_sc(_candidate("RVNL", 10)), _sc(_candidate("NTPC", 10))]
    opened = _step_auto_open(
        conn,
        date(2026, 5, 15),
        cands,
        "NEUTRAL",
        pool_capital=100_000.0,
        daily_cap=150.0,
        risk_pct=0.02,
        warnings=warnings,
    )
    assert opened == 1  # ₹150 cap / ₹100 entry = 1 share, one symbol
    deployed = conn.execute(
        "SELECT COALESCE(SUM(entry_price*qty),0) FROM paper_trades WHERE ts_exit IS NULL"
    ).fetchone()[0]
    assert deployed <= 150.0


def test_step_auto_open_no_cash_opens_nothing(
    conn: sqlite3.Connection,
) -> None:
    """available_cash ≤ 0 (a pre-existing huge deployed position) → no new opens."""
    # Seed an open trade that overdraws cash far below zero.
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days) "
        "VALUES ('2026-05-01T08:30:00','TCS','LONG',100.0,95.0,120.0,25)"
    )
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty) VALUES (?,?,?,?)",
        (cur.lastrowid, "2026-05-01T08:30:00", 100.0, 5000),  # ₹500k deployed
    )
    conn.commit()
    warnings: list[str] = []
    opened = _step_auto_open(
        conn,
        date(2026, 5, 15),
        [_sc(_candidate("RVNL", 10))],
        "NEUTRAL",
        pool_capital=100_000.0,
        daily_cap=7_000.0,
        risk_pct=0.02,
        warnings=warnings,
    )
    assert opened == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_jobs_pre_open.py -k step_auto_open -v`
Expected: FAIL (signature `TypeError` on the renamed kwargs and the new gate tests).

- [ ] **Step 4: Rewrite `_step_auto_open` and `run_pre_open`**

In `src/trading/jobs/pre_open.py`:

Add imports near the existing strategy imports:

```python
from trading.paper.reconcile import compute_paper_cash
from trading.strategy.daily_budget import BudgetCandidate, plan_daily_entries
```

Replace `run_pre_open`'s signature params `capital_per_trade: float = 100_000.0,` with:

```python
    pool_capital: float = 100_000.0,
    daily_deploy_cap: float = 7_000.0,
    risk_pct: float = 0.02,
```

Replace the `_step_auto_open` call site (currently passing `capital_per_trade, risk_pct,`) with:

```python
        opened = _step_auto_open(
            conn,
            as_of,
            scored,
            regime,
            pool_capital,
            daily_deploy_cap,
            risk_pct,
            warnings,
        )
```

Replace the whole `_step_auto_open` body with:

```python
def _step_auto_open(
    conn: sqlite3.Connection,
    as_of: date,
    scored: list[ScoredCandidate],
    regime: Regime,
    pool_capital: float,
    daily_cap: float,
    risk_pct: float,
    warnings: list[str],
) -> int:
    """Persist a signal for every scored candidate; open paper-trades for the
    selected ones the daily-budget planner can fund.

    Entry price = `cand.close` (D-1's close). The planner caps total new buys at
    `daily_cap` notional and at available cash, ranking by expected value, so the
    book paces instead of deploying every top-K pick against a fixed ₹1L (the
    over-deployment bug). Non-funded and non-selected candidates are logged as
    visibility-only signals with a skip reason.
    """
    available_cash = compute_paper_cash(conn, as_of=as_of)
    deployed_by_symbol = _deployed_by_symbol(conn)

    budget_cands: list[BudgetCandidate] = []
    signal_by_symbol: dict[str, Signal] = {}
    for sc in scored:
        cand = sc.candidate
        stop_price = cand.close - 1.5 * cand.atr_14
        if cand.close <= stop_price:
            warnings.append(f"{cand.symbol}: ATR={cand.atr_14:.2f} ≥ close — skip")
            continue
        signal_target = target_price(cand.close, stop_price)
        signal = Signal(
            id=None,
            ts=f"{as_of.isoformat()}T08:30:00",
            symbol=cand.symbol,
            side="LONG",
            entry=cand.close,
            stop=stop_price,
            target=signal_target,
            horizon_days=25,
            rules_passed_json=json.dumps([r.name for r in cand.rules if r.passed]),
            ml_score=sc.ml_score,
            created_by="pre_open",
        )
        signal_by_symbol[cand.symbol] = signal
        if not sc.selected:
            insert_signal(conn, signal)
            continue
        if _already_opened_today(conn, cand.symbol, as_of):
            continue
        budget_cands.append(
            BudgetCandidate(
                symbol=cand.symbol,
                entry=cand.close,
                stop=stop_price,
                target=signal_target,
                ml_score=sc.ml_score,
            )
        )

    plan = plan_daily_entries(
        budget_cands,
        available_cash=available_cash,
        deployed_by_symbol=deployed_by_symbol,
        regime=regime,
        pool_capital=pool_capital,
        daily_cap=daily_cap,
        risk_pct=risk_pct,
    )

    opened = 0
    planned_symbols = {e.symbol for e in plan.entries}
    for entry in plan.entries:
        signal = signal_by_symbol[entry.symbol]
        log_signal_and_open_trade(
            conn,
            signal=signal,
            entry_ts=signal.ts,
            entry_price=entry.entry,
            qty=entry.qty,
            atr_at_entry=None,
        )
        opened += 1

    # Visibility-only: any selected candidate the planner skipped logs a signal
    # plus its skip reason so the brief shows why it didn't open.
    for symbol, reason in plan.skipped:
        if symbol in planned_symbols:
            continue
        signal = signal_by_symbol.get(symbol)
        if signal is not None:
            insert_signal(conn, signal)
        warnings.append(f"{symbol}: not opened — {reason}")

    return opened


def _deployed_by_symbol(conn: sqlite3.Connection) -> dict[str, float]:
    """Cost-basis value of open paper positions, grouped by symbol."""
    rows = conn.execute(
        "SELECT s.symbol AS symbol, SUM(pt.entry_price * pt.qty) AS deployed "
        "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL GROUP BY s.symbol"
    ).fetchall()
    return {r["symbol"]: float(r["deployed"]) for r in rows}
```

Note: `atr_at_entry=None` — the prior code passed `cand.atr_14`. To preserve it, the planner's `PlannedEntry` does not carry ATR; keep entry-ATR by looking it up. Simplest faithful approach: keep a `atr_by_symbol: dict[str, float]` alongside `signal_by_symbol`, populated as `atr_by_symbol[cand.symbol] = cand.atr_14`, and pass `atr_at_entry=atr_by_symbol[entry.symbol]`. Add that dict next to `signal_by_symbol` and use it in the `log_signal_and_open_trade` call:

```python
        atr_by_symbol[cand.symbol] = cand.atr_14
```
```python
            atr_at_entry=atr_by_symbol[entry.symbol],
```

- [ ] **Step 5: Update the `pre-open` CLI command**

In `src/trading/cli.py`, the `pre-open` command (around line 1496): add a `--daily-cap` option and pass the renamed kwargs. Replace the option block + `run_pre_open(...)` call:

```python
@app.command("pre-open")
def pre_open_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    skip_news: Annotated[bool, typer.Option("--skip-news")] = False,
    capital: Annotated[float, typer.Option(help="Standing pool capital.")] = 100_000.0,
    daily_cap: Annotated[float, typer.Option(help="Max new buys per day (notional).")] = 7_000.0,
    risk_pct: Annotated[float, typer.Option(help="Risk per trade.")] = 0.02,
) -> None:
    """Phase 13 MVP — orchestrate Phases 1-12 and write the analyst bundle."""
    as_of = date.fromisoformat(date_str)
    try:
        result = run_pre_open(
            as_of,
            skip_news=skip_news,
            pool_capital=capital,
            daily_deploy_cap=daily_cap,
            risk_pct=risk_pct,
        )
```

- [ ] **Step 6: Run the pre_open tests**

Run: `uv run pytest tests/test_jobs_pre_open.py -v`
Expected: PASS. If `test_run_pre_open_full_happy_path_integration` (asserts `paper_trades_opened == candidates_passing`) or the line-786 test (`== candidates_selected`) now fail because the ₹7k cap opens fewer than all candidates, update those asserts to `<=` (the cap intentionally limits opens). Inspect each and relax to `result.paper_trades_opened <= result.candidates_selected`.

- [ ] **Step 7: Run the CLI + unattended + smoke tests**

Run: `uv run pytest tests/test_cli.py tests/test_jobs_daily_unattended.py tests/test_scheduled_jobs_smoke.py -v`
Expected: PASS (the `*a, **kw` monkeypatches and the default-kwarg `run_pre_open` call in `daily_unattended.py` are unaffected by the rename).

- [ ] **Step 8: Lint + type check**

Run: `uv run ruff check src/trading/jobs/pre_open.py src/trading/cli.py && uv run mypy src/trading/jobs/pre_open.py`
Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add src/trading/jobs/pre_open.py src/trading/cli.py tests/test_jobs_pre_open.py
git commit -m "feat(paper): cash-aware auto-open via daily-budget planner"
```

---

### Task 3: `funds top-up --to-available` CLI primitive

**Files:**
- Modify: `src/trading/cli.py` (new `funds top-up` command in the existing `funds_app`)
- Test: `tests/test_cli_funds.py`

**Interfaces:**
- Consumes: `add_funds` (`trading.paper.funds`), `compute_paper_cash` (`trading.paper.reconcile`), `compute_summary` (`trading.paper.positions`), existing `funds_app`, `console`, `get_paths`, `run_migrations`, `get_conn`.
- Produces: CLI command `trading funds top-up --to-available <amount> [--date YYYY-MM-DD]`.

- [ ] **Step 1: Write the failing test**

Check whether `tests/test_cli_funds.py` exists. If it does, append; otherwise create it:

```python
"""Tests for the `trading funds` CLI sub-app."""

from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from trading.cli import app
from trading.paper.reconcile import compute_paper_cash
from trading.store.db import get_conn
from trading.store.migrations import run_migrations

runner = CliRunner()


def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.config import get_paths

    return get_paths().db_path


def test_top_up_deposits_gap_to_target(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path, monkeypatch)
    with get_conn(db) as conn:
        run_migrations(conn)
    result = runner.invoke(
        app, ["funds", "top-up", "--to-available", "150000", "--date", "2026-06-19"]
    )
    assert result.exit_code == 0
    with get_conn(db) as conn:
        run_migrations(conn)
        cash = compute_paper_cash(conn, as_of=date(2026, 6, 19))
    assert abs(cash - 150_000.0) < 1e-6


def test_top_up_idempotent_when_already_funded(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path, monkeypatch)
    with get_conn(db) as conn:
        run_migrations(conn)
    runner.invoke(app, ["funds", "top-up", "--to-available", "150000", "--date", "2026-06-19"])
    result = runner.invoke(
        app, ["funds", "top-up", "--to-available", "150000", "--date", "2026-06-19"]
    )
    assert result.exit_code == 0
    assert "already" in result.stdout.lower()
    with get_conn(db) as conn:
        run_migrations(conn)
        n = conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0]
    assert n == 1  # second call wrote nothing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_funds.py -k top_up -v`
Expected: FAIL — `No such command 'top-up'` (exit code 2).

- [ ] **Step 3: Implement the command**

In `src/trading/cli.py`, after `funds_add_cmd` (before `funds_list_cmd`), ensure `compute_paper_cash` is imported (add `from trading.paper.reconcile import compute_paper_cash` if not already imported near the other paper imports) and add:

```python
@funds_app.command("top-up")
def funds_top_up_cmd(
    to_available: Annotated[
        float, typer.Option("--to-available", help="Target available cash in rupees.")
    ],
    as_of: Annotated[
        str | None,
        typer.Option("--date", help="Deposit date (YYYY-MM-DD). Defaults to today."),
    ] = None,
) -> None:
    """Deposit exactly enough to raise available cash to `--to-available`.

    Idempotent: if cash is already at or above the target, writes nothing.
    """
    paths = get_paths()
    deposit_date = as_of or date.today().isoformat()
    target_day = date.fromisoformat(deposit_date)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        cash = compute_paper_cash(conn, as_of=target_day)
        gap = to_available - cash
        if gap <= 0:
            console.print(
                f"[yellow]Already at/above ₹{to_available:,.0f} available "
                f"(cash ₹{cash:,.0f}).[/yellow] Nothing deposited."
            )
            return
        dep = add_funds(
            conn,
            amount=gap,
            date=deposit_date,
            note=f"top-up to ₹{to_available:,.0f} available",
        )
        summary = compute_summary(conn, as_of=target_day)
    console.print(
        f"[green]Deposited ₹{dep.amount:,.0f}[/green] on {dep.date} → "
        f"cash available ₹{summary.cash:,.0f}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_funds.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + type check**

Run: `uv run ruff check src/trading/cli.py tests/test_cli_funds.py && uv run mypy src/trading/cli.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/trading/cli.py tests/test_cli_funds.py
git commit -m "feat(cli): funds top-up --to-available (idempotent fund-to-target)"
```

---

### Task 4: One-time funding + snapshot regeneration (runbook)

**Files:**
- None (operational). This task funds the live DB and refreshes the equity curve.

**Interfaces:**
- Consumes: `trading funds top-up`, `trading post-close` CLI commands.

- [ ] **Step 1: Confirm full suite is green before touching live data**

Run: `uv run pytest -q`
Expected: PASS, no errors/warnings.

- [ ] **Step 2: Record the current cash for the runbook note**

Run: `uv run trading funds balance --date 2026-06-19`
Expected: prints current "Cash available" (a large negative number reflecting over-deployment). Note it.

- [ ] **Step 3: Fund available cash to ₹1,00,000**

Run: `uv run trading funds top-up --to-available 100000 --date 2026-06-19`
Expected: `Deposited ₹…` then `cash available ₹100,000`.

- [ ] **Step 4: Verify the balance**

Run: `uv run trading funds balance --date 2026-06-19`
Expected: "Cash available ₹100,000".

- [ ] **Step 5: Regenerate today's portfolio snapshot**

Run: `uv run trading post-close --date 2026-06-19 --apply`
Expected: writes/updates the `portfolio_snapshots` row for 2026-06-19; equity = cash (₹100k) + holdings value. (If `post-close` needs bars and reports missing symbols, that's the existing data-staleness behavior — note any warnings; the snapshot still writes with entry-price fallbacks per `compute_portfolio_snapshot`.)

- [ ] **Step 6: Commit the DB if it is tracked**

Run: `git status --short`
- If `data/trading.db` (or equivalent) appears as modified **and is tracked**, commit it:
```bash
git add data/trading.db
git commit -m "chore(paper): fund available cash to ₹1L + regenerate snapshot (2026-06-19)"
```
- If the DB is gitignored/untracked, skip — the funding lives in the local DB only. Do not force-add an ignored file.

---

## Final verification + push

- [ ] **Step 1: Full suite, lint, types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src/`
Expected: all green.

- [ ] **Step 2: Leak-check the staged history before pushing**

Run: `git log origin/main..HEAD -p | grep -Ec 'real.money|suspended.indefinitely'`
Expected: `0`. (The `.` is a wildcard matching the space, so this plan's own command does not trip the check. Confirms no F-005 sentinel wording entered any commit.)

- [ ] **Step 3: Confirm no protected untracked files were staged**

Run: `git status --short`
Expected: `.mcp.json`, `CLAUDE.md`, `Research/`, `data/README.md`, `data/mutual_funds_holdings.md`, `docs/daily-workflow.md`, and `docs/superpowers/plans/2026-06-19-f010-dormant-tables.md` remain `??` (untracked), never staged/committed.

- [ ] **Step 4: Push**

Run: `git push origin main`
Expected: the Task 1–4 commits (and this plan + the spec) land on origin/main.
