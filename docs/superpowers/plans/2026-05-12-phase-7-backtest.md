# Phase 7 — Backtest Engine Implementation Plan

**Goal:** A pure-Python event-loop backtester that drives Phase 5 (scanner) + Phase 6 (sizing + exits) over historical bars, applies Zerodha-realistic costs, and emits a metrics bundle plus a trade log.

**Architecture deviation from spec §8.1:** spec calls for `vectorbt`. We use a plain Python loop instead because (a) Phase 6's `evaluate_exit` is stateful per trade (trailing stops, days_held, intra-bar tie-break) and would force a Numba-jitted `Portfolio.from_order_func` in vectorbt — heavy and harder to test; (b) the same engine code can be reused in Phase 11 (paper ledger MTM), giving us one source of truth. Performance is fine: ~200 symbols × 750 bars × a handful of WF folds runs in seconds.

**Reference:** Spec §8 (backtest framework) — costs, walk-forward, metrics, anti-bias safeguards.

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/trading/backtest/__init__.py` | Re-exports public API |
| Create | `src/trading/backtest/costs.py` | `CostConfig`, `buy_charges`, `sell_charges`, `apply_slippage` |
| Create | `src/trading/backtest/metrics.py` | `MetricsBundle`, `compute_metrics`, individual stat fns |
| Create | `src/trading/backtest/engine.py` | `BacktestConfig`, `Trade`, `BacktestResult`, `run_backtest` |
| Create | `src/trading/backtest/walkforward.py` | `WalkForwardConfig`, `windows()`, `run_walkforward` |
| Modify | `src/trading/cli.py` | Add `backtest` subcommand |
| Create | `tests/test_costs.py` | Cost-model math |
| Create | `tests/test_metrics.py` | Metric fns vs hand-computed values |
| Create | `tests/test_engine.py` | Synthetic 1- and 2-symbol scenarios |
| Create | `tests/test_walkforward.py` | Window enumeration + integration |
| Modify | `PROGRESS.md` | Tick 7.1-7.8 |

---

## costs.py — Public API

```python
@dataclass(frozen=True)
class CostConfig:
    brokerage_pct: float = 0.0003       # 0.03%
    brokerage_cap: float = 20.0          # ₹20 per order
    stt_buy_pct: float = 0.001           # 0.1% on buy turnover
    stt_sell_pct: float = 0.001          # 0.1% on sell turnover
    exchange_tx_pct: float = 0.0000297   # 0.00297% per side
    sebi_pct: float = 0.000001           # 0.0001% per side
    stamp_duty_pct: float = 0.00015      # 0.015% on buy only
    gst_pct: float = 0.18                # 18% on (brokerage + exchange + sebi)
    slippage_pct: float = 0.001          # 0.1% per side, applied as price shift

@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    total: float

def buy_charges(value: float, cfg: CostConfig = CostConfig()) -> CostBreakdown: ...
def sell_charges(value: float, cfg: CostConfig = CostConfig()) -> CostBreakdown: ...
def apply_slippage(price: float, side: Literal["buy", "sell"], cfg: CostConfig = CostConfig()) -> float: ...
```

**Semantics:**

- `buy_charges(value)` returns the explicit charges paid *in addition* to `value`. Total cash debited for the buy = `value + charges.total`.
- `sell_charges(value)` similarly — cash credited = `value − charges.total`.
- `apply_slippage` returns `price × (1 + slippage)` on buy, `price × (1 − slippage)` on sell. Slippage is a price shift, not a fee.
- Round-trip total cost (slippage + explicit) on a flat trade ≈ 0.4% — pinned by a sanity test.

---

## metrics.py — Public API

```python
@dataclass(frozen=True)
class MetricsBundle:
    cagr: float                # annualized
    sharpe: float              # annualized, rf=0
    sortino: float             # annualized, rf=0
    max_drawdown: float        # negative, e.g. -0.142
    hit_rate: float            # fraction of trades with net_pnl > 0
    profit_factor: float       # gross_profit / gross_loss
    expectancy: float          # mean net_pnl per trade
    avg_r_multiple: float      # mean (net_pnl / initial_risk) per trade
    total_trades: int
    total_costs: float
    alpha_annualized: float | None  # None if no benchmark passed
    beta: float | None

