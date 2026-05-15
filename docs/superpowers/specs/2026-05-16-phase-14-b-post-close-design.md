# Phase 14.B — post_close MVP Design

**Date:** 2026-05-16
**Status:** Approved
**Predecessors:**
- [Phase 11 — paper-trade ledger + mtm + reconcile](2026-05-11-trading-system-design.md) (`reconcile_day` reused unchanged)
- [Phase 14.A — mid_day MVP](2026-05-16-phase-14-a-mid-day-design.md) (the `/kite-quotes-snapshot` skill, `gather_quote_symbols`, `_quotes_to_bars`, `read_latest_quotes` are all reused)

## 1. Context & motivation

Phase 14.B is the third major scheduled job (after Phase 13 pre_open and
Phase 14.A mid_day). Per spec §10 it runs at 16:00 IST and does:
final close MTM, daily P&L snapshot, matured-prediction evaluation,
holdings reconciliation, sector/oi updates, LLM recap, and a
post_close.md write.

The MVP scope is intentionally narrow and rides on top of existing
infrastructure:

- **Quotes:** the same `/kite-quotes-snapshot` skill from 14.A — Kite
  returns end-of-day OHLC after market close, so no new MCP tool or
  skill is needed. The skill is invoked again at 16:00.
- **MTM:** `paper.mtm.mtm_open_trades` already handles `EXIT_TIME`
  closures and final stop ratchets. Reused unchanged.
- **Reconcile:** `paper.reconcile.reconcile_day` already evaluates
  matured predictions and writes `portfolio_snapshots`. Reused
  unchanged.
- **Brief assembly:** `compile_brief` already accepts `mode="post_close"`
  and stitches in `post_close_recap.md` (the `/analyst` skill's prose
  output). We add an opportunistic include for a new
  `post_close_summary.md` (numbers).

