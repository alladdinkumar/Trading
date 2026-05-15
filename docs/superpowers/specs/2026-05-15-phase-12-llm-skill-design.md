# Phase 12 — LLM Analyst (Claude Code skill version)

**Date:** 2026-05-15
**Status:** Approved (supersedes the Anthropic-SDK plan in
[`2026-05-11-trading-system-design.md`](2026-05-11-trading-system-design.md) §4.3 / §11 /
PROGRESS.md Phase 12 sub-tasks 12.1, 12.2, 12.5)

## 1. Context & motivation

The locked design (spec §4.3) calls for a Claude Sonnet/Haiku narrative layer driven by
the `anthropic` SDK with prompt caching, retries, and per-token cost tracking. Cost
target was ₹5–15/day.

The user has a Claude Pro subscription that already covers Claude Code usage at the
terminal but does **not** include Anthropic API credits. Building the SDK wrapper would
mean paying twice — once for Pro, again for API tokens — for the same model
intelligence.

This document records the deviation: Phase 12 becomes a **Claude Code skill**
(`/analyst`) that the user invokes interactively. Claude (the assistant in the terminal)
plays the analyst role directly — no SDK, no API spend. Surrounding Python code is
limited to deterministic file I/O: assembling the input bundle and compiling the final
brief from narrative parts.

