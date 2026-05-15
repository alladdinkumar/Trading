# Phase 13 — pre_open Job (MVP) Design

**Date:** 2026-05-15
**Status:** Approved (autonomous execution per user)
**Predecessors:**
- [Phase 12 LLM-skill design](2026-05-15-phase-12-llm-skill-design.md)
- [Phase 12.5 data-quality cleanup](2026-05-15-phase-12-5-data-quality-design.md)

## 1. Context & motivation

Phase 13 is the MVP milestone — the first end-to-end "it works" moment.
A single command (`trading pre-open --date YYYY-MM-DD`) runs every upstream
phase in dependency order, produces the input context bundle, and auto-opens
paper-trades for any signals that fire.

The design respects the file-handshake contract from Phase 12: pre_open
HALTS after writing `_context.md` and tells the user to run `/analyst`
followed by `trading brief compile`. Pre_open does not invoke the LLM
itself.

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  trading pre-open --date YYYY-MM-DD                                │
│  (or `python -m trading.jobs.pre_open`)                            │
│                                                                    │
│  1. Macro snapshot     ← yfinance + nse_fiidii → macro_snapshot    │
│  2. News ingest        ← fetch_all_news → FinBERT → news_items     │
│                          + sentiment_daily for alias-map universe  │
│  3. Scanner            ← scan(paths, as_of, ctx) over parquet      │
│  4. Portfolio health   ← Kite holdings → score_holding each        │
│                          (graceful empty if Kite token absent)     │
│  5. Auto-log fired     ← passing(candidates) → log_signal_and_     │
│     signals              open_trade (entry = D-1 close)            │
│  6. Assemble context   ← assemble_context(conn, …, inputs)         │
│                          → data/research/YYYY-MM-DD/_context.md    │
│  7. HALT — print:                                                  │
│       "Now run /analyst skill, then                                │
│        trading brief compile --date YYYY-MM-DD"                    │
└────────────────────────────────────────────────────────────────────┘
```

In-process invocation: `pre_open.py` imports each phase's public surface
directly (no subprocess). Each step is its own private function so the
orchestrator stays readable and individual steps stay unit-testable.

## 3. Components

### 3.1 New file — `src/trading/jobs/pre_open.py`

```python
@dataclass(frozen=True)
class PreOpenResult:
    """What pre_open produced. Returned by run_pre_open for tests + CLI."""
    as_of: date
    bundle_path: Path
    macro_written: bool          # macro_snapshot row upserted
    news_inserted: int           # news_items rows added
    sentiment_rows: int          # sentiment_daily rows written
    candidates_total: int        # all scanner outputs
    candidates_passing: int      # all-10-pass subset
    paper_trades_opened: int     # = candidates_passing (one per fired signal)
    holdings_scored: int         # = 0 when Kite token absent
    warnings: list[str]          # graceful-degradation notes


def run_pre_open(
    as_of: date,
    *,
    paths: Paths | None = None,
    skip_news: bool = False,
    skip_kite: bool = False,
    capital_per_trade: float = 100_000.0,
    risk_pct: float = 0.02,
) -> PreOpenResult:
    """Orchestrate Phases 1-12 for `as_of` and write the analyst bundle.

    Each upstream step is its own private function (`_step_macro`,
    `_step_news`, `_step_scan`, `_step_portfolio`, `_step_auto_open`,
    `_step_assemble`) so they can be unit-tested in isolation. The
    orchestrator threads the results through, collecting warnings on
    graceful-degradation paths.
    """
```

`skip_news` and `skip_kite` are convenience flags for fast iteration
during development (news ingest takes ~30s with FinBERT on first run).
Default behaviour matches the "real" run.

### 3.2 New CLI command — extension to `src/trading/cli.py`

```
trading pre-open --date YYYY-MM-DD
                 [--skip-news]
                 [--skip-kite]
                 [--capital 100000] [--risk-pct 0.02]
```

Prints a Rich summary table after completion (steps run, counts,
warnings) and the next-step instruction:

> Now run `/analyst` skill in Claude Code, then
> `trading brief compile --date YYYY-MM-DD`

### 3.3 Module-execution entry — `__main__` block

```python
if __name__ == "__main__":  # pragma: no cover
    import typer
    typer.run(_main)


def _main(
    date_str: str = typer.Option(..., "--date"),
    skip_news: bool = False,
    skip_kite: bool = False,
) -> None:
    """Allows `python -m trading.jobs.pre_open --date YYYY-MM-DD`."""
    result = run_pre_open(date.fromisoformat(date_str),
                          skip_news=skip_news, skip_kite=skip_kite)
    print(f"wrote {result.bundle_path}")
