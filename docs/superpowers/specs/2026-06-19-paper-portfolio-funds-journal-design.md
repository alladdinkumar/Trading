# Paper Portfolio + Funds Tracking + Journal Deviation — Design

**Date:** 2026-06-19
**Status:** Approved (design); pending spec review before implementation plan.

## Goal

Give the paper-trading side a Kite-portfolio-style view: a per-symbol holdings
table (qty, avg, invested, LTP, current value, P&L ₹, P&L %, today's P&L), an
Invested / Current / P&L summary, and separately-tracked **funds** (an initial
₹1L that can be topped up over time). Add bought-date / expected-target-date /
deviation columns to the existing Paper Journal. All existing paper-trading
data (trades, snapshots, equity curve) is retained — every change is additive.

## Background — what exists today

- **Funds** are a hardcoded constant `INITIAL_CAPITAL = 100_000.0` in
  `src/trading/paper/reconcile.py`. Cash is *derived* from the trade ledger by
  `compute_paper_cash`: `initial − deployed − costs + realised proceeds`
  (F-023 makes realised gains compound; F-025 applies Zerodha charges +
  slippage per fill). There is no funds/deposits table, so "top up capital" has
  nowhere to live.
- **Trades** live in `paper_trades` (one row per trade, not aggregated per
  symbol), joined to `signals` for symbol/side/target/horizon.
- **Daily marks** live in `portfolio_snapshots` (PK `date`): `cash`, `equity`,
  `holdings_json = {symbol: {qty, value}}` (value = close × qty at that day's
  post-close), `drawdown_pct`. Written by `reconcile_day` →
  `compute_portfolio_snapshot`.
- **UI** (Streamlit, read-only over the DB):
  - `pages/1_Portfolio.py` — the user's **real Zerodha** holdings from
    `/kite-snapshot` (holdings.json). To be renamed **Kite Portfolio**.
  - `pages/3_Paper_Journal.py` — paper trade history: open trades, closed
    trades, equity curve, KPIs, calibration.
  - `ui/data.py` — DB loaders, incl. `load_paper_trades` (joins signals) and
    `load_portfolio_snapshots`.

## Key decisions (resolved during brainstorming)

1. **Price source = latest DB mark (offline).** LTP / current value / today's
   P&L come from `portfolio_snapshots` marks, not a live call. This matches the
   read-only, offline-first UI. The page is labelled "as of last close (date)".
2. **Add funds via both CLI and UI.** The CLI command is the tested core; the
   Paper Portfolio page exposes an Add-funds widget that calls the *same* store
   function.
3. **Page naming.** Rename `1_Portfolio.py` → `1_Kite_Portfolio.py` (real
   broker holdings); add `4_Paper_Portfolio.py` (paper holdings + funds).
4. **Funds model = additive deposits ledger.** `INITIAL_CAPITAL` stays the t=0
   seed; top-ups are rows in a new `cash_ledger` table. With no top-ups, cash
   math is byte-identical to today, so existing data and the equity curve are
   unchanged.
5. **Deviation = trading-day date deviation.** Expected target date = bought
   date + `signal.horizon_days` trading days. Deviation = signed trading-day
   gap (early/late for closed, remaining/overdue for open).

## Architecture

Three additive units on top of the existing paper layer, all DB-driven and
unit-testable in isolation:

- **Funds ledger** (`paper/funds.py`) — record/list/sum top-ups; wired into
  `compute_paper_cash`.
- **Positions view** (`paper/positions.py`) — pure aggregation of open
  `paper_trades` + snapshot marks into per-symbol holdings + a summary.
- **Journal schedule** (`paper/journal.py`) — pure date helpers for
  bought/target-date/deviation.

UI pages and the CLI consume these; no SQL leaks into the UI or CLI beyond the
existing `ui/data.py` loader pattern.

## Data model

New table (next migration version after the current head). Additive — no
existing table is altered, no backfill required.

```sql
CREATE TABLE IF NOT EXISTS cash_ledger (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT NOT NULL,           -- ISO date the funds were added (YYYY-MM-DD)
  amount     REAL NOT NULL,           -- positive top-up amount in ₹
  note       TEXT,                    -- optional free-text label
  created_at TEXT NOT NULL            -- ISO timestamp the row was written
);
CREATE INDEX IF NOT EXISTS idx_cash_ledger_date ON cash_ledger(date);
```

The initial ₹1,00,000 is **not** a row — it stays the `INITIAL_CAPITAL` seed
constant, so existing DBs need no migration data. The Funds panel renders it as
a labelled "Initial capital" line above the top-up list.

## Component 1 — Funds ledger (`src/trading/paper/funds.py`, new)

```python
@dataclass(frozen=True)
class FundsDeposit:
    id: int
    date: str        # ISO date
    amount: float
    note: str | None
    created_at: str

def add_funds(conn, *, amount: float, date: str, note: str | None = None) -> FundsDeposit
def list_funds(conn) -> list[FundsDeposit]            # ordered by date, then id
def total_funds_added(conn, *, as_of: date) -> float  # SUM(amount) WHERE date <= as_of; 0.0 if none
```

- `add_funds` raises `ValueError` on `amount <= 0`. `date` defaults are the
  caller's responsibility (CLI passes today or `--date`); `created_at` is set to
  `datetime.now().isoformat()` inside the function.
- `total_funds_added` is date-filtered so re-running an older `as_of`
  reproduces the balance as it stood that day (mirrors `compute_paper_cash`).

**Reconcile wiring** (`src/trading/paper/reconcile.py`): one change in
`compute_paper_cash` — seed becomes
`initial_capital + total_funds_added(conn, as_of=as_of)`. With an empty ledger
`total_funds_added` returns `0.0`, so behaviour is unchanged. `INITIAL_CAPITAL`
and the `initial_capital` parameter are retained as-is (the t=0 seed).

## Component 2 — Positions view (`src/trading/paper/positions.py`, new)

Pure, DB-driven, no network.

```python
@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg: float            # weighted-average entry price
    invested: float       # avg * qty  (= sum of entry_price*qty over open lots)
    ltp: float            # latest snapshot mark (value/qty); falls back to avg
    current_value: float  # ltp * qty
    pnl: float            # current_value - invested
    pnl_pct: float        # pnl / invested * 100
    today_pnl: float      # qty * (ltp - prev_close)

@dataclass(frozen=True)
class PortfolioSummary:
    invested: float
    current_value: float
    total_pnl: float
    total_pnl_pct: float
    today_pnl: float
    cash: float           # compute_paper_cash(as_of)
    funds_added: float    # total_funds_added(as_of)  (top-ups only, excludes initial)
    account_value: float  # cash + current_value
    as_of_mark: str | None  # date of the latest snapshot used for marks, or None

def compute_positions(conn, *, as_of: date) -> list[Position]
def compute_summary(conn, *, as_of: date, initial_capital: float = INITIAL_CAPITAL) -> PortfolioSummary
```

**Aggregation** — open lots = `paper_trades WHERE ts_exit IS NULL`, joined to
`signals` for the symbol. Group by symbol:
- `qty = Σ qty`
- `invested = Σ (entry_price * qty)`
- `avg = invested / qty`

**Marks** — from `portfolio_snapshots`, parsing `holdings_json`:
- `ltp` = latest snapshot's `holdings_json[symbol].value / qty`. **Fallback** =
  `avg` (so P&L = 0) when the symbol is absent from the latest snapshot
  (e.g. a position opened after the last post-close).
- `prev_close` = the **second-latest** snapshot's mark for the symbol.
  **Fallback** = `ltp` (so `today_pnl = 0`) when the symbol wasn't held the day
  before, or when fewer than two snapshots exist.
- `today_pnl = qty * (ltp - prev_close)`.

`as_of_mark` = the date of the latest snapshot used (drives the page's
"as of last close" caption); `None` when no snapshots exist.

`compute_summary` sums the positions and adds cash/funds. Positions list is
sorted by `current_value` descending.

Holdings list is empty (not an error) when there are no open trades; the
summary still renders cash and funds.

## Component 3 — Journal schedule (`src/trading/paper/journal.py`, new)

Pure date helpers, trading-day based (`numpy.busday_offset` / `busday_count`),
consistent with the engine's time-stop and `mtm._days_held`.

```python
def expected_target_date(entry_iso: str, horizon_days: int) -> date:
    """Bought date + horizon_days trading days (np.busday_offset, roll='forward')."""

def deviation_label(target: date, *, exit_iso: str | None, as_of: date) -> str:
    """Signed trading-day deviation from the expected target date.

    Closed (exit_iso set):
        exit < target -> "-Nd early";  exit > target -> "+Nd late";  "on time" at 0.
    Open (exit_iso None):
        as_of <= target -> "Nd left";  as_of > target -> "+Nd overdue".
    """
```

`expected_target_date` parses the date part of `entry_iso` (handles a full ISO
timestamp). Deviation magnitudes use `np.busday_count`.

## CLI (`src/trading/cli.py`)

A `funds` Typer sub-app, following the existing command style (resolve DB via
`get_paths`, open a connection, print a table/summary):

```
trading funds add <amount> [--note TEXT] [--date YYYY-MM-DD]
    Record a top-up (defaults to today). Rejects amount <= 0. Prints the new
    balance breakdown.

trading funds list
    Print: "Initial capital ₹1,00,000", each top-up (date / amount / note),
    and "Total funds in ₹...".

trading funds balance [--date YYYY-MM-DD]
    Print: total funds in, cash available (compute_paper_cash), invested
    (open holdings at cost), holdings value, account value (cash + holdings).
```

## UI changes

### Rename: `pages/1_Portfolio.py` → `pages/1_Kite_Portfolio.py`
Update `st.set_page_config(page_title=...)`, the sidebar title, and the
`## Portfolio` header / caption to read **"Kite Portfolio"** and make explicit
it is the real Zerodha account. No logic change. (Streamlit derives the sidebar
nav label from the filename, so the rename relabels the nav too.)

### New: `pages/4_Paper_Portfolio.py`
Read-only page, mirrors the Kite Portfolio layout but sourced from the paper DB
via new `ui/data.py` loaders. Sections:

1. **Caption** — "as of last close (`as_of_mark`)" (or an empty-state when no
   snapshots exist).
2. **Summary tiles** — Invested · Current value · Total P&L (₹ with % delta) ·
   Today's P&L · Cash available · Account value.
3. **Holdings table** — columns: Symbol, Qty, Avg (₹), Invested (₹), LTP (₹),
   Current (₹), P&L ₹, P&L %, Today's P&L ₹. Empty-state when no open
   positions. Currency/percent formatting via existing column-config helpers.
4. **Funds panel** — Initial capital ₹1,00,000 → top-up list (date / amount /
   note) → Total funds in → Cash available. Plus an **Add funds** widget
   (`st.number_input` + `st.button`) that calls `funds.add_funds` and reruns.
   This makes the page the one intentional UI writer (per the user's choice);
   everything else stays read-only.

### Modify: `pages/3_Paper_Journal.py` (deviation columns)
Add to both the open- and closed-trades tables:
- **Bought** — entry date (`ts_entry[:10]`).
- **Target date** — `expected_target_date(ts_entry, horizon_days)`.
- **Deviation** — `deviation_label(...)`: `"Nd left"` / `"+Nd overdue"` for
  open; `"-Nd early"` / `"+Nd late"` / `"on time"` for closed.

### Modify: `ui/data.py`
- `load_paper_trades` SELECT gains `s.horizon_days` (needed for target date).
- Add `load_cash_ledger() -> pd.DataFrame`, `load_paper_positions(as_of)` and
  `load_paper_summary(as_of)` thin wrappers over the new pure functions
  (open a connection, call `compute_positions` / `compute_summary`).

## Error handling

- `add_funds` rejects amount ≤ 0 (`ValueError` → friendly CLI/UI message).
- No snapshots / no open trades → empty-states; cash and funds still render.
- Symbols missing from snapshots fall back (LTP→avg, prev_close→ltp); marks are
  never NULL and the page never crashes.
- Historical `as_of` excludes future top-ups (date filter), so the equity curve
  and any back-dated balance stay correct.

## Testing (TDD throughout)

- `tests/test_paper_funds.py` — `add_funds` happy path + `amount<=0` rejection;
  `list_funds` ordering; `total_funds_added` sum and `date <= as_of` filtering.
- `tests/test_paper_positions.py` — multi-lot avg/invested aggregation; LTP
  from latest snapshot; LTP fallback to avg when symbol absent; today's P&L
  from the prior snapshot mark; fallback to 0 with <2 snapshots; summary totals
  incl. cash + funds.
- `tests/test_paper_journal.py` — `expected_target_date` trading-day offset;
  `deviation_label` for early / late / on-time / remaining / overdue.
- `tests/test_cli_funds.py` — `funds add` writes a row and updates balance;
  `funds list` shows initial + top-ups; `funds balance` reflects a top-up.
- `tests/test_paper_reconcile.py` — append a case proving cash rises by a
  top-up amount, and the no-top-up path is unchanged (regression guard for the
  additive model).
- Existing suite (950 passed) stays green; ruff + mypy clean.

## Out of scope (YAGNI)

- Withdrawals / negative ledger entries (deposits only for now).
- Live intraday LTP (the offline DB mark was chosen deliberately).
- A persisted per-symbol positions table (derived on the fly instead).
- Editing the initial ₹1L (it stays the seed; top-ups add on top).
- Price-deviation columns in the Journal (date deviation only, per the
  resolved decision).
