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
that flags fragility, assumptions, and hardening candidates for review.

## Legend

- `[ ]` — pending
- `[~]` — in progress
- `[x]` — done

## Status snapshot

| Phase | Document | State |
|---|---|---|
| 0 | `00-overview.md` (+ scaffold) | `[x]` |
| 1 | `01-architecture.md` | `[ ]` |
| 2 | `02-data-schema.md` | `[ ]` |
| 3 | `03-data-layer.md` | `[ ]` |
| 4 | `04-analysis-strategy.md` | `[ ]` |
| 5 | `05-backtest-portfolio-paper.md` | `[ ]` |
| 6 | `06-llm-and-skills.md` | `[ ]` |
| 7 | `07-jobs-workflows.md` | `[ ]` |
| 8 | `08-ops-cli-ui.md` | `[ ]` |

**Currently working on:** _Phase 0 complete — overview + scaffold shipped._
**Next up:** _Phase 1 — `01-architecture.md` (module dependency graph + cross-cutting patterns)._

---

## Method (applies to every phase)

1. Read the actual source modules for the layer (not just the old specs).
2. Write the doc: prose + tables + Mermaid diagrams, reviewer-friendly.
3. Close with **⚠️ Robustness notes / open questions**.
4. Update this tracker; commit `docs(arch): <phase> …`.

## Phase checklist

- [x] **Phase 0 — Overview + scaffold**
  - [x] Create `docs/architecture/` + this tracker
  - [x] `00-overview.md`: purpose, daily lifecycle, tech stack, repo map, CLI surface, design principles
- [ ] **Phase 1 — Architecture**
  - [ ] Layered architecture + module-dependency graph (Mermaid)
  - [ ] Cross-cutting patterns: graceful degradation, idempotency, pure functions, `SignalProvider`
- [ ] **Phase 2 — Data schema**
  - [ ] SQLite ER diagram + all 16 tables, field-level notes
  - [ ] Parquet layout, JSON snapshot contracts, `models/registry.csv`, `data/` map
- [ ] **Phase 3 — Data layer (`data/`)**
- [ ] **Phase 4 — Analysis + strategy (`features/`, `strategy/`)**
- [ ] **Phase 5 — Backtest + portfolio + paper (`backtest/`, `portfolio/`, `paper/`)**
- [ ] **Phase 6 — LLM layer + Claude Code skills (`llm/`, `.claude/skills/`)**
- [ ] **Phase 7 — Jobs + workflows (`jobs/`)**
- [ ] **Phase 8 — Ops + CLI + UI (`ops/`, `cli.py`, `ui/`)**
