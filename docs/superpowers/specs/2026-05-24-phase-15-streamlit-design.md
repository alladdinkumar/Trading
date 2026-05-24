# Phase 15 — Streamlit dashboard (design)

> **Status:** approved (autonomous build — user authorized "make
> recommended choices yourself, … use examples from existing trading
> dashboards, do it on your own and test and improve" on 2026-05-24).
> Spec written in flight rather than negotiated.
>
> **Parent spec:** [`docs/superpowers/specs/2026-05-11-trading-system-design.md`](2026-05-11-trading-system-design.md) §6/§11/§15
> **Plan:** [`docs/superpowers/plans/2026-05-24-phase-15-streamlit.md`](../plans/2026-05-24-phase-15-streamlit.md) (next)

## 1. Goal

A read-only Streamlit dashboard that surfaces everything Phases 1-14
already produce: daily regime, holdings + health, today's candidates +
why they pass/fail, paper-trade history + P&L. Optimized for quick
visual scanning before/after each scheduled job — not for executing
trades.

## 2. Non-goals

- No order placement, no DB writes, no job triggers (read-only per
  parent-spec §22 risk: "dashboard becomes the trader's casino UI").
- No multi-user / auth.
- No backtest dedicated page — Phase 7 backtests run on demand and dump
  markdown; we'll render those from a future helper if needed.
- No live tick streaming. Page loads pull a fresh snapshot via cached
  readers (TTL 60 s).

## 3. Architecture

```
src/trading/ui/
├── Home.py             # Entry — "Overview" page (Home)
├── data.py            # @st.cache_data wrappers around repo / parquet / markdown / kite_snapshot
├── charts.py          # Pure Plotly figure builders
├── components.py      # Shared widgets: regime_badge, kpi_tile, rule_chip_grid, empty_state
└── pages/             # Streamlit multipage auto-discovery
    ├── 1_Portfolio.py
    ├── 2_Today_Signals.py
    └── 3_Paper_Journal.py
```

**Entry:** `uv run streamlit run src/trading/ui/Home.py`

Streamlit auto-discovers files in `pages/` and renders them in the
sidebar in numeric order. `Home.py` is always the home page.

### Data layer (`ui/data.py`)

One thin module that owns every read the dashboard does. Each function
is cached with `@st.cache_data(ttl=60)` so a live session re-reads
state at most once per minute. Cache keys are the function args
(usually a date string), so switching the sidebar date invalidates
correctly.

Public helpers (selected):

| Function | Returns |
|---|---|
| `load_macro_snapshot(as_of)` | dict from `macro_snapshot` for that date (regime, VIX, FII, USDINR, …) or `None` |
| `load_portfolio_snapshots(start, end)` | DataFrame: date, cash, equity, drawdown_pct |
| `load_paper_trades(open_only=False)` | DataFrame of paper_trades joined with signals (entry/stop/target/symbol) |
| `load_signals_by_date(date)` | list[Signal] |
| `load_predictions(symbol=None)` | DataFrame of predictions w/ matured rows highlighted |
| `load_holdings(as_of)` | list[dict] from `data/raw/{as_of}/holdings.json` via `kite_snapshot.read_holdings`; returns `[]` when missing rather than raising |
| `load_gtts(as_of)` | same, `kite_snapshot.read_gtts` |
| `load_quote_snapshot(as_of)` | latest `quotes_HHMM.json` (capture time + dict) via `quotes_snapshot.read_latest_quotes`; `None` when missing |
| `load_ohlcv(symbol)` | parquet → DataFrame of OHLCV+indicators (uses `features.technicals.add_indicators`) |
| `load_brief_section(as_of, filename)` | markdown text or `None` (handles `_context.md`, `macro_brief.md`, `brief.md`, `mid_day_update.md`, `post_close_summary.md`) |
| `available_research_dates()` | sorted list of `data/research/YYYY-MM-DD/` dir names — feeds the sidebar date picker |

All helpers raise nothing on missing data — return `None` / empty
DataFrame / empty list so pages can branch on truthiness and show
`empty_state()` instead of an exception.

### Chart layer (`ui/charts.py`)

Pure functions returning `plotly.graph_objects.Figure`. No Streamlit
imports — keeps them unit-testable.

| Builder | Input | Notes |
|---|---|---|
| `equity_curve(df)` | snapshots DataFrame | line + drawdown shaded as second axis |
| `drawdown_curve(df)` | snapshots DataFrame | area chart |
| `candlestick(df, indicators=…)` | OHLCV DataFrame | OHLC + optional SMA-20/50/200 overlays + volume sub |
| `sector_pie(holdings)` | list[dict] | donut |
| `pnl_distribution(trades)` | closed trades DataFrame | histogram |
| `win_loss_donut(trades)` | closed trades DataFrame | donut (WIN/LOSS/BREAKEVEN) |
| `regime_history(snapshots)` | macro history | step plot of regime over time |

All builders apply a shared `_apply_theme(fig)` for consistent dark
template + tight margins + Inter font.

### Components (`ui/components.py`)

Small Streamlit-aware widgets (these *do* import `streamlit`):

- `regime_badge(regime)` — coloured chip (green/amber/red for ON/NEUTRAL/OFF)
- `kpi_tile(label, value, delta=None, fmt=…)` — st.metric wrapper with `₹` / `%` formatting
- `health_chip(verdict, score)` — coloured pill (HOLD green / TRIM amber / EXIT red)
- `rule_chip_grid(rules_passed_json)` — 10 mini-chips one per Layer-A rule, green ✓ / red ✗
- `empty_state(title, hint)` — large grey panel with an instruction (e.g. "Run `trading pre-open 2026-05-24` to seed data")
- `sidebar_date_picker(label='Date')` — wraps `st.date_input` defaulting to today, using `available_research_dates()` to constrain selectable dates when reasonable

## 4. Pages

Each page follows the same scaffold: sidebar date picker → header with
regime badge → content blocks → empty-state guards.

### 4.1 Overview (`Home.py`)

The "did anything change today / is anything broken" page.

```
┌──────────────────────────────────────────────────────────────────┐
│  Trading System • 2026-05-22                       [RISK_ON ●]   │
├──────────────────────────────────────────────────────────────────┤
│  ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐               │
│  │ Equity  │ │ Today   │ │ Drawdown │ │ Open     │               │
│  │ ₹100k   │ │ +0.00%  │ │   0.0%   │ │   0      │               │
│  └─────────┘ └─────────┘ └──────────┘ └──────────┘               │
│                                                                  │
│  Equity curve (90d)                  │  Macro snapshot          │
│  [────── chart ──────]               │  VIX     19.4            │
│                                      │  USDINR  95.76           │
│                                      │  FII Cr  +187            │
│  Today's brief preview (first 600 chars of brief.md)             │
│  [───── markdown ─────]                                          │
│                                                                  │
│  Quick links: Portfolio · Today's Signals · Paper Journal        │
└──────────────────────────────────────────────────────────────────┘
```

KPI tiles read `portfolio_snapshots`; today-delta is `equity[today] -
equity[prev_trading_day]`. "Open" reads `count(*)` of open paper_trades.

### 4.2 Portfolio (`pages/1_Portfolio.py`)

Live holdings, sector concentration, GTT viability.

- Top tiles: Equity / Cash / N holdings / N GTTs
- Holdings table: symbol, qty, avg_price, last_price (from quote snapshot if available, else avg), P&L₹, P&L%, weight%, sector
- Sector donut + concentration table (top-3 sectors share)
- GTT table: symbol, trigger, last_price, distance%, P(hit) and expected days (from `portfolio.gtt.project_gtt_viability`)
- Per-symbol expander (`st.expander`): candlestick (last 6 months) + key indicators (RSI, ATR%, dist to 52wH) + health verdict chip + recent symbol news

Holdings come from `kite_snapshot.read_holdings(as_of)`. Empty-state if
`data/raw/{as_of}/holdings.json` missing → tells user to run
`/kite-snapshot`.

### 4.3 Today's Signals (`pages/2_Today_Signals.py`)

What did the scanner find on this date and why.

- Funnel: `Universe N → Rules-pass N → IEP-pass N → Opened N` (numbers from `_context.md` parse + `signals` table + `paper_trades`)
- Candidates table: symbol, conviction, entry/stop/target, R/R, sector
- For each row: a horizontal rule-chip-grid showing all 10 Layer-A rules pass/fail
- Selecting a row opens a detail panel: 6-month candlestick + signal markers, sentiment summary (7d/30d, news count, has_critical), brief excerpt for that candidate (`candidates/SYMBOL.md` if present)
- Banner if `as_of`'s macro snapshot says RISK_OFF or regime gate failed

Empty-state if `signals` table empty for the date → "Scanner produced
no candidates that pass the regime gate." with a hint to inspect
`_context.md`.

### 4.4 Paper Journal (`pages/3_Paper_Journal.py`)

Track record. Visible even when current paper_trades is empty.

- Sidebar: date-range picker (defaults: inception → today)
- Top metric tiles: Total trades / Hit rate / Profit factor / Sharpe / Expectancy
- Equity curve (full range) + drawdown overlay
- Open trades table: symbol, days held, entry, current_stop, distance to stop/target, unrealised P&L
- Closed trades table: symbol, side, entry, exit, P&L₹, P&L%, exit reason chip (TARGET/STOP/TIME/MANUAL), days held
- Win/loss donut + P&L histogram
- Prediction accuracy: scatter of predicted_return_pct vs actual_return_at_horizon for matured predictions

Empty-state when no closed trades → "No closed paper trades yet. Run
`trading post-close --apply <date>` after each session to populate."

## 5. Visual design language

- **Theme:** dark (set via `.streamlit/config.toml`); consistent across all charts via `_apply_theme`.
- **Colors:**
  - Green `#26A69A` — positive / RISK_ON / PASS / HOLD
  - Red `#EF5350` — negative / RISK_OFF / FAIL / EXIT
  - Amber `#FFB300` — NEUTRAL / TRIM / warning
  - Blue `#42A5F5` — info / equity line
  - Grey `#9E9E9E` — empty-state / muted text
- **Typography:** Streamlit default + Inter for chart annotations (fallback sans-serif if Inter unavailable on the host — tested).
- **Tables:** `st.dataframe` with `column_config` for `NumberColumn(format="₹%.0f")`, `NumberColumn(format="%.2f%%")`, and small Plotly sparklines via `LineChartColumn` for short price history.
- **Charts:** tight margins (l=40, r=20, t=30, b=30), zero chart title, axis-label-only legends, hovermode="x unified" on time-series.

## 6. Error handling

| Failure mode | Response |
|---|---|
| `data/app.db` missing | App-level `st.error` with init hint; no page tries to read |
| Empty table for a query | `empty_state(...)` placeholder block — never a stacktrace |
| Missing `data/raw/{as_of}/holdings.json` | Inline hint: "Run `/kite-snapshot` for this date" |
| Parquet missing for a symbol | Detail expander shows "No history cached" — outer table still renders |
| Plotly fig with zero rows | builder returns `Figure()` with annotation "No data for selected range" |
| Markdown file missing | Section omitted entirely (no broken render) |
| Stale quote snapshot (> 30 min) | Show capture time in italics with amber tag — no abort (mid_day already enforces the hard gate elsewhere) |

## 7. Testing

| Layer | Tool | Coverage |
|---|---|---|
| Chart builders | pytest | One unit test per builder: synthetic DataFrame → assert `len(fig.data) > 0`, axes present, theme applied |
| Data loaders | pytest + in-memory SQLite | empty-DB returns / seeded-DB shape contracts |
| Pages | `streamlit.testing.v1.AppTest` | per page: `at.run()` succeeds with empty DB + with seeded DB; assert presence of key headers + at least one chart / dataframe |
| Visual | Playwright MCP | Manual smoke after `streamlit run` — screenshot each page on real data, iterate on layout |

Goal: 15-25 new tests across builders + loaders + page-smokes. Suite
target ~520 passed.

## 8. Out of scope / deferred

- Backtest viewer page (Phase 7 reports are on-demand markdown — render via a small helper when paper_trades has enough closed history to make Sharpe meaningful)
- Live auto-refresh (Streamlit reruns on widget interaction; explicit reload button on each page is enough)
- Mobile responsive tuning (the trader uses desktop)
- "Open paper trade from candidate row" button — explicitly **not** built, per parent-spec §22

## 9. Acceptance

1. `uv run streamlit run src/trading/ui/Home.py` opens a 4-page dashboard with no console errors against the current DB state (paper_trades empty, 2 portfolio_snapshots, 2 macro_snapshots, 11 holdings via Kite snapshot for 2026-05-22).
2. Playwright MCP screenshots saved for each page; layout passes a "could I quickly tell if anything's broken / how the portfolio looks?" eyeball test.
3. `pytest -q` green; `ruff check .` clean; `mypy src/` clean.
4. PROGRESS.md Phase 15 sub-items marked `[x]`; commit `feat(ui): streamlit dashboard (Phase 15)` pushed to origin/main.
