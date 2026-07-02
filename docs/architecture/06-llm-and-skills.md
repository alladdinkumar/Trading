# 06 — LLM Layer & Claude Code Skills (`llm/`, `.claude/skills/`)

> Part of the [`docs/architecture/`](./PROGRESS.md) set. Covers how the system
> uses an LLM and the broker without putting either inside the batch Python: the
> `llm/` context/brief pipeline plus the three Claude Code **skills**. Grounded
> in `src/trading/llm/*.py` and `.claude/skills/*/SKILL.md`.

## 1. The human/LLM-in-the-loop contract

The system deliberately splits work between deterministic Python and an
interactive Claude Code session. Python does all *computation*; the LLM does
*narrative and broker I/O*. They communicate through files on disk — never
in-process calls.

```mermaid
flowchart TD
    subgraph PY1["Python (deterministic)"]
        A["pre_open job"] -->|assemble_context| CTX["_context.md<br/>(machine bundle: numbers only)"]
    end
    subgraph LLM["Claude Code session (skills)"]
        SK["/analyst skill"]
    end
    subgraph PY2["Python (deterministic)"]
        CMP["brief compile"] --> BRIEF["brief.md<br/>(human-readable)"]
    end
    CTX -->|skill reads| SK
    SK -->|writes| NARR["macro_brief.md, sector_commentary.md,<br/>candidates/SYMBOL.md, post_close_recap.md"]
    NARR -->|compile reads| CMP
```

Three reasons for this design (recap of [00-overview §1](./00-overview.md)):
- **No API spend** — the user has Claude Pro, not API credits, so the LLM runs in
  the Claude Code session via skills rather than the `anthropic` SDK.
- **Secrets stay interactive** — broker (Kite) access is via MCP tools the session
  holds; Python only reads the JSON the skill writes.
- **Separation of concerns** — the LLM never does arithmetic; it reads computed
  numbers and writes prose. This bounds hallucination to *interpretation*, not
  *data*.

## 2. `llm/context.py` — the machine bundle

`assemble_context(conn, paths, as_of, mode, inputs) → _context.md`. A pure
renderer: it pulls from SQLite (`macro_snapshot`, `sector_daily`,
`sentiment_daily`, `news_items`, open `paper_trades`, matured `predictions`) and
from caller-supplied `ContextInputs` (the ephemeral `candidates`,
`holdings_health`, and optional `scored_candidates` that don't live in any
table). Sections, in order:

| Section | Source | Empty-state |
|---|---|---|
| Header | `as_of` + `mode` + assembly timestamp | — |
| `## Macro snapshot` | `macro_snapshot` row | `_(no data)_` |
| `## Sector momentum` | `sector_daily` (sorted by 20d RS) | `_(no data)_` |
| `## Today's candidates` | `inputs.candidates` + per-symbol news + sector bullet | `_(no data)_` |
| `## Layer B ranker` | `inputs.scored_candidates` (omitted if None) | section absent |
| `## Holdings health` | `inputs.holdings_health` | `_(no data)_` |
| `## Open paper-trades` | open `paper_trades` | `_(no data)_` |
| `## Matured predictions` | `predictions` (post_close only) | `_(no data)_` |

Every empty section renders the literal `_(no data)_` **on purpose** — it's a
signal to the analyst (and the reader) that a source was genuinely empty, not
that the renderer broke. The per-symbol news query caps `ts ≤ as_of` end-of-day
(Phase 12.5.2) so future-dated NSE events don't leak into "recent headlines".

`ContextInputs.candidates` are passed in by the job — note these are the
*all-candidates* list (incl. partial passes), which is why the bundle shows
"passes 9/10" rows even though none all-pass.

## 3. `llm/briefing.py` — compile to `brief.md`

`compile_brief(date_dir, mode)` concatenates the analyst's narrative files into a
single `brief.md` in a fixed section order:

1. Macro · 2. Sector commentary · 3. Candidates (one block per symbol) ·
4. (optional) mid-day update · 5. (optional) post-close summary ·
6. (post_close) post-close recap.

