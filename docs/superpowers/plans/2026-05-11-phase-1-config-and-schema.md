# Phase 1 — Config + SQLite Schema Implementation Plan

**Goal:** Provide a typed config layer (`config.py`) and a versioned SQLite schema (`store/`) covering all 16 tables from spec Section 13, with typed CRUD helpers for the three most-used tables (signals, paper_trades, predictions).

**Architecture:** Plain `@dataclass` types (no Pydantic overhead in the persistence layer), `python-dotenv` for `.env` loading, `sqlite3` stdlib for storage, idempotent SQL migrations indexed by a `schema_version` table.

**Tech Stack:** Python 3.11 stdlib (`sqlite3`, `pathlib`, `dataclasses`, `contextlib`) + `python-dotenv` (already in deps from Phase 0).

**Reference:** [docs/superpowers/specs/2026-05-11-trading-system-design.md](../specs/2026-05-11-trading-system-design.md) — Section 9.1 (signals/paper_trades/predictions schema), Section 13 (storage tiers list of 16 tables).

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/trading/config.py` | `.env` loading, `Paths` + `Settings` dataclasses, project constants |
| Create | `src/trading/store/db.py` | `get_conn()` context manager — opens SQLite, enables FK + WAL |
| Create | `src/trading/store/migrations.py` | All 16 table DDLs + `run_migrations(conn)` (idempotent, versioned) |
| Create | `src/trading/store/repo.py` | Typed dataclasses + CRUD for signals, paper_trades, predictions |
| Create | `tests/test_config.py` | Settings/Paths resolution |
| Create | `tests/test_db.py` | `get_conn` behavior (FK on, WAL on, dir auto-create) |
| Create | `tests/test_migrations.py` | All 16 tables created, idempotent re-run, schema_version recorded |
| Create | `tests/test_repo.py` | CRUD round-trips + FK integrity for signals → paper_trades |
| Modify | `PROGRESS.md` | Tick 1.1–1.8 |

---

## Schemas (v1)

The 16 tables, written verbatim. All timestamps stored as ISO-8601 strings (`TEXT`). All money in INR rupees. Optional fields are nullable.

```sql
-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT    NOT NULL
);