def cagr(equity: pd.Series) -> float
def sharpe(returns: pd.Series, periods_per_year: int = 252) -> float
def sortino(returns: pd.Series, periods_per_year: int = 252) -> float
def max_drawdown(equity: pd.Series) -> float
def hit_rate(trades: Sequence[Trade]) -> float
def profit_factor(trades: Sequence[Trade]) -> float
def expectancy(trades: Sequence[Trade]) -> float
def avg_r_multiple(trades: Sequence[Trade]) -> float
def alpha_beta(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> tuple[float, float]

def compute_metrics(result: BacktestResult, benchmark: pd.Series | None = None) -> MetricsBundle
```

**Edge cases:**

- Empty trade list → `hit_rate=0.0`, `profit_factor=0.0`, `expectancy=0.0`. Don't divide by zero.
- All-winning trades → `profit_factor = inf` (caller decides whether to display "∞").
- Zero-variance returns → `sharpe=0.0` (not NaN/inf).
- Equity series with 0 or 1 points → `cagr=0.0`, `max_drawdown=0.0`.

---

## engine.py — Public API

```python
@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 500_000          # ₹5L
    risk_pct: float = 0.02
    atr_stop_multiple: float = 1.5
    max_per_stock_pct: float = 0.25
    max_per_sector_pct: float = 0.30
    regime: Regime = "RISK_ON"                # static for Phase 7; Phase 9 will vary
    costs: CostConfig = field(default_factory=CostConfig)
    sector_of: Callable[[str], str] | None = None   # default: all in "UNKNOWN"

@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float                # post-slippage
    initial_stop: float
    qty: int
    exit_date: pd.Timestamp | None    # None ⇒ still open at end
    exit_price: float | None
    exit_reason: str                  # "STOP" | "TARGET" | "TIME" | "OPEN_AT_END"
    gross_pnl: float                  # qty × (exit − entry)
    costs_paid: float                 # explicit (sum of buy + sell CostBreakdown.total)
    net_pnl: float                    # gross_pnl − costs_paid

@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    trades: tuple[Trade, ...]
    equity_curve: pd.Series           # indexed by date
    daily_returns: pd.Series          # equity.pct_change().fillna(0)
    total_costs: float
    final_cash: float

def run_backtest(
    enriched: Mapping[str, pd.DataFrame],
    config: BacktestConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    ctx_for: Callable[[pd.Timestamp], ScanContext] | None = None,
) -> BacktestResult
```

**Loop structure (per date `d` in sorted union of all symbols' dates within `[start, end]`):**

1. **Open queued entries from previous EOD.** For each pending `(symbol, qty, stop)` from day `d-1`'s signals:
   - Buy at `apply_slippage(open_of_d, "buy")`.
   - Deduct `value + buy_charges(value).total` from cash.
   - Insert into open positions dict with `TradeState(entry, initial_stop=stop, current_stop=stop, atr_at_entry, days_held=0)`.
   - If cash insufficient (sized at signal time but market gapped), skip the entry and log it.
2. **Evaluate exits on open positions** using `bar_of_d` for each symbol:
   - Call `evaluate_exit(state, Bar(open, high, low, close))`.
   - On EXIT_*, sell at `apply_slippage(decision.exit_price, "sell")`, credit cash with `value − sell_charges(value).total`, finalize the `Trade`.
   - On HOLD, update `current_stop` and bump `days_held` for next bar.
3. **Mark-to-market.** Equity = cash + Σ qty × close_of_d for open positions. Record into the equity curve at `d`.
4. **Scan at EOD** for new entries. For each symbol with `df` containing `d`:
   - Slice `df` up to `d` inclusive, run `evaluate_symbol(symbol, slice, ctx_for(d) or ScanContext(d))`.
   - If `candidate.all_passed`:
     - Skip if already long this symbol.
     - Compute `entry_planned = close_of_d` (informational; actual fill is tomorrow's open + slippage).
     - Compute `stop = close − atr_stop_multiple × atr_14`.
     - Compute `deployed_in_symbol` from open positions; `deployed_in_sector` from `sector_of`.
     - Call `position_size(SizingInput(capital=equity, entry=entry_planned, stop=stop, ...))`.
     - If `qty > 0`, queue to open tomorrow.
5. End of loop. Any positions still open at `end`: close at the last bar's close (no costs — they're unrealized), label `exit_reason="OPEN_AT_END"`.

**Anti-bias (spec §8.6):**

- All scanner inputs use only data through `d` (inclusive). Open queued from `d`'s close opens at `d+1`'s open — no same-bar fills.
- Survivorship is currently bias-free only insofar as the parquet universe covers the period. Phase 16 will introduce historical Nifty 200 membership; for Phase 7 baseline we operate on whatever symbols we ingested.
- Stops fire intra-bar at the stop price (not at low) — matches `evaluate_exit` and §8.6 "conservative fills."

---

## walkforward.py — Public API

```python
@dataclass(frozen=True)
class WalkForwardConfig:
    train_years: float = 3.0
    test_months: float = 6.0
    step_months: float = 3.0

@dataclass(frozen=True)
class Window:
    train_start: pd.Timestamp
    train_end: pd.Timestamp     # exclusive of test_start
    test_start: pd.Timestamp
    test_end: pd.Timestamp

def windows(start: pd.Timestamp, end: pd.Timestamp, cfg: WalkForwardConfig) -> list[Window]
def run_walkforward(
    enriched: Mapping[str, pd.DataFrame],
    bt_config: BacktestConfig,
    wf_config: WalkForwardConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[BacktestResult, list[Window]]:
    """Concatenate test-window trades into a single BacktestResult covering the full OOS span."""
```

For Phase 7's rules-only baseline `train_*` is unused (no ranker yet), but kept in the signature so Phase 16 just slots in. `run_walkforward` runs the engine on each test window with the same initial capital and concatenates trades; the equity curve is stitched per fold.

---

## CLI

```
trading backtest [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--capital 500000] [--walk-forward]
```

Writes a markdown report to `data/research/backtest_<timestamp>.md`:

- Metrics table (MetricsBundle as rows)
- First 20 / last 20 trades
- Cost breakdown summary (total ₹ vs gross P&L)
- Notes on universe coverage (n_symbols, date range)

---

## Test Plan

### tests/test_costs.py (~6 tests)

| Test | Pins |
|---|---|
| `test_buy_charges_components` | STT 0.1%, exchange, SEBI, stamp 0.015%, GST 18% on (brokerage+exchange+sebi) — hand-computed for ₹100,000 buy |
| `test_sell_charges_no_stamp` | sell side has no stamp duty |
| `test_brokerage_capped_at_20` | turnover where 0.03% > ₹20 → brokerage clamps to ₹20 |
| `test_brokerage_below_cap` | small turnover → brokerage = 0.03% |
| `test_slippage_buy_and_sell` | buy returns price × 1.001, sell returns price × 0.999 |
| `test_round_trip_total_around_0_4_pct` | full buy+sell on flat trade ≈ 0.4% of gross |

### tests/test_metrics.py (~12 tests)

| Test | Pins |
|---|---|
| `test_cagr_known_doubling` | equity doubles in 1 year → cagr ≈ 1.0 |
| `test_cagr_short_series_is_zero` | <2 points → 0.0 |
| `test_sharpe_constant_return_is_zero` | zero-variance returns → 0.0 (no NaN/inf) |
| `test_sharpe_known_value` | hand-computed for a small series |
| `test_sortino_only_penalizes_downside` | upside-only returns → sortino > sharpe |
| `test_max_drawdown_monotonic_curve` | strictly increasing → 0.0 |
| `test_max_drawdown_known_curve` | 100→120→80 → −0.333 |
| `test_hit_rate_empty_zero` | no trades → 0.0 |
| `test_profit_factor_all_wins_inf` | only winners → inf |
| `test_expectancy_mean_net_pnl` | average matches manual |
| `test_alpha_beta_self_is_one_zero` | benchmark = strategy → β=1.0, α=0.0 |
| `test_compute_metrics_bundle_all_fields` | full integration — bundle populated end-to-end |

### tests/test_engine.py (~8 tests)

| Test | Pins |
|---|---|
| `test_single_symbol_no_signal_zero_trades` | flat data fails uptrend → 0 trades, equity flat = initial_capital |
| `test_signal_fires_opens_next_day` | engineered DataFrame with a passing setup → 1 trade opened at d+1's open |
| `test_exit_stop_fires_realises_loss` | engineered drop → EXIT_STOP, net_pnl < 0, costs > 0 |
| `test_exit_target_fires_realises_win` | engineered +20% bar → EXIT_TARGET, net_pnl > 0 |
| `test_costs_deducted_from_pnl` | gross_pnl = qty × (exit − entry); net_pnl < gross_pnl by exactly costs_paid |
| `test_cash_conservation` | cash + Σ(positions × close) at every bar consistent with final_cash on close-out |
| `test_open_at_end_marks_unrealised` | position still open on `end` → exit_reason="OPEN_AT_END", exit_price = last close |
| `test_no_lookahead_signal_uses_only_past` | scanner slice never includes future bars (assert via mock) |

### tests/test_walkforward.py (~3 tests)

| Test | Pins |
|---|---|
| `test_windows_count_and_dates` | 5y span, 3y/6mo/3mo → 5 windows with expected boundaries |
| `test_windows_no_overlap_test_segments` | test_end[i] ≤ test_start[i+1] for adjacent windows |
| `test_run_walkforward_integration` | small synthetic dataset; result.trades is concat of per-window trades |

---

## Verification

1. `uv run ruff format src/trading/backtest/ tests/test_costs.py tests/test_metrics.py tests/test_engine.py tests/test_walkforward.py`
2. `uv run ruff check .`
3. `uv run mypy src/trading/`
4. `uv run pytest tests/test_costs.py tests/test_metrics.py tests/test_engine.py tests/test_walkforward.py -v` — all green
5. `uv run pytest -q` — full suite still green (181 → 210+ tests)
6. Smoke: `uv run trading backtest --start 2023-01-01 --end 2025-12-31` on the ingested ~59-symbol universe; verify trades fire, equity finite, costs reasonable
7. Update `PROGRESS.md` (7.1–7.8 → `[x]`)
8. Commit `feat(backtest): engine + walk-forward (Phase 7)`

---

## Out of scope (deferred to later phases)

- **Historical Nifty 200 membership** — survivorship bias mitigation needs monthly index snapshots; Phase 16 baseline.
- **Sector classification** — without a real `sector_of` map, the 30%-sector cap collapses into a single bucket. Acceptable for Phase 7 since the 25% stock cap binds first for high-priced names. Phase 10 (portfolio analyzer) will provide a real sector mapper.
- **LightGBM ranker integration** — walk-forward `train_start/train_end` are present in the API but unused. Phase 16 swaps in retraining.
- **Regime-varying multiplier** — `BacktestConfig.regime` is static. Phase 9 will provide a regime time series.
- **Intraday backtest** — bar = one trading day. Same-bar stop+target tie resolves per `evaluate_exit`.
- **Position pyramiding / scale-ins** — one open trade per symbol at a time.
- **Short selling** — long-only baseline; spec §2.3 keeps shorting out of scope.
