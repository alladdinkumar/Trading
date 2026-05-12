# Phase 6 — Sizing + Exits Implementation Plan

**Goal:** Two pure modules that turn a `Candidate` (Phase 5) plus a running `TradeState` into (a) a position size honouring the 2%-risk budget and concurrency caps, and (b) a deterministic exit decision per end-of-day bar.

**Architecture:** No I/O, no network, no DB. Just frozen dataclasses + functions. Both modules are inputs to Phase 7 (backtest) — the backtest engine drives them in a loop over historical bars.

**Reference:** Spec §4.4 (entry/exit/sizing rules), §4.5 (kill-switch overrides), §9 (paper-trade lifecycle).

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/trading/strategy/sizing.py` | `position_size(...)`, regime multiplier, concurrency caps |
| Create | `src/trading/strategy/exits.py` | `evaluate_exit(...)` state machine, target/stop/time/trail |
| Create | `tests/test_sizing.py` | Branch coverage for the formula + caps + regime |
| Create | `tests/test_exits.py` | One test per exit branch + trailing edge cases |
| Modify | `PROGRESS.md` | Tick 6.1-6.5 |

No CLI command in Phase 6 — sizing/exits aren't user-facing yet. They surface via the backtest engine (Phase 7) and the paper ledger (Phase 11).

---

## sizing.py — Public API

```python
Regime = Literal["RISK_ON", "NEUTRAL", "RISK_OFF"]

@dataclass(frozen=True)
class SizingInput:
    capital: float              # total capital available (e.g. ₹100,000)
    entry: float                # signal entry price
    stop: float                 # signal stop price (entry > stop for LONG)
    risk_pct: float = 0.02      # 2% of capital per trade (spec §4.4)
    regime: Regime = "RISK_ON"  # spec §4.5 multiplier
    deployed_in_symbol: float = 0.0  # ₹ already in this symbol
    deployed_in_sector: float = 0.0  # ₹ already in this sector
    max_per_stock_pct: float = 0.25  # ≤25% capital per stock (spec §4.4)
    max_per_sector_pct: float = 0.30 # ≤30% capital per sector (spec §4.4)

@dataclass(frozen=True)
class SizingResult:
    qty: int                    # integer shares (floor)
    notional: float             # qty × entry
    capital_at_risk: float      # qty × (entry − stop)
    reasons: tuple[str, ...]    # human-readable caps that bound qty

REGIME_MULTIPLIER: dict[Regime, float] = {
    "RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.5,
}

def position_size(inp: SizingInput) -> SizingResult: ...
```

**Algorithm:**

1. Validate: `entry > stop > 0`, `capital > 0`, `0 < risk_pct ≤ 1`. Otherwise raise `ValueError`.
2. Per-share risk = `entry − stop`.
3. Risk budget = `capital × risk_pct × REGIME_MULTIPLIER[regime]`.
4. Risk-based qty = `floor(risk_budget / per_share_risk)`.
5. Per-stock cap qty = `floor((max_per_stock_pct × capital − deployed_in_symbol) / entry)`.
6. Per-sector cap qty = `floor((max_per_sector_pct × capital − deployed_in_sector) / entry)`.
7. `qty = max(0, min(risk_qty, stock_qty, sector_qty))`.
8. `reasons` records which constraint was the *binding* one (≥1 reasons OK — ties listed in order).

**Edge cases:**

- entry == stop → `ValueError` (would mean infinite size).
- Risk budget too small for even 1 share → qty=0, `reasons=("budget too small for 1 share",)`.
- deployed_in_symbol ≥ cap → qty=0.
- Regime RISK_OFF + tight stop → qty floored to 0 in some scenarios (correct behaviour).

---

## exits.py — Public API

```python
ExitAction = Literal["HOLD", "EXIT_TARGET", "EXIT_STOP", "EXIT_TIME"]

@dataclass(frozen=True)
class TradeState:
    entry: float
    initial_stop: float    # never moves
    current_stop: float    # ratchets up via trailing
    atr_at_entry: float    # used by 1×ATR trail
    days_held: int         # trading days since entry (0 on entry day)

@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float

@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    new_stop: float
    exit_price: float | None  # None when action == HOLD
    reason: str

TIME_STOP_DAYS = 25
TARGET_PCT = 0.20
TARGET_RR = 2.5
TRAIL_TRIGGER_BREAKEVEN = 0.10  # +10% → stop to entry
TRAIL_TRIGGER_ATR = 0.15        # +15% → 1×ATR trail