The trade-off is that Phase 13's `pre_open` job can no longer run fully unattended;
it splits into two phases (assemble-context → analyst → compile-brief) with a file
handshake between them. This is acceptable because the daily run is already manually
supervised in v1, per spec §10.

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 13 pre_open --assemble-context (deterministic Python)        │
│   • runs scanner / portfolio health / macro snapshot / paper MTM    │
│   • writes data/research/YYYY-MM-DD/_context.md (input bundle)      │
│   • prints "now run /analyst skill in Claude Code"                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ (file handshake)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  /analyst skill (Claude Code, user-in-the-loop)                     │
│   • reads _context.md + cited DB rows / parquet bars on demand      │
│   • writes:  macro_brief.md                                          │
│              candidates/{SYMBOL}.md  (one per surfaced candidate)    │
│              sector_commentary.md                                    │
│              post_close_recap.md  (only when context says EOD run)   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ (file handshake)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 13 pre_open --compile-brief (or post_close --compile-brief)  │
│   • briefing.compile_brief() concatenates parts → brief.md          │
│   • logs paper-trades, snapshots portfolio                          │
└─────────────────────────────────────────────────────────────────────┘
```

The handshake is a directory contract: each side reads/writes specific filenames in
`data/research/YYYY-MM-DD/`. Either side can be re-run independently for the same
date.

## 3. Components

### 3.1 Python — `src/trading/llm/`

| Module        | Public surface                                            | Responsibility |
|---------------|-----------------------------------------------------------|----------------|
| `context.py`  | `ContextInputs` dataclass · `assemble_context(conn, paths, as_of, mode, inputs) -> Path` | Render the `_context.md` markdown bundle into `data/research/YYYY-MM-DD/`. Pulls genuinely-persisted state from SQLite (`macro_snapshot`, `sentiment_daily`, `paper_trades`, `predictions`) directly via `conn`. Takes ephemeral upstream outputs (`candidates`, `holdings_health`) via the `ContextInputs` dataclass — these are computed once per pre_open run and don't merit their own DB table. `mode ∈ {"pre_open", "post_close"}` controls which sections are included. Returns the written path. |
| `briefing.py` | `compile_brief(date_dir, mode) -> Path` · `expected_parts(mode, candidate_symbols) -> list[str]` · `MissingNarrativeError` | Concatenate analyst-produced parts into `brief.md` in a fixed order. Raises `MissingNarrativeError` listing absent expected files. Ignores orphan candidate files (with stderr warning). |
| `__init__.py` | re-exports the public surface                              | — |

Both modules are pure file I/O over typed dataclasses. Neither depends on `anthropic`,
network, or the Claude Code skill — they are independently unit-testable.

The `ContextInputs` dataclass keeps `assemble_context` decoupled from how upstream
phases choose to compute their outputs: Phase 13's `pre_open.py` will run
`scan()` and `portfolio.health` once, build `ContextInputs`, and pass it in. Tests
seed `ContextInputs` directly without needing to mock the scanner or analyzer.

### 3.2 Skill — `.claude/skills/analyst/`

Project-level skill, checked into the repo so it is reproducible across machines and
versioned with PROGRESS.md.

| File                                | Purpose |
|-------------------------------------|---------|
| `SKILL.md`                          | Frontmatter (`name: analyst`, `description: …`) + step-by-step instructions: (1) read `_context.md`, (2) verify timestamp ≤ 12 h old, (3) write the four output files following templates, (4) keep tone evidence-first / cite numbers from the context. |
| `references/output-templates.md`    | Exact markdown skeletons for `macro_brief.md`, `candidates/{SYMBOL}.md`, `sector_commentary.md`, `post_close_recap.md`. The skill instructs me to follow the skeletons so `compile_brief` can rely on stable headings. |

The skill is the only place where prose-generation guidance lives. Python code never
constructs prompts; it only reads/writes file artifacts.

### 3.3 CLI — additions to `src/trading/cli.py`

```
trading brief assemble-context --date YYYY-MM-DD --mode {pre_open,post_close}
trading brief compile          --date YYYY-MM-DD
```

Both commands are thin wrappers over the `llm` module. `assemble-context` prints
"now run /analyst skill in Claude Code" on success. `compile` prints the path to the
final `brief.md`. Phase 13's `pre_open.py` job will call them as the first and last
steps of the daily run.

## 4. Data flow — file contents

### 4.1 `_context.md` (input bundle, written by `context.py`)

Sections, in order, with their input source:

| # | Section | Source |
|---|---------|--------|
| 1 | Header (date, mode, assembly timestamp) | computed in `context.py` |
| 2 | Macro snapshot (VIX, USDINR Δ, FII flow, futures mean, regime label/score, reasoning) | DB: `macro_snapshot` row for `as_of` |
| 3 | Today's candidates — top N (N ≤ 5), each with rule pass count, entry/stop/target/qty/RR, ATR/RSI, sector + relative strength, recent news headlines (last 7d, dated, scored, categorised), critical-news flag | `ContextInputs.candidates` (caller passes scanner output, sorted by rule pass count desc, then by symbol); news headlines pulled from DB `news_items` + `sentiment_daily` per symbol |
| 4 | Holdings health — top movers (verdict, score, key technical drivers, GTT P(hit)) | `ContextInputs.holdings_health` (caller passes portfolio analyzer output) |
| 5 | Open paper-trades (symbol, entry, days_held, current pnl%, trailing stop) | DB: `paper_trades WHERE status='OPEN'` |
| 6 | Matured predictions *(post_close mode only)* — predicted vs actual table | DB: `predictions` rows whose horizon matured for `as_of` |

Empty sections are written as `_(no data)_` rather than omitted, so the skill can
flag the gap explicitly in the output.

### 4.2 `macro_brief.md` (analyst output)

One paragraph, ≤ 120 words. Opens with the regime call (RISK_ON / NEUTRAL /
RISK_OFF). Cites VIX, FII flow, USDINR move from the bundle. No prose if the macro
section was `_(no data)_` — instead a one-line "Macro: not classified today —
review needed".

### 4.3 `candidates/{SYMBOL}.md` (analyst output, one per surfaced candidate)

Fixed skeleton:

```markdown
# {SYMBOL} — Conviction: {HIGH|MEDIUM|LOW}

## Bullish case
{3–4 sentences citing rule pass count, sector strength, sentiment score}

## Bearish case / risks
{3–4 sentences citing failed rules, drawdowns, negative news}

