# Phase 5 — Rule Scanner (Layer A) Implementation Plan

**Goal:** A deterministic candidate generator that runs spec Section 4.1 filters over the universe each day and returns a ranked list of `Candidate` records — the input to Phase 6 (sizing) and Phase 7 (backtest).

**Architecture:** Pure functions, one per rule. Each takes a DataFrame (or symbol + context) and returns a `RuleResult` with `passed`/`reason`. An orchestrator `evaluate_symbol` runs them all and packages the outcome into a `Candidate` dataclass. `scan(paths, scan_date, ctx)` iterates the universe. Rules that depend on data we don't have yet (macro regime, F&O ban, critical sentiment events) accept an opt-in `ScanContext` and **pass-by-default** when the context is empty — so the scanner works today and tightens as Phases 8 / 9 light up the missing inputs.

**Tech Stack:** Pure Python + pandas. No new deps.

**Reference:** Spec Section 4.1 (13 filters), Section 11 (`src/trading/strategy/rules.py`).

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/trading/strategy/rules.py` | Rule functions + `Candidate` / `ScanContext` / `RuleResult` types + `scan()` orchestrator |
| Modify | `src/trading/cli.py` | Add `trading scan` command |
| Create | `tests/test_rules.py` | Per-rule pass/fail tests + orchestrator + scan |
| Modify | `tests/test_cli.py` | Add `scan` CLI test |
| Modify | `PROGRESS.md` | Tick 5.1-5.5 |

---

## Rule Inventory (from spec §4.1)

Active in Phase 5 (we have the data):
1. **uptrend** — close > 200-DMA AND 50-DMA > 200-DMA
2. **pullback** — |close − sma_20| ≤ 3% OR |close − sma_50| ≤ 3%
3. **rsi_band** — 30 ≤ RSI(14) ≤ 45
4. **volume_exhaustion** — on selling days, today's volume < 20-day avg
5. **liquidity** — 20-day avg (close × volume) > ₹10 cr/day
6. **no_recent_breakdown** — not down > 15% from 30-day high

Conditional in Phase 5 (depend on future-phase context; pass-by-default when context is empty):
7. **regime** — uses `ScanContext.india_vix` < 25 AND `nifty200_drawdown_5d_pct` > -5
8. **not_fno_banned** — symbol not in `ScanContext.fno_ban_symbols`
9. **not_t2t** — symbol not in `ScanContext.t2t_symbols`
10. **no_critical_event** — symbol not in `ScanContext.critical_event_symbols`

Deferred (need pre-open / earnings calendar data; not in this phase):
- earnings-window guard (needs corporate calendar)
- upper-circuit-today (needs intraday quote)
- pre-open gap (needs IEP from 08:55 sub-job)

The deferred rules will land alongside the data sources that feed them.

---

## Public API

```python
@dataclass(frozen=True)
class RuleResult:
    name: str
    passed: bool
    reason: str = ""

@dataclass(frozen=True)
class ScanContext:
    scan_date: date
    india_vix: float | None = None
    nifty200_drawdown_5d_pct: float | None = None
    fno_ban_symbols: frozenset[str] = frozenset()
    t2t_symbols: frozenset[str] = frozenset()
    critical_event_symbols: frozenset[str] = frozenset()

@dataclass(frozen=True)
class Candidate:
    symbol: str
    scan_date: date
    close: float
    rsi_14: float
    sma_20: float
    sma_50: float
    sma_200: float
    atr_14: float
    rules: tuple[RuleResult, ...]

    @property
    def all_passed(self) -> bool: ...

# Individual rules (DataFrame-based — expects add_indicators applied)
def passes_uptrend(df: pd.DataFrame) -> RuleResult: ...
def passes_pullback(df: pd.DataFrame, max_pct: float = 0.03) -> RuleResult: ...
def passes_rsi_band(df: pd.DataFrame, low: float = 30, high: float = 45) -> RuleResult: ...
def passes_volume_exhaustion(df: pd.DataFrame) -> RuleResult: ...
def passes_liquidity(df: pd.DataFrame, min_turnover_cr: float = 10.0) -> RuleResult: ...
def passes_no_recent_breakdown(df: pd.DataFrame, lookback: int = 30, max_drop: float = 0.15) -> RuleResult: ...

# Context-based rules
def passes_regime(ctx: ScanContext) -> RuleResult: ...
def passes_not_fno_banned(symbol: str, ctx: ScanContext) -> RuleResult: ...
def passes_not_t2t(symbol: str, ctx: ScanContext) -> RuleResult: ...
def passes_no_critical_event(symbol: str, ctx: ScanContext) -> RuleResult: ...

# Orchestration
def evaluate_symbol(symbol: str, df: pd.DataFrame, ctx: ScanContext) -> Candidate: ...
def scan(paths: Paths, scan_date: date, *, symbols: list[str] | None = None,
         ctx: ScanContext | None = None) -> list[Candidate]: ...
def passing(candidates: list[Candidate]) -> list[Candidate]: ...
```

Conventions:
- Every rule returns `RuleResult(name, passed, reason)` — never raises for "rule failed"; only raises on programmer error.
- When indicators are NaN (insufficient history), the rule **fails** with `reason="insufficient history"`. We don't accidentally pass a stock we can't evaluate.
- The orchestrator never crashes — `scan` skips symbols with < 200 bars (can't compute SMA 200).

---

## CLI

```python
@app.command("scan")
def scan_cmd(
    as_of: Annotated[str | None, typer.Option("--date", help="Scan date (default: latest available).")] = None,
    show_all: Annotated[bool, typer.Option("--show-all")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run Layer A rules across the universe; print the passing candidates."""
```

Output:
- Default: Rich table of passing candidates with close, RSI, distance-to-SMAs
- `--show-all`: include failing candidates with per-rule reasons
- `--json`: emit `[{"symbol": ..., "rules": [...], ...}]`

`--date` is `YYYY-MM-DD`. If not given, use the latest date found in any parquet (so it works without me knowing the exact ingest end-date).

---

## Tests

`tests/test_rules.py`:
- One pass + one fail test per rule (~12 tests)
- `evaluate_symbol` returns a populated `Candidate` with all rules attached
- `scan(paths, ...)` skips symbols with < 200 bars
- `Candidate.all_passed` only true when every rule passed
- Pass-by-default for context rules when context is empty
- Liquidity rule converts to crore correctly (10cr threshold = ₹10⁸)

`tests/test_cli.py` (+):
- `trading scan --date YYYY-MM-DD` runs without error against a small fixture parquet
- `--show-all` includes failures
- `--json` produces parseable JSON

## Tasks (TDD order)

1. Write tests (failing).
2. Implement `src/trading/strategy/rules.py`.
3. Add `scan` CLI to `cli.py`.
4. Add CLI test in `test_cli.py`.
5. Lint / type / pytest → green.
6. Smoke run: `uv run trading scan --date 2025-02-28` on the 3 parquets we already have.
7. PROGRESS.md → commit.
