# 00 — System Overview

> Part of the [`docs/architecture/`](./PROGRESS.md) current-system design set.
> This file is the map; the numbered files that follow are the territory.

## 1. What this system is

A **single-operator, AI-assisted trading & portfolio-intelligence system for the
Indian equity market (NSE/BSE)**. It learns from daily market trends and plans
trades. It is currently in a **paper-trading** phase — it records simulated
trades and tracks their outcomes; it does **not** place real orders.

The system's job, every market day, is to answer three questions:

1. **What is the market regime today?** (risk-on / neutral / risk-off, from
   macro signals.)
2. **Which stocks are valid buy candidates?** (a rules gate, then an ML re-rank.)
3. **How are my existing holdings doing, and should anything be trimmed/exited?**

It produces a human-readable **daily brief** (assembled from data + an LLM
analyst pass) and an auto-maintained **paper-trade ledger**, then reconciles
predicted vs. actual outcomes over time.

### Two-layer decision model

The strategy is deliberately split so the cheap, explainable layer gates the
expensive, statistical one:

- **Layer A — Rule scanner** (`strategy/rules.py`): ten hard pass/fail filters
  (uptrend, pullback, RSI band, liquidity, no critical news, regime, F&O-ban,
  etc.). A stock must pass to be considered at all. This is fully transparent.
- **Layer B — LightGBM ranker** (`ranking/ranker.py`): scores the Layer-A
  survivors and selects the top-K. It is *advisory* and **cold-starts safely** —
  with no trained/active model, every rules-passing candidate is kept, so the
  system degrades to pure Layer A.

### Human-in-the-loop by design

The system is **reminder-driven**, not fully autonomous. Windows Task Scheduler
fires Slack/toast reminders at fixed IST times; the operator runs each command
manually in sequence. Two responsibilities are intentionally delegated to a
Claude Code LLM session via *skills*:

- **Kite data fetch** — the broker (Zerodha Kite) is read through MCP tools
  inside Claude Code (`/kite-snapshot`, `/kite-quotes-snapshot`), which write
  JSON files that the Python jobs consume. Python never holds the broker session.
- **The analyst narrative** — `/analyst` reads a machine-assembled context
  bundle and writes the prose brief (macro commentary, per-candidate cases).

This keeps secrets and live-account access in the interactive session, and uses
the LLM only for judgement/narrative, never for raw numeric computation.

## 2. The daily lifecycle at a glance

All times are IST. `<date>` is today's ISO date. Each block is a short sequence
of a Python command and (where broker data is needed) a Claude Code skill.

```mermaid
flowchart TD
    subgraph PRE["Pre-open block · 08:30–08:45"]
        A1["/kite-snapshot<br/>(holdings + GTTs → JSON)"] --> A2["trading pre-open --date date<br/>(macro, sector, news, OHLCV refresh, scan, rank,<br/>plan → _pending_entries.json, assemble bundle)"]
        A2 --> A3["/analyst<br/>(reads _context.md → narrative .md files)"]
        A3 --> A4["trading brief compile --date date<br/>(→ brief.md)"]
    end
    subgraph IEP["IEP block · 08:55–09:00"]
        B1["/kite-quotes-snapshot<br/>(candidate quotes → JSON)"] --> B2["trading pre-open-iep --date date<br/>(gap + sector filter, rerank, rewrite bundle)"]
    end
    subgraph OPEN["Open-fills block · ~09:15–09:20 · post-open"]
        E1["trading open-fills (prepare)<br/>(_pending_entries → _quote_symbols.txt)"] --> E2["/kite-quotes-snapshot"] --> E3["trading open-fills --apply<br/>(re-plan at live LTP, open funded paper-trades)"]
    end
    subgraph MID["Mid-day MTM · 12:25–12:35"]
        C1["trading mid-day (prepare)"] --> C2["/kite-quotes-snapshot"] --> C3["trading mid-day --apply<br/>(mark-to-market open paper-trades)"]
    end
    subgraph POST["Post-close · 16:05–16:15"]
        D1["trading post-close (prepare)"] --> D2["/kite-quotes-snapshot"] --> D3["trading post-close --apply<br/>(final MTM, reconcile, portfolio snapshot)"]
    end
    PRE --> IEP --> OPEN --> MID --> POST
```