- **Required vs optional** — `required_parts` = `macro_brief.md` + one
  `candidates/{SYMBOL}.md` per bundle candidate (+ `post_close_recap.md` in
  post_close mode). Missing required parts raise `MissingNarrativeError` (so a
  half-run is *loud*). `sector_commentary.md` is optional — a placeholder body is
  substituted, header kept.
- **Symbol list** is parsed from the bundle with the regex
  `^### ([A-Z0-9_]+) — passes \d+/\d+ rules`. Orphan candidate files (a symbol not
  in the bundle) are skipped with a stderr warning.
- **Mode** is inferred from the bundle header (`(mode: post_close)`) if not
  passed.
- **Deterministic guardrails (F-026):** before compiling, `compile_brief` parses
  the bundle's `_Assembled at_` stamp and raises `StaleBundleError` if it is
  older than `max_age` (default 12 h) relative to `now` — both injectable; a
  missing stamp skips the check; `allow_stale=True` (CLI `--allow-stale`) is the
  override. It also parses the bundle's `## Macro snapshot` table and warns to
  stderr when a VIX/USDINR figure cited in `macro_brief.md` disagrees with the
  bundle (rounding-tolerant, warn-only). Both keep `compile_brief` a pure file
  operation — the bundle, not the DB, is the source of truth at compile time.

> **Brittle three-way coupling (F-027):** the candidate heading string is written
> by `context._render_candidates`, re-written in place by `pre_open_iep` when it
> reorders candidates, and parsed by `briefing._CANDIDATE_HEADING`. All three must
> agree on the exact `### SYM — passes N/M rules` format. A change in any one
> silently breaks symbol parsing (candidates dropped from the brief, or an
> orphan-warning storm) with no test spanning the three. → F-027.

## 4. The skills (`.claude/skills/`)

Each skill is a `SKILL.md` the Claude Code session executes. They are the only
place MCP / the LLM is invoked. Three drive the daily narrative/broker loop
(below); a fourth, `/macro-doctor` (§4.4), is the F-036 reconciliation
orchestrator; a fifth, `/daily-workflow` (§4.5), orchestrates the whole day.

### 4.1 `/kite-snapshot` — broker holdings/GTTs
Probes `mcp__kite__get_profile`; on auth failure it halts and tells the user to
run `mcp__kite__login` (no partial writes). Otherwise calls
`mcp__kite__get_holdings` / `get_gtts` (and optionally `get_positions`), maps each
row to the `data.kite` dataclass field names, and writes
`data/raw/<date>/{holdings,gtts,positions}.json` atomically plus `_meta.json`
(`source: "mcp"`). Consumed by `data.kite_snapshot` ([03 §3.4](./03-data-layer.md)).

### 4.2 `/kite-quotes-snapshot` — intraday quotes
Reads `_quote_symbols.txt` (written by `open-fills`/`mid-day`/`post-close`
prepare). Halts if absent. Calls `mcp__kite__get_quotes` for the (NSE-defaulted) symbols, writes
`quotes_HHMM.json` (HHMM = capture time, the staleness source of truth), and
merges `quotes_at` into `_meta.json`. Consumed by `data.quotes_snapshot`.

### 4.3 `/analyst` — the narrative
Reads the most recent `_context.md`, and writes `macro_brief.md`,
`sector_commentary.md` (optional), `candidates/{SYMBOL}.md` per candidate, and
(post_close) `post_close_recap.md`, following fixed skeletons so `compile_brief`'s
headings parse. Style rules: **evidence-first** (cite numbers from the bundle,
never invent), concise word caps, and a conviction (HIGH/MEDIUM/LOW) justified in
the body. **Refuse-stale:** if the bundle's `_Assembled at_` timestamp is > 12 h
old, it must refuse and tell the user to re-assemble — and this is now also
enforced in code (F-026): `compile_brief` raises `StaleBundleError` on a stale
bundle regardless of whether the model honours the SKILL rule.