## Event risks in 25-day horizon
- {YYYY-MM-DD}: {event} — {impact note}
```

`compile_brief` only consumes files where the bare filename (sans `.md`) matches a
symbol listed in the bundle's candidates section; other files are skipped with a
warning.

### 4.4 `sector_commentary.md` (analyst output)

One section per active sector. Each section: 3 lines — LEADING/LAGGING flag, 5d
relative strength, one-line driver.

### 4.5 `post_close_recap.md` (analyst output, post_close mode only)

Three sub-sections: paragraph on the day's market, prediction-error commentary on the
matured-predictions table, kill-switch notes (which fired, which were close).

### 4.6 `brief.md` (compiled output, written by `compile_brief`)

Concatenation order:

1. Header (date, mode, "compiled at" timestamp).
2. `macro_brief.md`.
3. `sector_commentary.md`.
4. `candidates/*.md` in the order symbols appear in the bundle.
5. `post_close_recap.md` (post_close mode only).

## 5. Error handling

Single-user, local — fail loud, fix the cause.

| Failure mode | Behaviour |
|--------------|-----------|
| Missing input row in `assemble_context` (e.g. no macro snapshot for date) | Section written as `_(no data)_`. Bundle still produced. Skill instructs me to flag the gap in the output. |
| Skill not run / narrative parts absent when `compile_brief` runs | `MissingNarrativeError` lists every absent expected file. Caller decides whether to abort or proceed with `--allow-partial`. |
| `_context.md` more than 12 h old at skill runtime | Skill instructions tell me to refuse to write outputs and ask the user to re-assemble. |
| Orphan candidate file (symbol not in bundle) | Skipped by `compile_brief`, warning printed to stderr. |
| Skill output is wrong shape | Not validated programmatically. We rely on the templates + my discipline. A markdown schema validator would be over-engineering for v1. |

## 6. Testing

All tests are deterministic and offline. The skill prompt is not unit-tested — only
file I/O is.

| Test file | Coverage | Approx count |
|-----------|----------|--------------|
| `tests/test_llm_context.py` | `assemble_context` against in-memory SQLite seeded with `macro_snapshot` / `sentiment_daily` / `news_items` / `paper_trades` / `predictions` rows + a hand-built `ContextInputs` (candidates, holdings_health). Syrupy snapshot on rendered `_context.md` for both `pre_open` and `post_close` modes. Empty-table → `_(no data)_` markers. | 5 |
| `tests/test_llm_briefing.py` | `compile_brief` against a synthetic `data/research/2026-05-15/` tree with hand-written narrative parts. Syrupy snapshot on `brief.md`. `MissingNarrativeError` tests for each required part missing. Orphan-file warning test. | 7 |
| `tests/test_cli.py` (additions) | Happy-path invocations of `trading brief assemble-context` and `trading brief compile`. | 2 |

Total new tests: ~14. `syrupy` is already in the dev dependency group (pyproject.toml).

## 7. PROGRESS.md sub-task rewrites

```
## Phase 12 — LLM analyst (Claude Code skill version)

- [ ] 12.1 src/trading/llm/context.py: assemble_context(...) writes _context.md
       bundle from SQLite + parquet for pre_open / post_close modes
- [ ] 12.2 .claude/skills/analyst/ (SKILL.md + references/output-templates.md):
       project-level skill that reads _context.md, writes the four narrative parts
- [ ] 12.3 src/trading/llm/briefing.py: compile_brief(date_dir) + MissingNarrativeError
       + expected_parts(mode); concatenates parts into brief.md
- [ ] 12.4 Tests: ~14 across test_llm_context.py (syrupy snapshot bundles),
       test_llm_briefing.py (compile + missing-parts), test_cli.py (CLI happy path)
- [ ] 12.5 N/A — cost tracking dropped (Claude Pro plan, no per-call cost)
- [ ] 12.6 Update PROGRESS.md → commit feat(llm): analyst skill + briefing pipeline (Phase 12)
       and push to origin/main
```

12.5 is intentionally retained as a numbered slot marked N/A so future readers see
why the original sub-task is gone.

## 8. Out of scope / future

- Migration back to the Anthropic SDK if/when API credits become available — the
  `_context.md` → narrative-parts → `brief.md` contract would not change. A new
  `llm/client.py` could replace the manual skill step by feeding `_context.md`
  through `messages.create` and writing the same output files.
- Batch / multi-day re-generation. v1 expects one date per invocation.
- A markdown schema validator for narrative parts.
- Phase 13's `pre_open.py` orchestrator itself — that is Phase 13 work; this design
  only commits to the CLI contract it will call.