def evaluate_exit(trade: TradeState, bar: Bar) -> ExitDecision: ...
```

**State machine (one call per bar, end-of-day):**

1. Compute `target_price = min(entry × 1.20, entry + 2.5 × (entry − initial_stop))` — "whichever comes first" = the lower price.
2. **Intra-bar stop check** (conservative, fires before target if both hit same bar): `bar.low ≤ current_stop` → `EXIT_STOP` at `current_stop`.
3. **Intra-bar target check:** `bar.high ≥ target_price` → `EXIT_TARGET` at `target_price`.
4. **Time stop (end-of-bar):** `days_held ≥ 25` AND `bar.close ≤ entry` → `EXIT_TIME` at `bar.close`.
5. **Trailing update (HOLD):** based on `bar.close`:
   - `move_pct = (close − entry) / entry`
   - If `move_pct ≥ 0.15`: `new_stop = max(current_stop, close − atr_at_entry)`
   - Else if `move_pct ≥ 0.10`: `new_stop = max(current_stop, entry)` (breakeven)
   - Else: `new_stop = current_stop`
6. Return `HOLD` with the (possibly upgraded) `new_stop`.

**Invariants:**

- Stops never ratchet down — enforced by `max(...)`.
- Same-bar tie (stop + target both hit) → stop wins (conservative; matches spec §8 "stops triggered intra-day at the stop price (not at low — conservative)").
- Time stop only fires if **not in profit** at close (`close ≤ entry`).
- Trailing only updates on `HOLD` outcomes — exits return the `current_stop` unchanged (no spurious updates after exit).

---

## Test Plan

### sizing (~10 tests)

| Test | What it pins |
|---|---|
| `test_basic_size` | qty = floor(2000 / 5) = 400 for 100→95 stop, ₹100K capital |
| `test_neutral_regime_halves_then_some` | RISK_ON 1.0× → 400 shares, NEUTRAL 0.75× → 300, RISK_OFF 0.5× → 200 |
| `test_stock_cap_binds` | Tight stop → risk-qty huge; capped at 25% capital / entry |
| `test_sector_cap_binds` | deployed_in_sector pushes available below per-stock cap |
| `test_already_capped_returns_zero` | deployed_in_symbol == max_per_stock_pct × capital → qty=0 |
| `test_entry_equals_stop_raises` | `ValueError` |
| `test_negative_inputs_raise` | capital<0, entry<0, risk_pct=0 or >1 |
| `test_budget_too_small_returns_zero` | tiny capital + wide stop → qty=0 with reason |
| `test_reasons_lists_binding_constraint` | Each cap when it binds appears in `reasons` |
| `test_capital_at_risk_matches_qty` | invariant: result.capital_at_risk == qty × (entry-stop) |

### exits (~12 tests)

| Test | What it pins |
|---|---|
| `test_stop_hit` | bar.low ≤ current_stop → EXIT_STOP at current_stop |
| `test_target_hit_rr_first` | tight stop → R/R target < +20%; high ≥ R/R → EXIT_TARGET at R/R price |
| `test_target_hit_pct_first` | wide stop → +20% target < R/R; high ≥ entry×1.20 → EXIT_TARGET at +20% |
| `test_time_stop_at_loss` | days_held=25, close < entry → EXIT_TIME at close |
| `test_time_stop_not_triggered_in_profit` | days_held=25, close > entry → HOLD |
| `test_time_stop_not_triggered_before_25d` | days_held=24, close < entry → HOLD |
| `test_trail_breakeven` | close at +10% → new_stop = entry |
| `test_trail_atr` | close at +15% → new_stop = close − atr_at_entry |
| `test_trail_never_ratchets_down` | next bar lower close still > +10% → new_stop unchanged |
| `test_stop_wins_when_tied_same_bar` | low ≤ stop AND high ≥ target → EXIT_STOP (conservative) |
| `test_hold_keeps_current_stop` | quiet bar → HOLD, new_stop unchanged |
| `test_exit_returns_current_stop_not_trailed` | EXIT branches don't trigger trailing update |

---

## Verification

1. `uv run ruff format src/trading/strategy/sizing.py src/trading/strategy/exits.py`
2. `uv run ruff check src/trading/strategy/`
3. `uv run mypy src/trading/`
4. `uv run pytest tests/test_sizing.py tests/test_exits.py -v` — all green
5. `uv run pytest -q` — full suite still green
6. Update `PROGRESS.md` (6.1-6.5 → `[x]`, current → Phase 7)
7. Commit `feat(strategy): sizing + exits (Phase 6)`

---

## Out of scope (deferred)

- **Max-concurrent-positions cap** — spec §4.4 doesn't give an integer; we'll add it as a sizing-time check in Phase 11 (ledger) where we know how many positions are already open.
- **Kill-switches** (spec §4.5 toggles like "Paper-trade 30d DD > 8% → freeze entries") — orchestration concern; lives in Phase 13 pre_open job.
- **STT/slippage in exit-price** — handled by the cost model in Phase 7. `evaluate_exit` returns the raw price; the backtest engine applies fills + costs on top.
- **Multi-day intraday simulation** — we model one bar = one trading day. Intra-bar ordering (stop-then-target tie) follows spec §8.