> **Plan/fill split (2026-06-23 design).** `pre-open` only *plans* — it records the
> selected candidates to `_pending_entries.json` and opens **no** trades. The
> **open-fills** block then fills those at the live post-open LTP (re-planning qty/
> stop/target from the LTP via the same daily-budget planner), so the paper book
> reflects an obtainable market-order price rather than an unfillable previous
> close. A stale/missing quote snapshot aborts open-fills (exit 2) rather than
> filling on old prices.

### The decision funnel (universe → paper trade)

How a symbol travels from candidate to position — each stage can only *shrink*
the set:

```mermaid
flowchart LR
    U["Nifty 50<br/>(nifty50.txt)"] --> A["Layer A<br/>10 hard rules"]
    A --> B["Layer B<br/>LightGBM top-K<br/>(cold-start: keep all)"]
    B --> G["IEP gap filter<br/>(regime-scaled)"]
    G --> PEND["_pending_entries.json<br/>(plans, no trades)"]
    PEND --> LTP["Open-fills @ live LTP<br/>stop = LTP − 1.5×ATR"]
    LTP --> BUD["Daily-budget planner<br/>cash · p_win calibration ·<br/>lot + sector caps"]
    BUD -->|funded| T["paper_trades row<br/>(created_by=open_fills)"]
    BUD -->|unfunded| V["visibility signal only"]
    T --> MTM["MTM (mid-day + post-close)<br/>stop / target / 25d time / trail"]
    MTM --> REC["reconcile → predictions,<br/>portfolio_snapshots (equity curve)"]
    REC -.->|matured outcomes| BUD
    REC -.->|labels| B
```

The two dotted feedbacks are the learning loops: matured trade outcomes
re-calibrate the score→p_win mapping the budget planner uses, and realized
forward returns become the labels the weekly retrain fits — both gated so a
young system degrades to rules-only rather than trusting itself early.

Cadence beyond the daily loop:

- **Weekly (Sunday)** — `trading weekly-train`: performance review +
  walk-forward LightGBM retrain with a soft-promotion gate.
- **Monthly (1st)** — `trading sip`: SIP allocation plan (top-up / new / cash).

The canonical command sequence with exact times lives in
[`docs/daily-workflow.md`](../daily-workflow.md); operational setup (Slack, Task
Scheduler import, troubleshooting) lives in [`docs/operations.md`](../operations.md).

## 3. Tech stack

| Concern | Choice |
|---|---|
| Language | Python `>=3.11,<3.12` (pinned — `ta` lacked reliable 3.12 wheels on Windows) |
| Package manager | `uv` |
| Lint / format | `ruff` (line length 100, broad rule set) |
| Type checking | `mypy --strict` on `src/trading` |
| Tests | `pytest` (markers: `live`, `integration`, `slow`); 91 test files |
| Data / numerics | `pandas`, `polars`, `pyarrow`, `numpy`, `ta` |
| Storage | SQLite (`data/app.db`) + Parquet (per-symbol OHLCV) |
| Sources | `kiteconnect` (fallback), `yfinance`, `nsepython`, `feedparser` |
| ML / sentiment | `lightgbm`, `scikit-learn`, `joblib`, `transformers` + `torch` (FinBERT) |
| Backtest | event-loop engine (custom, hand-rolled — no backtest library) |
| LLM | Claude Code skills (no LLM SDK — the production path uses skills, not API calls) |
| UI | `streamlit` + `plotly` |
| CLI / output | `typer`, `rich` |
| Ops | `loguru` (logging), `plyer` (Windows toast), Slack webhook |

> Note for reviewers: there are deliberately **no backtest or LLM-SDK packages**
> in the manifest — the backtester is a custom event loop (Phase 7 deviation) and
> the LLM is invoked via Claude Code skills (Phase 12 deviation, no API credits).
> The earlier `vectorbt`/`anthropic` placeholders were dropped per finding F-001
> so the dependency list isn't mistaken for the architecture.

## 4. Repository map

