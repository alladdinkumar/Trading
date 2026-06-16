# Architecture Review — Findings Log

> Running log of **vulnerabilities, gaps, inaccuracies, missing tests/fixtures,
> and tech debt** discovered while writing the [`docs/architecture/`](./PROGRESS.md)
> design set. The layer docs describe *what the code does today*; anything that
> *should change* lands here instead, so it can be triaged and fixed in a
> dedicated pass — after which we revisit and update the docs.

## How to use this file

- Each finding gets a stable ID (`F-NNN`) so docs and commits can reference it.
- Add findings as they're discovered, per phase. Don't fix inline during doc
  writing — capture here, keep moving.
- When fixed: set **Status → Fixed**, note the commit, and update any doc the
  fix invalidates.

### Categories

| Tag | Meaning |
|---|---|
| `VULN` | Security / correctness vulnerability or data-integrity risk |
| `GAP` | Missing functionality, validation, or guardrail |
| `INACC` | Code disagrees with a spec, comment, docstring, or manifest |
| `TEST` | Missing test coverage or a fixture needed to test safely |
| `DEBT` | Cleanup / maintainability / clarity |

### Severity

`High` (can cause wrong trades, data loss, or silent failure) ·
`Med` (degrades robustness or misleads) · `Low` (cosmetic / nice-to-have)

### Status

`Open` · `In progress` · `Fixed` · `Wontfix` (with reason) · `Needs decision`

---

## Findings

| ID | Cat | Sev | Phase | Title | Status |
|---|---|---|---|---|---|
| F-001 | INACC | Med | 0 | `vectorbt` + `anthropic` declared deps but unused in production paths | Open |
| F-002 | GAP | High | 0 | No schema validation on broker/quote JSON contract between `/kite-*` skills and `data/*_snapshot.py` | Open |
| F-003 | GAP | Med | 0 | Daily flow is ~13 manually-sequenced commands with no half-run/missed-step detection | Open |
| F-004 | DEBT | Low | 0 | Each job re-derives "today" in IST independently; no single canonical clock | Open |
| F-005 | GAP | High | 0 | No real-money execution path, kill-switch, or risk-halt design (gated to future Phase 19, tracked here so it isn't forgotten) | Needs decision |
| F-006 | DEBT | Med | 1 | Domain DTOs live in `data/` so `store` depends "up" into the ingestion layer | Open |
| F-007 | DEBT | Med | 1 | `data.macro.snapshot_and_classify` puts a decision concern (regime classify) in the data layer (upward back-edge into `features`) | Open |
| F-008 | DEBT | Med | 1 | `strategy ⇄ backtest` package-level import cycle, broken only by lazy/TYPE_CHECKING imports | Open |
| F-009 | GAP | Low | 1 | No automated dependency-layering enforcement (e.g. import-linter); layering is convention-only | Open |

---

## Detail

### F-001 — Unused declared dependencies (`INACC`, Med, Phase 0)
`pyproject.toml` declares `vectorbt>=0.26` and `anthropic>=0.40`, but the
backtester is a hand-rolled event loop (Phase 7 deviation) and the LLM runs via
Claude Code skills (Phase 12 deviation, no API credits). A reader could mistake
the manifest for the architecture.
- **Fix idea:** Remove both, or move to an `optional`/commented block with a
  one-line note pointing at the deviation specs. Confirm nothing imports them
  (`grep -r "import vectorbt\|import anthropic"`).
- **Doc to revisit after fix:** `00-overview.md` §3 tech-stack note.

### F-002 — No validation of the broker JSON contract (`GAP`, High, Phase 0)
`/kite-snapshot` and `/kite-quotes-snapshot` write JSON that `data/kite_snapshot.py`
and `data/quotes_snapshot.py` read back into dataclasses. The contract is
enforced only by the skill obeying its `SKILL.md`. A malformed write (wrong
`exchange`, missing required field, wrong types) may pass partially and corrupt
downstream logic (e.g. a wrong-exchange quote driving an MTM exit).
- **Fix idea:** Add a pydantic/dataclass validator at the read boundary that
  fails loudly with a clear remediation message; add `TEST` fixtures of malformed
  payloads.
- **Depends on:** F-006-range fixtures (to be added when Phase 3 is written).

### F-003 — No half-run detection in the daily flow (`GAP`, Med, Phase 0)
The operator runs the blocks manually; skipping IEP (or running blocks out of
order) silently leaves the bundle un-reranked or stale. Nothing reconciles
"which steps ran today".
- **Fix idea:** A lightweight per-date run-state file (or a `run_log` table)
  each step stamps, plus a `trading status --date` that reports gaps.

### F-004 — No canonical clock (`DEBT`, Low, Phase 0)
"Today in IST" is re-derived in several places (`ops/runner._today_ist`, jobs,
skills). Centralising would reduce drift and make freezegun-based tests simpler.

### F-005 — No execution / kill-switch design (`GAP`, High → Needs decision, Phase 0)
The system is paper-only. The Phase 18.5 gate (≥3 months OOS Sharpe > 1.0)
leads to a real-money Phase 19 that currently has no spec, no order-placement
path, and no risk-halt/kill-switch. Logged so the hardening review explicitly
decides scope and sequencing.

---

### F-006 — Domain types in `data/` couple `store` to ingestion (`DEBT`, Med, Phase 1)
`store/{ohlcv,macro_store,news_store,sector_store}.py` import their row types
from `data/{yfinance,macro,news,sector}.py`. Persistence depends on the fetcher
for "what the row is."
- **Fix idea:** Introduce `trading/domain.py` (or `types/`) holding the frozen
  DTOs; have both `data` and `store` import from there.
- **Doc to revisit:** `01-architecture.md` §3.1 + layer table.

### F-007 — Regime classification lives in the data layer (`DEBT`, Med, Phase 1)
`data.macro.snapshot_and_classify` lazily imports `features.regime` — the only
upward (ingestion→analysis) edge.
- **Fix idea:** Split into a pure `fetch` (in `data`) + `classify` (in
  `features` or the `pre_open` job). Keep `data` fetch-only.
- **Doc to revisit:** `01-architecture.md` §3.2; `03-data-layer.md` (Phase 3).

### F-008 — `strategy ⇄ backtest` cycle (`DEBT`, Med, Phase 1)
Mutual package dependency: `backtest.engine` runs `strategy.*`; `strategy.ranker*`
reuse `backtest.*` for labels/OOS scoring. Only lazy imports prevent an
import-time cycle.
- **Fix idea:** Extract the shared exit-replay/labeling logic into a small
  neutral module both depend on; or formally accept the cycle and document the
  import rules (never import across the seam at module top).
- **Doc to revisit:** `01-architecture.md` §3.3; Phase 4/5 docs.

### F-009 — No layering enforcement (`GAP`, Low, Phase 1)
The downward-dependency rule is convention, unenforced.
- **Fix idea:** Add `import-linter` contracts to CI mirroring the L0–L6 layers.

---

_Counts: 9 open · 0 fixed. Updated through Phase 1._
