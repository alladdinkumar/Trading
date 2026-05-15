"""SQLite schema v1 — all 16 tables from spec Section 13, idempotent bootstrap.

The current implementation has a single migration (v1). Future schema changes
add a new constant `SCHEMA_V2` (etc.) and a branch in `run_migrations` so the
upgrade path stays explicit and replayable.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

CURRENT_VERSION = 2


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT    NOT NULL
);

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
"""


# v2: persist trailing-stop state on paper_trades so MTM can ratchet
# across daily runs without re-deriving from history. `current_stop`
# initialises from signals.stop at trade open; `atr_at_entry` is fixed
# (used by strategy.exits._trail_new_stop's ATR-based trail). Both
# nullable so legacy rows can be skipped (or migrated) gracefully.
SCHEMA_V2 = """
ALTER TABLE paper_trades ADD COLUMN current_stop  REAL;
ALTER TABLE paper_trades ADD COLUMN atr_at_entry  REAL;
"""


def _current_db_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied version, or 0 if schema_version doesn't exist yet."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row is not None else 0


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Idempotent — re-running on an up-to-date DB is a no-op.

    Returns the version the DB is at after running.
    """
    current = _current_db_version(conn)
    if current >= CURRENT_VERSION:
        return current
    if current < 1:
        conn.executescript(SCHEMA_V1)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (1, datetime.now(UTC).isoformat()),
        )
    if current < 2:
        conn.executescript(SCHEMA_V2)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (2, datetime.now(UTC).isoformat()),
        )
    return CURRENT_VERSION