```

### 3.4 Windows launcher — `scripts/pre_open.bat`

```bat
@echo off
cd /d "%~dp0\.."
uv run python -m trading.jobs.pre_open --date %DATE_ISO%
```

The launcher is the entry point for Phase 17's Task Scheduler. For Phase
13 it just exists; users invoke it manually.

## 4. Data flow per step

### 4.1 Step `_step_macro(conn, as_of) -> tuple[bool, Regime]`

Calls `snapshot_and_classify(as_of)` (which returns `(MacroSnapshot,
RegimeResult)`) then `upsert_macro_snapshot(conn, snap)`. Returns
`(True, regime_result.regime)` on success, `(False, "NEUTRAL")` (with
warning) if yfinance fails. The regime is threaded into `_step_auto_open`
so position sizing applies the regime multiplier (spec §4.5).

### 4.2 Step `_step_news(conn, as_of) -> tuple[int, int]`

Returns `(news_inserted, sentiment_rows)`.

```python
items = fetch_all_news()
scored = score_news_items(items)
inserted = insert_news_items(conn, scored)
watched = sorted(DEFAULT_ALIASES.keys())
rollups = aggregate_daily(conn, watched, as_of)
return inserted, len(rollups)
```

Single-source failures degrade gracefully (Phase 8 contract). FinBERT
loads ~440MB once; subsequent runs use the cached model.

### 4.3 Step `_step_scan(paths, as_of) -> list[Candidate]`

```python
ctx = ScanContext(scan_date=as_of)
return scan(paths, as_of, ctx=ctx)
```

Reuses Phase 5 directly. The full candidate list (passing + failing) is
threaded into the bundle for narrative context; only `passing(...)` are
auto-opened as paper-trades.

### 4.4 Step `_step_portfolio(paths, as_of, settings) -> list[HealthScore]`

Conditional on Kite token presence:

```python
if not settings.kite_access_token or skip_kite:
    return []
try:
    client = make_client(settings)
    holdings = get_holdings(client)
except KiteAuthError:
    return []  # warn

results = []
for h in holdings:
    history = read_ohlcv(h.tradingsymbol, paths)
    technicals = technicals_from_history(history)
    sentiment = SentimentSnapshot()  # MVP: no per-holding aggregate yet
    fundamentals = FundamentalsSnapshot()  # MVP: yfinance fundamentals deferred
    ctx = HoldingContext(symbol=h.tradingsymbol, technicals=technicals,
                         fundamentals=fundamentals, sentiment=sentiment)
    results.append(score_holding(ctx))
return results
```

The fundamentals slice stays empty for MVP (yfinance `Ticker.info` is
sparse for Indian equities and the Phase 10 health module already
handles all-Optional gracefully). GTT viability projection is also
deferred — `holdings_health` is enough for the bundle's "Holdings health"
section.

### 4.5 Step `_step_auto_open(conn, as_of, frames, passing, capital, risk_pct) -> int`

Opens one paper-trade per all-pass candidate using `log_signal_and_
open_trade` from Phase 11. Entry price = the candidate's `close` (which
IS D-1's close because pre_open reads `read_ohlcv(..., end=as_of)` and
the most recent bar in the parquet is the D-1 close).

```python
opened = 0
for cand in passing:
    if _already_opened_today(conn, cand.symbol, as_of):
        continue
    stop_price = cand.close - 1.5 * cand.atr_14   # spec §4.4
    target_price = cand.close * 1.20              # spec §4.4 (+20%)
    sizing_input = SizingInput(
        capital=capital, risk_pct=risk_pct,
        entry=cand.close,
        stop=stop_price,
        regime=regime,                            # NEUTRAL/RISK_ON/RISK_OFF
    )
    sizing = position_size(sizing_input)
    if sizing.qty == 0:
        continue  # caps bound to zero — skip
    signal = Signal(
        id=None,
        ts=f"{as_of.isoformat()}T08:30:00",
        symbol=cand.symbol,
        side="LONG",
        entry=cand.close,
        stop=stop_price,
        target=target_price,
        horizon_days=25,
        rules_passed_json=json.dumps([r.name for r in cand.rules if r.passed]),
        created_by="pre_open",
    )
    log_signal_and_open_trade(
        conn, signal=signal, entry_ts=signal.ts,
        entry_price=cand.close, qty=sizing.qty,
        atr_at_entry=cand.atr_14,
        predicted_return_pct=20.0,
    )
    opened += 1