What's actually new for 14.B: a thin orchestrator (`jobs/post_close.py`)
+ a numbers-only markdown renderer + the `trading post-close` CLI + a
`.bat` launcher.

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  trading post-close --date YYYY-MM-DD          (mode: prepare)     │
│   • gather_quote_symbols (reused from mid_day): paper-trades       │
│     ∪ today's signals ∪ holdings.json                              │
│   • write data/raw/<as_of>/_quote_symbols.txt                      │
│     (overwrites mid_day's file with the definitive end-of-day set) │
│   • print "now run /kite-quotes-snapshot, then re-run with --apply"│
└──────────────────────────────┬─────────────────────────────────────┘
                               │ (file handshake — same skill as 14.A)
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  /kite-quotes-snapshot skill (UNCHANGED — already shipped 14.A)    │
│   writes quotes_HHMM.json with the closing OHLC + last_price       │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  trading post-close --date YYYY-MM-DD --apply                      │
│   • read_latest_quotes (reused) → dict[symbol, Quote]              │
│   • _quotes_to_bars (reused) → dict[symbol, Bar]                   │
│   • paper.mtm.mtm_open_trades — final stop ratchet, closes any     │
│     TIME-stopped trades hitting day 25 today                       │
│   • paper.reconcile.reconcile_day → matured predictions update     │
│     + portfolio_snapshots row written for as_of                    │
│   • _render_post_close_summary → write data/research/<as_of>/      │
│     post_close_summary.md (pure numbers: trades closed, P&L,       │
│     prediction-error table, equity, drawdown)                      │
└────────────────────────────────────────────────────────────────────┘

(Optional, separate flow:)
  /analyst skill writes post_close_recap.md (prose), then
  trading brief compile --date X assembles brief.md including
  macro → sector → candidates → mid_day_update → post_close_summary
  → post_close_recap.
```

## 3. Components

### 3.1 New: `src/trading/jobs/post_close.py`

```python
class PostCloseAborted(RuntimeError):
    """Raised when run_post_close cannot proceed (analogue of MidDayAborted)."""


@dataclass(frozen=True)
class PostCloseResult:
    as_of: date
    quotes_capture_ts: datetime | None
    bars_built: int
    trades_evaluated: int
    trades_closed: int
    trades_held: int
    trades_skipped: int
    predictions_matured: int
    equity: float | None
    drawdown_pct: float | None
    summary_path: Path | None
    symbols_path: Path | None
    warnings: list[str]


def run_post_close(
    as_of: date,
    *,
    paths: Paths | None = None,
    apply: bool = False,
    cash: float = 100_000.0,
) -> PostCloseResult: ...
```

Plus private `_render_post_close_summary(capture_ts, mtm_results,
reconcile_result) -> str` and `_main` typer entry.

Imports reused from sibling jobs:

```python
from trading.jobs.mid_day import _quotes_to_bars, gather_quote_symbols
```

If a third caller needs these helpers later, they can move to a small
shared module. Today, importing from `mid_day` avoids premature
abstraction.

### 3.2 Modify: `src/trading/jobs/__init__.py`

Add `PostCloseAborted`, `PostCloseResult`, `run_post_close` to the
re-exports.

### 3.3 Modify: `src/trading/cli.py`

```
trading post-close --date YYYY-MM-DD [--apply] [--cash 100000]
```

Mirrors `mid-day` shape. Try/except `PostCloseAborted` → exit 2 with
remediation. Apply branch prints a Rich summary table:

| step | count |
| ---- | ----: |
| quotes_captured_at | … |
| bars_built | N |
| trades_evaluated | N |
| trades_closed | N |
| trades_held | N |
| trades_skipped | N |
| predictions_matured | N |
| equity | ₹… |
| drawdown_pct | … |

### 3.4 Modify: `src/trading/llm/briefing.py`

Add an opportunistic include for `post_close_summary.md` after the
existing `mid_day_update.md` include. Both are optional regardless of
mode (additive). The required `post_close_recap.md` for `mode="post_close"`
stays as-is.

Compiled order in `mode="post_close"`: header → Macro → Sector
commentary → Candidates → mid_day_update (if present) →
post_close_summary (if present) → post_close_recap (required).

### 3.5 New: `scripts/post_close.bat`

Two-step launcher matching `mid_day.bat`:

```bat
@echo off
REM Phase 14.B two-step launcher.
REM Usage: post_close.bat YYYY-MM-DD prepare
REM        post_close.bat YYYY-MM-DD apply
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: post_close.bat YYYY-MM-DD {prepare^|apply} & exit /b 2)
if "%~2"=="apply" (
  uv run python -m trading.jobs.post_close %1 --apply
) else (
  uv run python -m trading.jobs.post_close %1
)
```

### 3.6 Reused unchanged

- `paper.mtm.mtm_open_trades`
- `paper.reconcile.reconcile_day` (+ `evaluate_matured_predictions`,
  `compute_portfolio_snapshot`, `upsert_portfolio_snapshot`)
- `paper.ledger.open_trades`
- `data.quotes_snapshot.read_latest_quotes`
- `.claude/skills/kite-quotes-snapshot/SKILL.md` (no changes)

## 4. `post_close_summary.md` shape

```markdown
## Post-close summary — captured 2026-05-16T16:01:23

### Final MTM (3 open trades evaluated)

| symbol | action | exit price | reason | new stop |
|---|---|---|---|---|
| RVNL | EXIT_TIME | 283.00 | 25 trading days elapsed | — |
| NTPC | HOLD | — | trail moved | 387.50 |
| COALINDIA | HOLD | — | no movement | 458.40 |

### Portfolio snapshot

- equity: ₹527,341
- cash: ₹100,000
- drawdown from peak: -1.2%
- open positions: 2

### Matured predictions (2)

| symbol | predicted % | actual % | error % | horizon (d) |
|---|---|---|---|---|
| RVNL | +20.00 | -7.21 | -27.21 | 25 |
| TATAPOWER | +20.00 | +5.50 | -14.50 | 25 |