-- Strategy outputs
CREATE TABLE IF NOT EXISTS signals (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT    NOT NULL,
  symbol            TEXT    NOT NULL,
  side              TEXT    NOT NULL CHECK (side IN ('LONG', 'SHORT')),
  entry             REAL    NOT NULL,
  stop              REAL    NOT NULL,
  target            REAL    NOT NULL,
  horizon_days      INTEGER NOT NULL,
  rules_passed_json TEXT,
  ml_score          REAL,
  conviction        TEXT             CHECK (conviction IS NULL OR conviction IN ('HIGH','MEDIUM','LOW')),
  rationale         TEXT,
  created_by        TEXT    NOT NULL DEFAULT 'auto'
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts        ON signals(ts);

CREATE TABLE IF NOT EXISTS paper_trades (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id    INTEGER NOT NULL,
  ts_entry     TEXT    NOT NULL,
  entry_price  REAL    NOT NULL,
  qty          INTEGER NOT NULL,
  ts_exit      TEXT,
  exit_price   REAL,
  exit_reason  TEXT             CHECK (exit_reason IS NULL OR exit_reason IN ('TARGET','STOP','TIME','MANUAL')),
  pnl          REAL,
  pnl_pct      REAL,
  days_held    INTEGER,
  FOREIGN KEY (signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_signal ON paper_trades(signal_id);

CREATE TABLE IF NOT EXISTS predictions (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                       TEXT    NOT NULL,
  symbol                   TEXT    NOT NULL,
  predicted_return_pct     REAL    NOT NULL,
  predicted_horizon_days   INTEGER NOT NULL,
  actual_return_at_horizon REAL,
  error_pct                REAL,
  evaluated_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_predictions_symbol_ts ON predictions(symbol, ts);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  date          TEXT PRIMARY KEY,
  cash          REAL NOT NULL,
  holdings_json TEXT NOT NULL,
  equity        REAL NOT NULL,
  drawdown_pct  REAL
);

-- News & sentiment
CREATE TABLE IF NOT EXISTS news_items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT    NOT NULL,
  symbol      TEXT,
  source      TEXT    NOT NULL,
  headline    TEXT    NOT NULL,
  url         TEXT,
  sentiment   REAL,
  category    TEXT,
  is_critical INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_news_symbol_ts ON news_items(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_news_critical  ON news_items(is_critical, ts);

CREATE TABLE IF NOT EXISTS sentiment_daily (
  date                TEXT NOT NULL,
  symbol              TEXT NOT NULL,
  score_7d            REAL,
  score_30d           REAL,
  news_count          INTEGER NOT NULL DEFAULT 0,
  negative_news_count INTEGER NOT NULL DEFAULT 0,
  has_critical        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, symbol)
);

-- Market context
CREATE TABLE IF NOT EXISTS sector_daily (
  date   TEXT NOT NULL,
  sector TEXT NOT NULL,
  close  REAL NOT NULL,
  rs_5d  REAL,
  rs_20d REAL,
  rs_60d REAL,
  regime TEXT CHECK (regime IS NULL OR regime IN ('LEADING','NEUTRAL','LAGGING')),
  PRIMARY KEY (date, sector)
);

CREATE TABLE IF NOT EXISTS macro_snapshot (
  date         TEXT PRIMARY KEY,
  sgx_nifty    REAL,
  dow_fut      REAL,
  nasdaq_fut   REAL,
  sp500        REAL,
  usdinr       REAL,
  crude        REAL,
  vix          REAL,
  us_10y       REAL,
  fii_flow_cr  REAL,
  dii_flow_cr  REAL,
  regime       TEXT CHECK (regime IS NULL OR regime IN ('RISK_ON','NEUTRAL','RISK_OFF'))
);

-- F&O / derivatives
CREATE TABLE IF NOT EXISTS oi_daily (
  date      TEXT    NOT NULL,
  symbol    TEXT    NOT NULL,
  expiry    TEXT    NOT NULL,
  strike    REAL    NOT NULL,
  opt_type  TEXT    NOT NULL CHECK (opt_type IN ('CE','PE','FUT')),
  oi        INTEGER NOT NULL,
  oi_change INTEGER,
  volume    INTEGER,
  PRIMARY KEY (date, symbol, expiry, strike, opt_type)
);

CREATE TABLE IF NOT EXISTS fno_ban_list (
  date   TEXT NOT NULL,
  symbol TEXT NOT NULL,
  PRIMARY KEY (date, symbol)
);

-- Deals & flows
CREATE TABLE IF NOT EXISTS bulk_block_deals (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  date      TEXT    NOT NULL,
  symbol    TEXT    NOT NULL,
  deal_type TEXT    NOT NULL CHECK (deal_type IN ('BULK','BLOCK')),
  qty       INTEGER NOT NULL,
  price     REAL    NOT NULL,
  client    TEXT,
  side      TEXT             CHECK (side IS NULL OR side IN ('BUY','SELL'))
);
CREATE INDEX IF NOT EXISTS idx_bulk_block_symbol_date ON bulk_block_deals(symbol, date);

CREATE TABLE IF NOT EXISTS corp_actions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ex_date         TEXT    NOT NULL,
  symbol          TEXT    NOT NULL,
  action_type     TEXT    NOT NULL,
  ratio_or_amount TEXT
);
CREATE INDEX IF NOT EXISTS idx_corp_actions_symbol ON corp_actions(symbol, ex_date);

-- Account & live state
CREATE TABLE IF NOT EXISTS account_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT    NOT NULL,
  event_type   TEXT    NOT NULL,
  symbol       TEXT,
  details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_account_events_ts ON account_events(ts);

CREATE TABLE IF NOT EXISTS preopen_snapshot (
  date            TEXT NOT NULL,
  symbol          TEXT NOT NULL,
  iep             REAL NOT NULL,
  prev_close      REAL NOT NULL,
  gap_pct         REAL NOT NULL,
  preopen_volume  INTEGER,
  PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS live_quotes (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT    NOT NULL,
  symbol         TEXT    NOT NULL,
  ltp            REAL    NOT NULL,
  bid            REAL,
  ask            REAL,
  bid_qty        INTEGER,
  ask_qty        INTEGER,
  volume         INTEGER,
  oi             INTEGER,
  circuit_upper  REAL,
  circuit_lower  REAL
);
CREATE INDEX IF NOT EXISTS idx_live_quotes_symbol_ts ON live_quotes(symbol, ts);

CREATE TABLE IF NOT EXISTS event_calendar (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_date   TEXT    NOT NULL,
  event_type   TEXT    NOT NULL,
  description  TEXT    NOT NULL,
  impact_level TEXT             CHECK (impact_level IS NULL OR impact_level IN ('HIGH','MEDIUM','LOW')),
  country      TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_calendar_date ON event_calendar(event_date);
```

---

## Task 1 — `config.py`

`src/trading/config.py` exposes:
- `TIMEZONE = "Asia/Kolkata"` (constant)
- `@dataclass(frozen=True) class Paths` — project_root, data_dir, parquet_dir, cache_dir, logs_dir, research_dir, raw_dir, models_dir, db_path
- `@dataclass(frozen=True) class Settings` — `anthropic_api_key: str | None`, `kite_api_key: str | None`, `kite_api_secret: str | None`, `kite_access_token: str | None`, `log_level: str`, `news_user_agent: str`
- `get_paths(root: Path | None = None) -> Paths` — derives paths from project root (auto-detect from `__file__` if not given)
- `get_settings() -> Settings` — loads `.env`, reads env vars, returns frozen settings

Tests: `tests/test_config.py` covers:
- Default project_root is the repo root (parent of `src/`)
- `db_path` defaults to `<root>/data/app.db`
- `get_settings()` reads `LOG_LEVEL` from environment (use `monkeypatch`)
- Missing optional keys default to `None`

## Task 2 — `store/db.py`

`get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]` context manager that:
- Auto-creates parent directory
- Opens connection with `detect_types=sqlite3.PARSE_DECLTYPES`
- Sets `PRAGMA foreign_keys = ON`
- Sets `PRAGMA journal_mode = WAL`
- Sets `row_factory = sqlite3.Row`
- Commits on clean exit, rolls back on exception
- Closes connection in `finally`

Tests: `tests/test_db.py`:
- `get_conn(tmp_path/'x.db')` creates the file
- FK enforcement is ON inside the conn
- WAL mode is set
- Parent directory is auto-created when it doesn't exist
- Exception inside `with` block triggers rollback

## Task 3 — `store/migrations.py`

- Module-level `SCHEMA_V1` string containing all DDL above
- `CURRENT_VERSION = 1` constant
- `run_migrations(conn: sqlite3.Connection) -> int` — applies any pending migrations, returns the version reached
- Idempotent: re-running on an already-migrated DB is a no-op

Tests: `tests/test_migrations.py`:
- Fresh DB after `run_migrations` has all 16 expected tables + `schema_version`
- `schema_version` table contains exactly one row with version=1
- Running `run_migrations` twice does not error or duplicate rows
- Each CHECK constraint enforced: e.g. inserting `signals.side='HOLD'` raises `IntegrityError`

## Task 4 — `store/repo.py`

Typed dataclasses + CRUD for the three most-used tables:

```python
@dataclass(frozen=True)
class Signal:
    id: int | None
    ts: str
    symbol: str
    side: Literal["LONG", "SHORT"]
    entry: float
    stop: float
    target: float
    horizon_days: int
    rules_passed_json: str | None = None
    ml_score: float | None = None
    conviction: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    rationale: str | None = None
    created_by: str = "auto"

@dataclass(frozen=True)
class PaperTrade:
    id: int | None
    signal_id: int
    ts_entry: str
    entry_price: float
    qty: int
    ts_exit: str | None = None
    exit_price: float | None = None
    exit_reason: Literal["TARGET", "STOP", "TIME", "MANUAL"] | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    days_held: int | None = None

@dataclass(frozen=True)
class Prediction:
    id: int | None
    ts: str
    symbol: str
    predicted_return_pct: float
    predicted_horizon_days: int
    actual_return_at_horizon: float | None = None
    error_pct: float | None = None
    evaluated_at: str | None = None
```

Functions (one per table, all take `conn: sqlite3.Connection` as first arg):
- `insert_signal(conn, signal) -> int` (returns new id)
- `get_signal(conn, signal_id) -> Signal | None`
- `list_signals_by_date(conn, date_iso) -> list[Signal]` (matches `signals.ts` LIKE 'YYYY-MM-DD%')
- `insert_paper_trade(conn, trade) -> int`
- `close_paper_trade(conn, trade_id, ts_exit, exit_price, exit_reason, pnl, pnl_pct, days_held) -> None`
- `get_paper_trade(conn, trade_id) -> PaperTrade | None`
- `list_open_paper_trades(conn) -> list[PaperTrade]` (ts_exit IS NULL)
- `insert_prediction(conn, pred) -> int`
- `get_prediction(conn, pred_id) -> Prediction | None`
- `list_predictions_by_symbol(conn, symbol) -> list[Prediction]`

Use a `_row_to_signal`, `_row_to_paper_trade`, `_row_to_prediction` set of helpers to map `sqlite3.Row` → dataclass.

Tests: `tests/test_repo.py`:
- Insert / get / list round-trip for each of the 3 dataclasses
- FK violation: inserting a paper_trade with a non-existent signal_id raises `IntegrityError`
- `list_open_paper_trades` excludes closed trades
- `list_signals_by_date` filters correctly by date prefix

## Task 5 — Verify & commit

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest -q
```

All four must exit clean. Then tick the 8 sub-tasks in `PROGRESS.md`, update "Currently working on" to Phase 2, and commit:

```
feat(store): SQLite schema v1 + typed repo

- config.py: Paths + Settings dataclasses, .env loading, IST timezone
- store/db.py: get_conn() with FK + WAL pragmas, auto-create parent
- store/migrations.py: schema v1 — all 16 tables + schema_version, idempotent
- store/repo.py: typed CRUD for signals, paper_trades, predictions

Closes Phase 1 of docs/superpowers/specs/2026-05-11-trading-system-design.md
```
