# Working Prompt — Session Bootstrap

> Paste this at the start of every Claude Code session on this project.
> It tells Claude where to start, how to work, and what's already decided.

---

## You are working on

A single-user, local Python trading-intelligence system for the Indian market. Goals: dip-scanner across Nifty 200, portfolio health check on existing Kite holdings, monthly ₹1L SIP allocator, backtest engine, paper-trade ledger. Paper-trades only in v1 — no real-money execution.

**Design is locked.** The full spec is at `docs/superpowers/specs/2026-05-11-trading-system-design.md`. **Read it first** before starting any work — every architectural decision is already made there. Do not propose alternative architectures unless something demonstrably breaks.

## First thing every session

1. Read `PROGRESS.md` → find the highest-numbered phase still pending or in-progress
2. Read the matching phase in spec Section 14 for full requirements
3. Read relevant spec sections referenced from that phase (e.g. Phase 5 → spec Section 4.1)
4. Confirm with me: "I'm about to work on Phase X, sub-task X.Y — proceed?"
5. After my confirmation, mark the sub-task `[~]` in PROGRESS.md and start

## Working style

- **Small, testable chunks.** Each PROGRESS.md sub-task is 15-60 min of work. Finish, test, commit, move on.
- **Tests-first when feasible.** Indicators, sizing, exits, cost models — write the failing test, then make it pass. For data fetchers and LLM glue, write tests after stubbing.
- **Always finish a sub-task before starting the next.** No half-done work across sub-tasks. If blocked, mark `[!]` with the reason inline.
- **Lint + type-check + test must pass before commit.** `ruff check .` · `mypy src/` · `pytest -q`
- **Commit per sub-task.** Use the conventions at the bottom of PROGRESS.md.
- **Update PROGRESS.md** as part of every sub-task.
- **Do not commit without explicit user permission** when in doubt — per CLAUDE.md / repo policy.

## What NOT to do

- ❌ Do not add tech outside spec Section 12. No Kafka, Airflow, FastAPI, Docker, etc.
- ❌ Do not invent new tables or schemas. Spec Section 13 is the storage tier; Phase 1 defines the schema.
- ❌ Do not place real-money orders or modify Kite GTTs unless I explicitly ask. Paper-trade ledger only.
- ❌ Do not propose UI redesigns. Streamlit pages are listed in spec Section 11 — build them as listed.
- ❌ Do not skip tests. The whole point of phased build is each phase is verifiable.
- ❌ Do not over-engineer error handling. We're single-user, local. Crash loud, fix the cause.
- ❌ Do not add abstractions for hypothetical future needs. Concrete first, abstract only when 3+ uses exist.

## Constraints from CLAUDE.md (project rules)

- Python 3.11+
- uv for deps (`uv sync`, `uv run`)
- ruff for lint + format
- mypy for type-check
- pytest for tests
- Streamlit for UI
- Kite MCP for live broker data (use the Python `kiteconnect` SDK for scheduled jobs; MCP for interactive sessions)
- `data/raw/YYYY-MM-DD/` and `data/research/YYYY-MM-DD/` already exist — write into them, don't change shape

## Common commands

```bash
# Install / sync deps
uv sync

# Lint
ruff check .

# Format
ruff format .

# Type-check
mypy src/

# All tests (excludes @live)
pytest -q

# Live integration tests (real Kite API — manual)
pytest -m live

# Run a job locally
uv run python -m trading.jobs.pre_open

# Streamlit dashboard
uv run streamlit run src/trading/ui/app.py

# CLI commands (once Phase 5+ done)
uv run trading scan --date 2026-05-12
uv run trading backtest --years 3
uv run trading portfolio
```

## End-of-session protocol

1. All sub-tasks worked on are either `[x]` or `[~]` (in-progress) or `[!]` (blocked) — never half-marked
2. PROGRESS.md `Currently working on` / `Next up` fields updated
3. Working tree is either clean (committed) or has a clear note in chat about what's uncommitted and why
4. Tests are green or the failure is documented in PROGRESS.md

## When confused

- Re-read the relevant spec section. The answer is almost always there.
- If genuinely ambiguous, ask me. Don't guess.
- If the spec is wrong, propose a spec edit (don't just diverge).

---

## Quick reference — phase dependency order

```
0  setup
1  config + db schema   (depends on 0)
2  yfinance OHLCV       (depends on 1)
3  Kite wrapper         (depends on 1)
4  indicators           (depends on 2)
5  rule scanner         (depends on 4)
6  sizing + exits       (depends on 1, 4)
7  backtest engine      (depends on 5, 6)
8  news + sentiment     (depends on 1)
9  macro + regime       (depends on 1, 2)
10 portfolio            (depends on 3, 4, 8)
11 paper ledger         (depends on 1, 3, 6)
12 LLM analyst          (depends on 8, 9)
13 pre_open job ⭐ MVP  (depends on 1-12)
14 mid_day + post_close (depends on 13)
15 streamlit dashboard  (depends on 1, 7, 10, 11)
16 LightGBM ranker      (depends on 7, 11)
17 task scheduler       (depends on 13, 14)
18 live paper trading   (depends on 17)
```

When in doubt about order, follow this graph.
