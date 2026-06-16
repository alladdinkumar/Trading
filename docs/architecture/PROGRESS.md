# Architecture Documentation — Progress

> **Goal:** A consolidated, in-depth, reviewer-friendly design document for the
> **current** (as-built) trading system — architecture, schema, data flow, and
> the logic of every layer — so it can be reviewed and hardened.
>
> **Why this exists:** The build was specced phase-by-phase under
> `docs/superpowers/specs/` (14 separate documents). Those describe *intent at
> the time*; they drift from the code. This set documents *what the code
> actually does today*, read straight from `src/trading/`.

Each layer doc ends with a **⚠️ Robustness notes / open questions** section
that flags fragility, assumptions, and hardening candidates for review. Concrete
actionable issues (vulnerabilities, gaps, inaccuracies, missing tests/fixtures,
tech debt) are logged in [`FINDINGS.md`](./FINDINGS.md) for a dedicated fix pass
— **don't fix inline during doc writing; capture and keep moving**.

## Legend

- `[ ]` — pending
- `[~]` — in progress
- `[x]` — done

## Status snapshot

| Phase | Document | State |
|---|---|---|
| 0 | `00-overview.md` (+ scaffold) | `[x]` |
| 1 | `01-architecture.md` | `[x]` |
| 2 | `02-data-schema.md` | `[x]` |
| 3 | `03-data-layer.md` | `[x]` |
| 4 | `04-analysis-strategy.md` | `[x]` |
| 5 | `05-backtest-portfolio-paper.md` | `[x]` |
| 6 | `06-llm-and-skills.md` | `[ ]` |
| 7 | `07-jobs-workflows.md` | `[ ]` |
| 8 | `08-ops-cli-ui.md` | `[ ]` |

**Currently working on:** _Phase 5 complete — backtest/portfolio/paper shipped (F-021..F-025)._
**Next up:** _Phase 6 — `06-llm-and-skills.md` (llm/ context+brief, Claude Code skills)._

---

## Method (applies to every phase)

1. Read the actual source modules for the layer (not just the old specs).
2. Write the doc: prose + tables + Mermaid diagrams, reviewer-friendly.
3. Close with **⚠️ Robustness notes / open questions**.
4. Append concrete actionable items to [`FINDINGS.md`](./FINDINGS.md) (`F-NNN`).
5. Update this tracker; commit `docs(arch): <phase> …`.

After all phases: triage `FINDINGS.md`, fix in a dedicated pass, then revisit
and update any docs the fixes invalidate.

## Phase checklist

- [x] **Phase 0 — Overview + scaffold**
  - [x] Create `docs/architecture/` + this tracker
  - [x] `00-overview.md`: purpose, daily lifecycle, tech stack, repo map, CLI surface, design principles
- [x] **Phase 1 — Architecture**
  - [x] Layered architecture + module-dependency graph (Mermaid)
  - [x] Cross-cutting patterns: graceful degradation, idempotency, pure functions, `SignalProvider`
  - [x] Notable couplings (F-006..F-009)
- [x] **Phase 2 — Data schema**
  - [x] SQLite ER diagram + all 16 tables (8 active / 8 dormant), field-level notes
  - [x] Parquet layout, JSON snapshot contracts, `models/registry.csv`, `data/` map
  - [x] Findings F-010..F-013 (incl. high-sev F-011 empty-table gates)
- [x] **Phase 3 — Data layer (`data/`)**
  - [x] Per-module deep dives (yfinance, cache, kite, kite_snapshot, quotes_snapshot, macro, news, sector, universe)
  - [x] Source→module→sink Mermaid; MCP-vs-SDK split; coverage reality (12 symbols)
  - [x] Findings F-014..F-018 (incl. high-sev F-014 universe coverage, F-018 freshness)
- [x] **Phase 4 — Analysis + strategy (`features/`, `strategy/`)**
  - [x] features: technicals suite, FinBERT sentiment + critical veto, 4-axis regime voter
  - [x] strategy: 10 Layer-A rules, sizing formula, exit state machine, Layer-B ranker (features/labels/train/registry)
  - [x] **Resolved F-011 → F-019** (4 rules are no-ops: empty ScanContext); + F-020 regime name collision
- [x] **Phase 5 — Backtest + portfolio + paper (`backtest/`, `portfolio/`, `paper/`)**
  - [x] backtest: cost model, event-loop engine (+F-021 cost bug), walk-forward, metrics
  - [x] portfolio: health (+F-022 TRIM bias), GTT Monte-Carlo, SIP allocator
  - [x] paper: ledger, MTM (+F-024 days_held), reconcile (+F-023 equity not compounded), +F-025 cost asymmetry
- [ ] **Phase 6 — LLM layer + Claude Code skills (`llm/`, `.claude/skills/`)**
- [ ] **Phase 7 — Jobs + workflows (`jobs/`)**
- [ ] **Phase 8 — Ops + CLI + UI (`ops/`, `cli.py`, `ui/`)**
