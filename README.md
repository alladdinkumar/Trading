# Trading

A Python application for learning from daily market trends, analyzing portfolios, and planning trades — integrated with Zerodha Kite via MCP.

## What It Does

- Pulls live portfolio holdings and GTT orders from Zerodha Kite
- Researches market sentiment, quarterly results, and analyst targets for held stocks
- Analyzes whether GTT sell targets are realistic within a given time horizon
- Stores raw Kite data and research notes as dated markdown snapshots for historical reference

## Tech Stack

| Tool | Purpose |
|------|---------|
| Python 3.9+ | Core language |
| UV | Package manager (`uv sync`, `uv run`) |
| Ruff | Linting and formatting |
| mypy | Type checking |
| pytest | Testing |
| Streamlit / Marimo | UI dashboard (TBD) |
| Kite MCP | Live Zerodha broker data via Claude Code |

## Project Structure

```
Trading/
├── CLAUDE.md                  # Claude Code instructions for this project
├── README.md                  # This file
│
├── data/                      # All market data and research
│   ├── README.md              # Data directory index and known issues log
│   ├── raw/                   # Snapshots pulled directly from Kite MCP
│   │   └── YYYY-MM-DD/
│   │       ├── profile.md     # Zerodha account profile
│   │       ├── holdings.md    # All equity holdings with quantities and P&L
│   │       └── gtt_orders.md  # All active GTT orders
│   └── research/              # AI-assisted analysis compiled per session
│       └── YYYY-MM-DD/
│           ├── market_context.md      # Macro/sector backdrop for the session date
│           ├── portfolio_analysis.md  # Portfolio P&L, GTT viability, exit projections
│           └── stocks/
│               └── SYMBOL.md          # Per-stock deep dive (results, analysts, GTT verdict)
│
├── src/                       # Application source (to be built)
│   ├── data/                  # Market data fetching and caching
│   ├── analysis/              # Technical indicators, trend detection
│   ├── strategy/              # Trading logic and decision rules
│   └── ui/                    # Streamlit or Marimo dashboard
│
└── tests/                     # pytest test suite
```

## Architecture (Planned)

Layers are kept decoupled so each can be tested independently and works with cached/offline data during development:

- **Data layer** (`src/data/`): Fetches and caches market data from yfinance, Kite API, or MCP
- **Analysis layer** (`src/analysis/`): Technical indicators, trend detection, pattern recognition
- **Strategy layer** (`src/strategy/`): Trading logic, GTT planning, decision rules
- **UI layer** (`src/ui/`): Streamlit or Marimo dashboard for viewing trends and planning trades

## Common Commands

```bash
# Install dependencies
uv sync

# Run linter
ruff check .

# Format code
ruff format .

# Type check
mypy src/

# Run all tests
pytest

# Run a single test
pytest tests/test_foo.py::test_bar -v
```

## Paper Trading

The paper-trading layer ([`src/trading/paper/`](src/trading/paper)) persists signals,
mock positions, and daily MTM/reconciliation runs against the SQLite store. The
exit-logic from `strategy.exits` (trailing stop, target, time-stop) drives the
MTM pass; matured predictions are filled in at reconcile-time.

| Command | Purpose |
|---|---|
| `trading paper-open --symbol X --entry .. --stop .. --target .. --qty .. [--horizon N --atr A]` | Log a signal + open a paper trade + record the prediction (atomic). |
| `trading paper-mtm [--date YYYY-MM-DD]` | Mark every open trade to the day's parquet bar; closes trades on stop/target/time, ratchets trailing stops on holds. |
| `trading paper-reconcile [--date YYYY-MM-DD --cash X]` | Evaluate matured predictions and write today's `portfolio_snapshots` row (cash + equity + drawdown). |
| `trading paper-status` | Show open trades, the most-recent 10 closes, and the latest snapshot. |

Daily flow: `paper-open` (during/after `scan`) → `paper-mtm` (post-close) →
`paper-reconcile` (post-close) → `paper-status` (any time).

## Kite MCP Integration

Live broker data is accessed via the Kite MCP server configured in `.mcp.json`. Claude Code connects to Zerodha Kite and can:

- Fetch holdings, positions, GTT orders, quotes, historical data
- Place, modify, and cancel orders
- Pull margins, trades, and order history

Run `login kite mcp` at the start of each session to authenticate.

## Data Sessions

Research and raw data are stored under `data/` with dated folders. Each session produces:

| File | Contents |
|------|----------|
| `raw/YYYY-MM-DD/holdings.md` | Live holdings snapshot from Kite |
| `raw/YYYY-MM-DD/gtt_orders.md` | Active GTT orders with trigger/sell prices |
| `research/YYYY-MM-DD/market_context.md` | Macro outlook, sector sentiment, results calendar |
| `research/YYYY-MM-DD/portfolio_analysis.md` | P&L table, GTT viability, projected exit values |
| `research/YYYY-MM-DD/stocks/SYMBOL.md` | Per-stock: results, analyst targets, GTT verdict |

See [`data/README.md`](data/README.md) for the session index.

## Current Portfolio (as of 2026-05-09)

| Symbol | Qty | Avg Buy (₹) | CMP (₹) | P&L |
|--------|-----|-------------|---------|-----|
| RVNL | 594 | 328.21 | 305.00 | -₹13,784 |
| TATAPOWER | 340 | 384.90 | 436.00 | +₹17,374 |
| NTPC | 195 | 340.34 | 402.20 | +₹12,063 |
| IRB | 1904 | 24.55 | 21.43 | -₹5,942 |
| PFC | 54 | 364.06 | 461.35 | +₹5,254 |
| RECLTD | 77 | 359.14 | 359.40 | +₹20 |
| COALINDIA | 69 | 463.68 | 456.40 | -₹502 |
| IDFCFIRSTB | 250 | 72.90 | 71.27 | -₹407 |
| IREDA | 54 | 132.69 | 134.60 | +₹103 |
| JIOFIN | 23 | 232.82 | 249.34 | +₹380 |
| MAZDOCK | 6 | 2303.61 | 2657.30 | +₹2,122 |
| **Total** | | | | **+₹16,682 (+2.97%)** |
