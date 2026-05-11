# Phase 2 — Historical OHLCV Ingestion Implementation Plan

**Goal:** Fetch daily OHLCV bars for the trading universe via yfinance, store as per-symbol Parquet files under `data/parquet/nifty200/`, expose a CLI command to run ingestion.

**Architecture:** A typed fetcher wraps yfinance and normalises the DataFrame shape. A parquet I/O module handles per-symbol read/write. A bootstrap universe file lists NSE symbols (Nifty 50 + the user's current holdings — ~59 tickers; can be expanded to Nifty 200 later by editing the file). The CLI command iterates the universe with a progress bar.

**Tech Stack:** `yfinance`, `pandas`, `pyarrow`, `typer`, `rich`, `requests-cache` (already in deps).

**Reference:** [docs/superpowers/specs/2026-05-11-trading-system-design.md](../specs/2026-05-11-trading-system-design.md) Section 11 (data/yfinance.py), Section 13 (parquet storage tier).

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/trading/data/yfinance.py` | `fetch_ohlcv(symbol, start, end)` returning a normalised DataFrame |
| Create | `src/trading/data/universe.py` | `load_universe()` — reads the static universe file |
| Create | `src/trading/data/cache.py` | requests-cache CachedSession factory pointed at `data/cache/http` |
| Create | `src/trading/store/ohlcv.py` | `read_ohlcv`, `write_ohlcv`, `parquet_path`, `list_symbols` |
| Create | `src/trading/cli.py` | `trading` CLI entry — `ingest-history` command |
| Create | `data/static/universe.txt` | One ticker per line — bootstrap universe |
| Create | `tests/test_yfinance.py` | Mocked fetcher tests |
| Create | `tests/test_universe.py` | Universe loader tests |
| Create | `tests/test_ohlcv_store.py` | Parquet read/write/listing tests |
| Create | `tests/test_cli.py` | CLI smoke tests via typer.testing.CliRunner |
| Modify | `PROGRESS.md` | Tick 2.1-2.7 |

`data/static/` is the new static-reference tier — small files (universe lists, sector mappings) tracked in git. Distinct from `data/parquet/` (gitignored bulk data).

---

## Module Contracts

### `data/yfinance.py`

```python
NSE_SUFFIX = ".NS"
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

class OhlcvFetchError(Exception): ...

def to_yf_symbol(symbol: str) -> str:
    """RVNL → RVNL.NS  (idempotent)."""

def fetch_ohlcv(symbol: str, start: date | str, end: date | str,
                *, auto_adjust: bool = True) -> pd.DataFrame:
    """Fetch daily OHLCV; returns DataFrame indexed by tz-naive date,
    columns: open, high, low, close, volume (lowercase)."""
```

Normalisation steps inside `fetch_ohlcv`:
- Append `.NS` if missing
- Flatten multi-level columns (yfinance returns `('Close', 'RVNL.NS')` style)
- Lowercase column names
- Drop columns outside `REQUIRED_COLUMNS`
- Strip timezone from index, name it `"date"`
- Raise `OhlcvFetchError` on empty result

### `data/universe.py`

```python
DEFAULT_UNIVERSE_PATH = Paths.project_root / "data" / "static" / "universe.txt"

def load_universe(path: Path | None = None) -> list[str]:
    """Return tickers from one-per-line file. Strips, dedupes, skips blanks
    and # comments."""
```

### `data/cache.py`

```python
def get_cached_session(paths: Paths | None = None, expire_after: int = 3600) -> CachedSession:
    """SQLite-backed requests cache at data/cache/http.sqlite, default 1h TTL."""
```

### `store/ohlcv.py`

```python
def parquet_path(symbol: str, paths: Paths) -> Path:
    """data/parquet/nifty200/SYMBOL.parquet  (NSE_SUFFIX stripped)."""

def write_ohlcv(df: pd.DataFrame, symbol: str, paths: Paths) -> Path:
    """Validate schema, write parquet (compression=snappy)."""

def read_ohlcv(symbol: str, paths: Paths, *,
               start: date | str | None = None,
               end: date | str | None = None) -> pd.DataFrame:
    """Read parquet, optionally filter by date range."""

def list_symbols(paths: Paths) -> list[str]:
    """Sorted list of symbols with a parquet file on disk."""
```

### `cli.py`

```python
app = typer.Typer(help="Trading — research and paper-trading CLI.")

@app.command("ingest-history")
def ingest_history(
    start: str = "2023-01-01",
    end: str | None = None,
    symbols: list[str] | None = None,
    skip_existing: bool = False,
) -> None:
    """Fetch OHLCV for the universe (or --symbols) and write to parquet."""
```

CLI behaviour:
- If `symbols` is empty → load `data/static/universe.txt`
- If `end` is None → `date.today()`
- Show a Rich progress bar
- On per-ticker failure, log and continue (do not abort the whole run)
- Print summary at end: N succeeded, N failed, total bars

---

## Tasks

### Task 1 — Tests-first

Write `tests/test_yfinance.py`, `tests/test_universe.py`, `tests/test_ohlcv_store.py`, `tests/test_cli.py`. They will fail because the source modules don't exist yet.

Then implement source modules until tests pass.

### Task 2 — Bootstrap universe file

Create `data/static/universe.txt` with Nifty 50 + the 11 user holdings (deduped). Comment header explaining how to expand it to Nifty 200.

### Task 3 — Implement modules

`yfinance.py` → `universe.py` → `cache.py` → `ohlcv.py` → `cli.py`. Each as small as possible.

### Task 4 — Lint + type + tests

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest -q
```

All exit clean.

### Task 5 — Smoke test

Run on three symbols:

```powershell
uv run trading ingest-history --symbols RVNL --symbols RELIANCE --symbols NTPC --start 2025-01-01
```

Verify three files appear under `data/parquet/nifty200/` and `read_ohlcv("RELIANCE", paths).shape` is sensible.

### Task 6 — Commit

```
feat(data): historical OHLCV ingestion (Phase 2)

- data/yfinance.py: fetch_ohlcv with NSE-suffix handling + shape normalisation
- data/universe.py: load_universe from data/static/universe.txt
- data/cache.py: SQLite-backed requests cache at data/cache/http.sqlite
- store/ohlcv.py: per-symbol parquet read/write/list
- cli.py: typer app with `trading ingest-history` command
- data/static/universe.txt: Nifty 50 + user's 11 holdings (~59 symbols)

Closes Phase 2 of docs/superpowers/specs/2026-05-11-trading-system-design.md
```