1 closed (TIME); 2 held; 0 skipped (no quote).
```

If there are no open trades, the "Final MTM" table is empty + the
summary line says "0 open trades evaluated". If there are no matured
predictions, the section reads "_(none today)_". The renderer never
fabricates rows.

## 5. Error handling

| Failure | Behaviour |
|---|---|
| Quotes snapshot missing for `as_of` | `QuoteSnapshotMissingError` → `PostCloseAborted` → CLI exit 2 + "Run /kite-quotes-snapshot first". |
| Newest quotes > 30 min old | `QuoteSnapshotStaleError` → exit 2 + remediation. |
| MCP unavailable | Same as 14.A — skill prompts for `mcp__kite__login` and halts; Python halts on the missing quotes file. |
| `reconcile_day` raises (e.g. portfolio_snapshots PK conflict on re-run) | The existing `upsert_portfolio_snapshot` already does an UPSERT, so re-running for the same `as_of` overwrites cleanly. No special guard needed. |
| Re-run apply with already-closed time-stopped trades | `mtm_open_trades` only iterates open trades — already-closed ones skip naturally. Idempotent. |
| `cash` value | For MVP, accept `--cash 100000` flag with default. Real cash tracking requires the auto-open paper-trades logic to subtract entries — out of scope; today the flag is a placeholder so `compute_portfolio_snapshot` has a number to work with. |
| No open paper-trades AND no matured predictions | `bars_built` may still be > 0 (we built bars from holdings), `trades_evaluated == 0`, `predictions_matured == 0`. Summary markdown still written; brief reads "Final MTM (0 open trades evaluated)". This is the normal state on a quiet day. |

## 6. Testing

| File | Coverage | Approx count |
|------|----------|--------------|
| `tests/test_jobs_post_close.py` (new) | `run_post_close(prepare)` writes `_quote_symbols.txt`; `run_post_close(apply)` reads quotes → MTM closes a TIME-stopped trade → `reconcile_day` writes portfolio_snapshot row → markdown rendered with all sections; `PostCloseAborted` raised on missing quotes; idempotent re-run. | 6 |
| `tests/test_cli.py` (extend) | `trading post-close --date X` (prepare) writes symbols + prints next-step; `trading post-close --date X --apply` happy path; aborts with exit 2 on missing quotes. | 3 |
| `tests/test_llm_briefing.py` (extend) | `compile_brief` includes `post_close_summary.md` after `mid_day_update.md` when both present (regardless of mode). | 1 |

Total: ~10 new tests. Existing tests stay green — only `briefing.py`
adds a new opportunistic-include line.

## 7. Sub-task breakdown (PROGRESS.md)

```
## Phase 14.B — post_close MVP

- [ ] 14.B.1 src/trading/jobs/post_close.py: PostCloseAborted +
       PostCloseResult + run_post_close (prepare/apply) +
       _render_post_close_summary; reuses gather_quote_symbols +
       _quotes_to_bars from mid_day; calls reconcile_day. Tests.
- [ ] 14.B.2 src/trading/cli.py: trading post-close --date YYYY-MM-DD
       [--apply] [--cash N] subcommand + tests
- [ ] 14.B.3 src/trading/llm/briefing.py: opportunistically include
       post_close_summary.md after mid_day_update.md (additive across
       modes) + test
- [ ] 14.B.4 scripts/post_close.bat Windows launcher
- [ ] 14.B.5 Real-data smoke (prepare → /kite-quotes-snapshot → apply)
       + PROGRESS.md + commit + push
```

5 sub-tasks. No new skill, no new MCP tool, no new dataclass shapes.

## 8. Out of scope

- **Holdings reconciliation vs yesterday** (divs/splits/GTT triggers).
  Substantial; defer to a 14.B.x follow-up or a separate Phase.
- **Sector_daily updates** — blocked on Phase 12.6.
- **OI_daily updates** — F&O work; no current scope.
- **LLM-generated `post_close_recap.md` prose** — already routed through
  Phase 12 `/analyst` skill (`mode="post_close"`); no new code here.
- **Real cash tracking** — `--cash` is a placeholder; live cash
  arithmetic requires wiring entry-side debits into auto-open. Separate
  concern.
- **Kill-switch state propagation** to next pre_open — same deferral as
  in 14.A out-of-scope.
- **Phase 14.C (pre_open_iep)** — separate spec next.