### 4.4 `/macro-doctor` — macro cross-source reconciliation (F-035/F-036)
Probes `mcp__kite__get_profile`, then pulls a **read-only** second source —
`NSE:INDIA VIX` (+ a USDINR currency-future proxy) via `mcp__kite__get_quotes` —
and writes `data/raw/<date>/macro_cross_HHMM.json` (the validated `MacroCrossSource`
schema). It then hands off to the deterministic CLI: `trading macro refresh
--cross` gap-fills any still-missing figure, and `trading macro verify --cross`
cross-checks within tolerance (exit-1 on mismatch). **Orchestrator only** — every
DB write goes through the CLI, and it never calls `place_order`/`modify`. Kite has
no FII/DII feed, so those stay `unreconciled`. This is the "skill is the brain,
deterministic code is the hands" split applied to ingestion.

### 4.5 `/daily-workflow` — the day orchestrator
Walks the four IST time-gated blocks (pre-open → IEP → open-fills → mid-day →
post-close) end to end in one Claude Code session: runs each block's CLI steps,
drives the `/kite-*` skills in between, pauses for Kite login when the session
is dead, and self-schedules a wake-up for the next block. It layers *sequencing*
on top of the reminder-driven flow but is still session-bound — if the session
dies mid-day, the remaining blocks fall back to the human-run reminders.

## 5. Where the trust boundaries are

| Boundary | Enforced by | Risk |
|---|---|---|
| Broker JSON shape | `snapshot_schema` validation at the read boundary | code-enforced ✅ (F-002) — malformed write → `SnapshotSchemaError` with remediation |
| Quote freshness | `quotes_snapshot` code (30 min) | code-enforced ✅ |
| Bundle freshness (analyst) | `compile_brief` → `StaleBundleError` (>12h) + SKILL.md | code-enforced ✅ (F-026) — deterministic refuse, `--allow-stale` override |
| Narrative accuracy | `compile_brief` macro figure cross-check + "evidence-first" SKILL rule | partial ✅ (F-026) — VIX/USDINR cited in `macro_brief.md` are checked against the bundle (warn-only); other prose still LLM-trust |
| Bundle-vs-reality (macro) | `reconcile_macro` + `trading macro verify` + `_render_macro` annotation | code-enforced ✅ (F-036) — VIX/USDINR cross-checked vs Kite within tolerance, mismatches flagged in the bundle; FII/DII `unreconciled` (no Kite feed) |
| Candidate heading format | shared string across 3 modules | silent breakage on change (F-027) |

## 6. Current-state note

Because the daily flow is manual, the brief only exists for a date if a human ran
`/analyst` *and* `brief compile` after `pre-open`. `pre-open` writes the bundle
unconditionally, so the machine context is always present; the *narrative* is the
human-dependent part. A skipped `/analyst` makes `compile_brief` raise on the
missing required parts — loud, which is good, but there's no automated fallback or
"narrate-later" path. Ties to F-003 (half-run detection).

---

## ⚠️ Robustness notes / open questions

- **Narrative verification (F-026) ✅ done 2026-06-18.** `compile_brief` now
  cross-checks the VIX/USDINR figures cited in `macro_brief.md` against the
  bundle's macro snapshot and warns on a mismatch (warn-only; FII/DII left to the
  human). This catches the brief contradicting the bundle. The two data-layer
  follow-ups are now **✅ done 2026-06-19**: auto re-pull of a stale/missing macro
  snapshot (F-035, `trading macro refresh`) and cross-source verification of the
  bundle's own figures (F-036, `reconcile_macro` + `trading macro verify` +
  `_render_macro` annotation + `/macro-doctor`) — both in ingestion, keeping the
  narrative-compile step decoupled.
- **Refuse-stale (F-026) ✅ done 2026-06-18.** The 12-hour gate is now
  deterministic in `compile_brief` (`StaleBundleError`, `--allow-stale` override),
  not just `SKILL.md` text.
- **Three-way heading coupling (F-027)** has no spanning test.
- **Manual narrative step** means no brief on days the human skips `/analyst`;
  acceptable for solo use but a single point of process failure (F-003).
- **`assemble_context` docstring is stale** — it lists "header, macro,
  candidates, holdings health, open trades, matured predictions" and omits the
  sector and ranker sections added in Phases 12.6/16. Minor. → F-028.