return opened
```

Note: spec §4.4 says target = "+20% OR 1:2.5 R/R, whichever first". For
MVP the persisted `Signal.target` is the +20% level; the Phase 6 exit
evaluator handles the R/R-first detection at MTM time (Phase 14). This
matches Phase 7 backtest behaviour.

### 4.6 Step `_step_assemble(conn, paths, as_of, candidates, holdings) -> Path`

Thin wrapper: `assemble_context(conn=conn, paths=paths, as_of=as_of,
mode="pre_open", inputs=ContextInputs(candidates=candidates,
holdings_health=holdings))`. Returns the bundle path.

## 5. Error handling

Single-user, local — fail loud, fix the cause.

| Failure | Behaviour |
|---|---|
| yfinance down (macro fetch) | `_step_macro` returns False; warning collected; bundle proceeds with `_(no data)_` macro section. |
| News source down (RSS feed) | Phase 8 already isolates per-source failures. Counted in warnings. |
| Kite token absent or expired | `_step_portfolio` returns `[]`; warning collected; bundle proceeds with `_(no data)_` Holdings health section. |
| Scanner finds zero passing candidates | `_step_auto_open` skips the loop; `paper_trades_opened = 0`. Normal correction-day outcome. |
| `aggregate_daily` writes 0 rows | Already known limitation (alias-map narrowness, see project memory). Not a hard error. |
| Re-run for same date | All upstream operations are idempotent (UPSERTs on macro/sentiment, dedupe on news, scanner is pure). Auto-opened paper-trades on the second run would create duplicates — guard with `if list_open_paper_trades_for_symbol(conn, sym, as_of)` before each open. |

The duplicate-paper-trade guard is the one new piece of logic outside
existing modules. It belongs in `_step_auto_open` and uses a small new
helper `_already_opened_today(conn, symbol, as_of) -> bool` checking
`paper_trades JOIN signals WHERE symbol = ? AND ts_entry LIKE 'YYYY-MM-DD%'
AND ts_exit IS NULL`.

## 6. Testing

### 6.1 Unit tests for each `_step_*`

Five small `_step_*` functions, each tested in isolation against fixtures
or in-memory SQLite. ~10-15 tests total.

### 6.2 Integration test — `test_pre_open_integration.py`

One full end-to-end test:
- Seeds `tmp_path/data/parquet/` with 2-3 synthetic enriched OHLCV
  frames (constructed to make 1 of them all-pass).
- Stubs `fetch_all_news`, `snapshot_and_classify`, `get_holdings` with
  monkeypatch fixtures.
- Calls `run_pre_open(date(2026, 5, 15), skip_kite=True)`.
- Asserts:
  - `_context.md` exists with expected sections
  - `signals` table has 1 row, `paper_trades` has 1 OPEN row
  - `PreOpenResult.candidates_passing == 1`, `paper_trades_opened == 1`
  - Re-running returns `paper_trades_opened == 0` (idempotency guard).

### 6.3 No new snapshot tests

The bundle rendering is already snapshot-tested in
`test_llm_context.py`. Pre_open's new responsibility is *what* it
threads into `assemble_context`, not the rendering itself.

## 7. Sub-task breakdown (PROGRESS.md alignment)

PROGRESS.md lists 13.1–13.7. The implementation plan splits 13.1 into
several TDD-sized tasks:

- **13.1.a** Stub `pre_open.py` + `PreOpenResult` + skeleton `run_pre_open`
- **13.1.b** Wire `_step_macro` (TDD)
- **13.1.c** Wire `_step_news` (TDD)
- **13.1.d** Wire `_step_scan` (TDD)
- **13.1.e** Wire `_step_portfolio` (graceful-degradation TDD)
- **13.1.f** Wire `_step_auto_open` with idempotency guard (TDD)
- **13.1.g** Wire `_step_assemble` and orchestrator (TDD)
- **13.2** (covered by 13.1.g — bundle is the output of `_step_assemble`)
- **13.3** (covered by 13.1.f — auto-log is its own step)
- **13.4** Add `scripts/pre_open.bat` Windows launcher
- **13.5** Integration test (covers the full happy path)
- **13.6** Manual smoke run on real data + record findings
- **13.7** PROGRESS.md update + final commit + push

## 8. Out of scope

- Phase 14 jobs (mid_day, post_close, pre_open_iep) — separate phase.
- Phase 16 ranker — `Signal.ml_score` stays None.
- GTT viability projection — already in `portfolio.gtt`, but adding it
  to the bundle widens the design; defer to Phase 12.6 / 13.x follow-up.
- Yfinance fundamentals for `_step_portfolio` — Phase 10 handles all-
  Optional gracefully; we just don't populate them today.
- The Phase 13-prep items from the data-quality memory (alias-map
  widening, NSE event dedupe to event_calendar) — they don't block the
  MVP launch; they affect *brief quality*, not correctness.

## 9. Acceptance

After implementation:
- `trading pre-open --date 2026-05-15` runs end-to-end in <1 minute
  (with FinBERT model already cached).
- `data/research/2026-05-15/_context.md` is written with all 5 expected
  sections (macro, candidates, holdings, open trades, no matured
  predictions in pre_open mode).
- Real-data smoke shows ≥0 paper-trades opened (correction days have
  zero; rally days surface a few).
- Suite stays green; ruff + mypy clean.
- PROGRESS.md updated; commit pushed to origin/main.