```
src/trading/
├── config.py            # Paths + Settings (frozen dataclasses); .env loading
├── cli.py               # Typer app — the entire operator command surface
├── clock.py             # canonical IST clock (now_ist / today_ist)
├── data/                # Layer 1: ingestion (decoupled from analysis)
│   ├── yfinance.py      #   historical OHLCV fetch (parquet I/O is store/ohlcv.py)
│   ├── ohlcv_refresh.py #   incremental OHLCV top-up used by pre_open
│   ├── cache.py         #   requests-cache for HTTP fetchers
│   ├── kite.py          #   kiteconnect SDK wrapper (emergency fallback only)
│   ├── kite_snapshot.py #   reads broker JSON written by /kite-snapshot skill
│   ├── quotes_snapshot.py #  reads intraday quote JSON (freshness-checked)
│   ├── snapshot_schema.py #  validates the skill-written JSON contract
│   ├── reconcile.py     #   predicted-vs-actual outcome reconciliation
│   ├── macro.py         #   global indices, USDINR, VIX, FII/DII
│   ├── macro_cross.py   #   /macro-doctor second-source cross-check reader
│   ├── fno_ban.py       #   NSE F&O ban-list fetch (rules filter input)
│   ├── news.py          #   RSS aggregator + NSE event calendar
│   ├── sector.py        #   11 NSE sectoral indices + relative strength
│   └── universe.py      #   universe loaders (ingest list + Nifty-50 candidates)
├── features/            # Layer 2: analysis
│   ├── technicals.py    #   indicator suite (RSI, MACD, ATR, EMA, ADX, …)
│   ├── sentiment.py     #   FinBERT headline scoring
│   └── regime.py        #   4-axis macro regime voter
├── strategy/            # Layer 3: decision logic
│   ├── rules.py         #   Layer A — 10 hard filters
│   ├── sizing.py        #   position sizing (risk budget × regime × caps)
│   ├── exits.py         #   stop / target / time / trailing exit logic
│   ├── daily_budget.py  #   daily-deploy planner (cash, lot & sector caps)
│   ├── calibration.py   #   score → p_win calibration from matured outcomes
│   ├── trajectory.py    #   entry-trajectory / dip-quality evidence
│   └── factors.py       #   cross-sectional factor library (research only)
├── ranking/             # Layer B — LightGBM ranker
│   ├── ranker.py        #   scoring + cold-start + backtest signal provider
│   └── ranker_{features,labels,train,io}.py
├── backtest/            # costs, event-loop engine, walk-forward, metrics
├── portfolio/           # holdings health, GTT Monte-Carlo, SIP allocator
├── paper/               # ledger, positions, funds, pending entries, journal,
│                        #   mark-to-market, cash reconciliation
├── llm/                 # context-bundle assembly + brief compilation
├── jobs/                # orchestrators: pre_open, pre_open_iep, open_fills,
│                        #   mid_day, post_close, weekly_train, monthly_sip,
│                        #   daily_unattended (broker-free spine)
├── store/               # SQLite (db, migrations, repos) + parquet + registry
├── ops/                 # runner/SCHEDULE, holiday calendar, logging, notify,
│                        #   run_status (half-run detection), retention (prune)
└── ui/                  # Streamlit dashboard (Home + 4 pages: Kite Portfolio,
                         #   Today Signals, Paper Journal, Paper Portfolio)

data/                    # gitignored runtime data
├── app.db               # SQLite (all tabular state)
├── parquet/             # per-symbol OHLCV history (subdir "nifty200" is a
│                        #   historical misnomer — it holds the whole universe)
├── raw/<date>/          # broker JSON snapshots (holdings, gtts, quotes_HHMM,
│                        #   _quote_symbols.txt, macro_cross_HHMM)
├── research/<date>/     # context bundle + analyst .md + brief.md + open_fills.md
├── cache/               # HTTP cache + FinBERT model cache
└── logs/                # rotating per-job loguru logs + failures.log
models/                  # LightGBM pickles + registry.csv
.claude/skills/          # analyst, kite-snapshot, kite-quotes-snapshot,
                         #   macro-doctor, daily-workflow (day orchestrator)
docs/scheduler/          # Windows Task Scheduler XML (one per reminder slot)
```

## 5. CLI command surface

The Typer app (`trading …`) is the entire operator interface. Commands group by
purpose:

| Group | Commands |
|---|---|
| **Daily jobs** | `pre-open`, `pre-open-iep`, `open-fills` (prepare / `--apply`), `mid-day`, `post-close`, `daily-unattended` |
| **Periodic** | `weekly-train`, `sip` |
| **Brief** | `brief assemble-context`, `brief compile` |
| **Data ingest** | `ingest-history`, `refresh-ohlcv`, `ingest-news`, `macro snapshot`, `macro refresh`, `macro verify`, `sector` |
| **Analysis** | `scan`, `backtest`, `factor-eval` |
| **Portfolio / paper** | `portfolio`, `paper-open`, `paper-mtm`, `paper-status`, `paper-reconcile`, `funds add`, `funds top-up`, `funds list`, `funds balance` |
| **Ranker (Layer B)** | `train-ranker`, `ranker-status` |
| **Broker fallback** | `kite-emergency-login`, `kite-emergency-snapshot` |
| **Ops** | `remind --slot <name>`, `notify-test`, `status` (run-status of the day's blocks), `prune` (retention) |

## 6. Core design principles

These recur in every layer; later docs assume them.

1. **Ingestion is decoupled from analysis.** `data/` fetchers know nothing about
   strategy; analysis runs against cached parquet/SQLite, so the whole pipeline
   works offline against historical data during development.
2. **Graceful degradation everywhere.** Every external source can fail
   independently without aborting the job: yfinance down → empty macro; Kite
   token absent → empty holdings; RSS down → no news. The job logs a warning and
   continues with reduced data, rather than crashing.
3. **Idempotent re-runs.** Jobs use SQLite UPSERTs and idempotent file writes
   plus guards (e.g. `_already_opened_today`), so re-running a job for the same
   date is safe and doesn't double-count.
4. **Pure functions for logic.** The decision cores — rules, sizing, exits,
   regime voting, health scoring, GTT simulation — are pure functions over
   dataclasses. This makes them unit-testable in isolation and is why the suite
   can be large and fast.
5. **Extension points over rewrites.** The backtest engine takes a
   `SignalProvider`, so the same engine runs the rules-only baseline *and* the
   Layer-B ranker without modification.
6. **Conservative simulation.** Fills happen next-day-open with slippage; on a
   same-bar stop/target tie the stop wins; costs use a Zerodha-accurate model.
   The paper P&L is intended to *under*-promise.
7. **Secrets stay in the interactive session.** Live broker access is mediated
   by Claude Code MCP skills writing JSON to disk; the batch Python never carries
   the broker session token in the production path.

## 7. How to read the rest of this set

- **[01-architecture](./01-architecture.md)** — how the modules depend on each
  other and the patterns that hold it together.
- **[02-data-schema](./02-data-schema.md)** — every table, file, and on-disk
  contract. Read this before the layer docs; they reference it.
- **03–08** — one layer per file, each explaining *what it does, how it works,
  and where it's fragile*.

---

## ⚠️ Robustness notes / open questions (system-wide)

Surfaced here at the overview level; each is expanded in its layer doc.

- **Single operator, manual sequencing.** The daily flow depends on a human
  running ~13 commands in order at the right times. A missed step (e.g. skipping
  IEP) silently leaves the bundle un-reranked. `ops/run_status.py` + the `status`
  command now detect a half-run day after the fact, but nothing *enforces*
  ordering, and the open-fills prepare step is not tracked (F-062) — a day can
  read "complete" with zero entries filled.
- **Broker data via LLM skill is a trust/consistency boundary.** The JSON
  contract between the `/kite-*` skills and `data/*_snapshot.py` is validated by
  `data/snapshot_schema.py` on read. That checks shape and types; it cannot catch
  semantically wrong-but-well-formed writes (wrong exchange, stale copy-paste),
  which remain a residual risk of the skill boundary.
- **Dependency vs. reality drift.** ~~`vectorbt`/`anthropic` are installed but
  unused in production; conversely the real engine is custom.~~ Resolved by F-001:
  both placeholder deps were pruned and the manifest now carries a breadcrumb note
  for the custom-engine / skills deviations.
- **Paper-only, no execution path.** Nothing here places orders. The jump to
  Phase 19 (real money) is gated on ≥3 months OOS Sharpe > 1.0 and will need its
  own risk/kill-switch design — currently absent.
- **Time-zone correctness is load-bearing.** Everything keys off IST
  (`Asia/Kolkata`); `clock.py` is now the canonical clock (`now_ist` /
  `today_ist`) and the jobs use it. A few call sites still read the naive host
  clock — notably the quote-snapshot freshness check (F-058) — so a host not set
  to IST can mis-judge quote staleness.
