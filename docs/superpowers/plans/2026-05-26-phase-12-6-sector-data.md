# Phase 12.6 — Sector data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily NSE sectoral-index snapshot (`sector_daily`) with relative strength vs Nifty 50, and wire it into the pre-open pipeline, the IEP gap-filter, and the LLM analyst bundle.

**Architecture:** A new `data/sector.py` mirrors `data/macro.py` (yfinance fetcher + per-source isolation + RS computation). A new `store/sector_store.py` mirrors `store/macro_store.py` (upsert + get). The `pre_open` orchestrator gains a `_step_sector`; `pre_open_iep` auto-loads from `sector_daily` when its injection parameters are `None`; `assemble_context` renders a `## Sector momentum` section and tags each candidate with its sector RS. A static `data/static/sector_map.csv` is the source of truth for symbol → sector.

**Tech Stack:** Python 3.11 · yfinance · pandas · sqlite3 · typer · Rich · pytest · ruff · mypy

**Spec:** [docs/superpowers/specs/2026-05-26-phase-12-6-sector-data-design.md](../specs/2026-05-26-phase-12-6-sector-data-design.md)

---

## File map

**New files:**
- `src/trading/data/sector.py` — sector fetcher + RS + regime + sector-map loader
- `src/trading/store/sector_store.py` — upsert + get for `sector_daily`
- `data/static/sector_map.csv` — symbol → sector index map
- `tests/test_data_sector.py`
- `tests/test_store_sector.py`

**Modified files:**
- `src/trading/jobs/pre_open.py` — new `_step_sector`; `PreOpenResult.sector_written`
- `src/trading/jobs/pre_open_iep.py` — auto-load `sector_map` + `sector_momentum` from DB
- `src/trading/llm/context.py` — new `_render_sector_snapshot`; per-candidate sector bullet
- `src/trading/llm/briefing.py` — reword `SECTOR_COMMENTARY_PLACEHOLDER`
- `src/trading/cli.py` — new `sector` command; `pre-open` table gains `sector_written` row
- `.claude/skills/analyst/SKILL.md` — drop "optional while unwired" caveat
- `PROGRESS.md` — flip Phase 12.6 to done
- `tests/test_jobs_pre_open.py` — `_step_sector` happy + failure tests
- `tests/test_jobs_pre_open_iep.py` — auto-load tests
- `tests/test_llm_context.py` — sector section tests; re-record snapshots
- `tests/test_llm_briefing.py` — re-record snapshots if needed
- `tests/test_cli.py` — `trading sector` happy path
- `tests/__snapshots__/test_llm_context.ambr` — delete obsolete entries, re-record

---

## Task 1: Sector fetcher pure functions (data/sector.py — RS, regime, sector map)

**Files:**
- Create: `src/trading/data/sector.py`
- Test: `tests/test_data_sector.py`

This task builds the pure (no-IO) parts only: `compute_rs`, `_regime_for`, and `load_sector_map`. The yfinance-dependent `fetch_sector_history` / `fetch_all_sectors` come in Task 2.

- [ ] **Step 1: Write failing tests for compute_rs and _regime_for**

Create `tests/test_data_sector.py`:

```python
"""Tests for trading.data.sector — RS computation + regime labels + map loader."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.data.sector import (
    LAGGING_THRESHOLD,
    LEADING_THRESHOLD,
    _regime_for,
    compute_rs,
    load_sector_map,
)


def _series(values: list[float]) -> pd.Series:
    """Build a date-indexed close series, oldest first."""
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, name="close")


def test_compute_rs_simple_difference() -> None:
    # Sector +5% over the window, benchmark +2% → RS = +0.03
    sector = _series([100.0] * 5 + [105.0])
    bench = _series([100.0] * 5 + [102.0])
    rs = compute_rs(sector, bench, window=5)
    assert rs is not None
    assert abs(rs - 0.03) < 1e-9


def test_compute_rs_returns_none_when_history_too_short() -> None:
    sector = _series([100.0, 101.0, 102.0])  # 3 bars, asking for window=5
    bench = _series([100.0, 100.0, 100.0])
    assert compute_rs(sector, bench, window=5) is None


def test_compute_rs_returns_none_when_lookback_close_is_zero() -> None:
    sector = _series([0.0, 100.0, 100.0, 100.0, 100.0, 105.0])
    bench = _series([100.0, 100.0, 100.0, 100.0, 100.0, 102.0])
    assert compute_rs(sector, bench, window=5) is None


def test_regime_for_leading_when_above_threshold() -> None:
    assert _regime_for(LEADING_THRESHOLD + 0.001) == "LEADING"


def test_regime_for_lagging_when_below_threshold() -> None:
    assert _regime_for(LAGGING_THRESHOLD - 0.001) == "LAGGING"


def test_regime_for_neutral_on_boundary() -> None:
    # Strictly inside the (lagging, leading) band → NEUTRAL.
    assert _regime_for(LEADING_THRESHOLD) == "NEUTRAL"
    assert _regime_for(LAGGING_THRESHOLD) == "NEUTRAL"
    assert _regime_for(0.0) == "NEUTRAL"


def test_regime_for_none_when_rs_none() -> None:
    assert _regime_for(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_data_sector.py -v`
Expected: ImportError or ModuleNotFoundError on `trading.data.sector`.

- [ ] **Step 3: Create `src/trading/data/sector.py` with the three pure helpers**

```python
"""NSE sectoral indices — relative-strength snapshot.

Mirrors data/macro.py: a defensive yfinance wrapper per source so a
single failing ticker doesn't abort the whole snapshot. RS is the simple
difference between sector and benchmark returns over a window. Per-row
`regime` ('LEADING' / 'NEUTRAL' / 'LAGGING') is derived from rs_20d.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from trading.config import Paths, get_paths

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 11 NSE sectoral indices we track. Keys are the codes that live in
# sector_daily.sector + data/static/sector_map.csv.
SECTOR_TICKERS: dict[str, str] = {
    "NIFTYBANK": "^NSEBANK",
    "IT": "^CNXIT",
    "AUTO": "^CNXAUTO",
    "FMCG": "^CNXFMCG",
    "PHARMA": "^CNXPHARMA",
    "METAL": "^CNXMETAL",
    "ENERGY": "^CNXENERGY",
    "REALTY": "^CNXREALTY",
    "PSUBANK": "^CNXPSUBANK",
    "FINSERV": "^CNXFIN",
    "INFRA": "^CNXINFRA",
}

BENCHMARK_TICKER = "^NSEI"  # Nifty 50
RS_WINDOWS: tuple[int, int, int] = (5, 20, 60)
LEADING_THRESHOLD = 0.02
LAGGING_THRESHOLD = -0.02


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectorRow:
    """One row of `sector_daily`."""

    date: str  # YYYY-MM-DD
    sector: str
    close: float
    rs_5d: float | None
    rs_20d: float | None
    rs_60d: float | None
    regime: str | None  # 'LEADING' | 'NEUTRAL' | 'LAGGING' | None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def compute_rs(
    sector_closes: pd.Series, benchmark_closes: pd.Series, *, window: int
) -> float | None:
    """Simple-difference RS = sector_return_N - benchmark_return_N.

    Returns None if either series has fewer than window+1 bars or the
    lookback close is zero (would divide by zero).
    """
    if len(sector_closes) < window + 1 or len(benchmark_closes) < window + 1:
        return None
    s_now, s_back = float(sector_closes.iloc[-1]), float(sector_closes.iloc[-(window + 1)])
    b_now, b_back = float(benchmark_closes.iloc[-1]), float(benchmark_closes.iloc[-(window + 1)])
    if s_back == 0 or b_back == 0:
        return None
    sector_ret = (s_now / s_back) - 1.0
    bench_ret = (b_now / b_back) - 1.0
    return sector_ret - bench_ret


def _regime_for(rs_20d: float | None) -> str | None:
    """Apply leading/lagging thresholds. Strictly outside the band → label."""
    if rs_20d is None:
        return None
    if rs_20d > LEADING_THRESHOLD:
        return "LEADING"
    if rs_20d < LAGGING_THRESHOLD:
        return "LAGGING"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Sector map
# ---------------------------------------------------------------------------


def _default_sector_map_path(paths: Paths | None = None) -> Path:
    p = paths if paths is not None else get_paths()
    return p.project_root / "data" / "static" / "sector_map.csv"


def load_sector_map(paths: Paths | None = None) -> dict[str, str]:
    """Read `data/static/sector_map.csv` (header: symbol,sector).

    Returns `{symbol: sector_code}`. Skips blank lines and `#` comments.
    Returns `{}` if the file doesn't exist (graceful — callers degrade).
    """
    path = _default_sector_map_path(paths)
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        # Strip comment and blank lines before csv.DictReader sees them.
        cleaned: list[str] = []
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cleaned.append(line)
    if not cleaned:
        return {}
    reader = csv.DictReader(cleaned)
    for row in reader:
        sym = (row.get("symbol") or "").strip()
        sec = (row.get("sector") or "").strip()
        if sym and sec:
            out[sym] = sec
    return out
```

- [ ] **Step 4: Run pure-helper tests to verify they pass**

Run: `uv run pytest tests/test_data_sector.py::test_compute_rs_simple_difference tests/test_data_sector.py::test_compute_rs_returns_none_when_history_too_short tests/test_data_sector.py::test_compute_rs_returns_none_when_lookback_close_is_zero tests/test_data_sector.py::test_regime_for_leading_when_above_threshold tests/test_data_sector.py::test_regime_for_lagging_when_below_threshold tests/test_data_sector.py::test_regime_for_neutral_on_boundary tests/test_data_sector.py::test_regime_for_none_when_rs_none -v`
Expected: 7 passed.

- [ ] **Step 5: Add tests for load_sector_map**

Append to `tests/test_data_sector.py`:

```python
def test_load_sector_map_reads_csv_with_comments_and_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    static_dir = tmp_path / "data" / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "sector_map.csv").write_text(
        "# Comment\n"
        "\n"
        "symbol,sector\n"
        "INFY,IT\n"
        "HDFCBANK,NIFTYBANK\n"
        "# another comment\n",
        encoding="utf-8",
    )
    paths = get_paths()
    assert load_sector_map(paths) == {"INFY": "IT", "HDFCBANK": "NIFTYBANK"}


def test_load_sector_map_returns_empty_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    assert load_sector_map(paths) == {}
```

- [ ] **Step 6: Run all data_sector tests to verify**

Run: `uv run pytest tests/test_data_sector.py -v`
Expected: 9 passed.

- [ ] **Step 7: Commit**

```bash
git add src/trading/data/sector.py tests/test_data_sector.py
git commit -m "feat(data): sector RS pure helpers + map loader (Phase 12.6.1)"
```

---

## Task 2: Sector fetcher yfinance wrappers (data/sector.py — fetch + orchestrator)

**Files:**
- Modify: `src/trading/data/sector.py` (append fetcher functions)
- Test: `tests/test_data_sector.py` (append)

- [ ] **Step 1: Write failing test for fetch_sector_history error path**

Append to `tests/test_data_sector.py`:

```python
from unittest.mock import patch


def test_fetch_sector_history_returns_none_on_yfinance_error() -> None:
    from trading.data.sector import fetch_sector_history

    with patch("trading.data.sector.yf.download", side_effect=RuntimeError("boom")):
        result = fetch_sector_history("^NSEBANK", lookback_days=90)
    assert result is None


def test_fetch_sector_history_returns_none_on_empty_frame() -> None:
    from trading.data.sector import fetch_sector_history

    with patch("trading.data.sector.yf.download", return_value=pd.DataFrame()):
        result = fetch_sector_history("^NSEBANK", lookback_days=90)
    assert result is None


def test_fetch_all_sectors_skips_failed_tickers_and_returns_rows() -> None:
    """One sector + benchmark succeed; another sector fails. Result has 1 row."""
    from trading.data.sector import fetch_all_sectors

    bench_df = pd.DataFrame(
        {"Close": [100.0] * 21 + [102.0]},
        index=pd.date_range("2026-01-01", periods=22, freq="B"),
    )
    sector_df = pd.DataFrame(
        {"Close": [100.0] * 21 + [105.0]},
        index=pd.date_range("2026-01-01", periods=22, freq="B"),
    )

    def fake_download(ticker: str, **kwargs: object) -> pd.DataFrame:
        if ticker == "^NSEI":
            return bench_df.copy()
        if ticker == "^NSEBANK":
            return sector_df.copy()
        raise RuntimeError("ticker failed")

    with patch("trading.data.sector.yf.download", side_effect=fake_download):
        rows = fetch_all_sectors(date(2026, 2, 1))

    # Only NIFTYBANK succeeded among the 11 sectors.
    assert len(rows) == 1
    r = rows[0]
    assert r.sector == "NIFTYBANK"
    assert r.date == "2026-02-01"
    assert r.close == 105.0
    # rs_20d ≈ 0.03 (sector +5%, bench +2%); window=20 hits LEADING.
    assert r.rs_20d is not None and r.rs_20d > 0.02
    assert r.regime == "LEADING"
    # rs_5d / rs_60d both share the same 22-bar synthetic history;
    # rs_5d uses last 6 bars (all flat sector + flat bench until last) so
    # the result follows from the close jump on the final bar:
    # sector return over 5d = 5%, bench return = 2% → rs_5d = +0.03
    assert r.rs_5d is not None and abs(r.rs_5d - 0.03) < 1e-9
    # rs_60d needs 61 bars, we have 22 → None
    assert r.rs_60d is None
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/test_data_sector.py::test_fetch_sector_history_returns_none_on_yfinance_error tests/test_data_sector.py::test_fetch_sector_history_returns_none_on_empty_frame tests/test_data_sector.py::test_fetch_all_sectors_skips_failed_tickers_and_returns_rows -v`
Expected: 3 failures (ImportError on `fetch_sector_history` and `fetch_all_sectors`).

- [ ] **Step 3: Append fetch_sector_history + fetch_all_sectors to `src/trading/data/sector.py`**

Add the following sections at the bottom of `src/trading/data/sector.py`:

```python
# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_sector_history(ticker: str, *, lookback_days: int = 90) -> pd.DataFrame | None:
    """Pull a daily-close history for `ticker`. Returns None on any failure.

    Same defensive pattern as data.macro.fetch_yf_quote: HTTP error,
    rate-limit, deprecated symbol → None so the surrounding snapshot keeps
    going. Returned frame is single-level columns with a `close` column,
    indexed by trading date.
    """
    try:
        raw: Any = yf.download(
            ticker,
            period=f"{lookback_days}d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            actions=False,
        )
    except Exception:
        return None
    if raw is None or getattr(raw, "empty", True):
        return None
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    if "Close" not in raw.columns or len(raw) == 0:
        return None
    df = raw[["Close"]].rename(columns={"Close": "close"}).dropna()
    if df.empty:
        return None
    return df


def fetch_all_sectors(as_of: date) -> list[SectorRow]:
    """Pull benchmark + every sector ticker; build SectorRow per success.

    Per-sector fetch failures yield no row (so the count reflects success).
    Benchmark failure returns an empty list — without it, RS is undefined.
    All rows are tagged with `as_of.isoformat()` regardless of the actual
    last-bar date (yfinance may lag a day after market close).
    """
    bench = fetch_sector_history(BENCHMARK_TICKER)
    if bench is None or bench.empty:
        return []
    bench_closes = bench["close"]
    rows: list[SectorRow] = []
    for sector_code, ticker in SECTOR_TICKERS.items():
        history = fetch_sector_history(ticker)
        if history is None or history.empty:
            continue
        closes = history["close"]
        last_close = float(closes.iloc[-1])
        rs_values: dict[int, float | None] = {
            w: compute_rs(closes, bench_closes, window=w) for w in RS_WINDOWS
        }
        rows.append(
            SectorRow(
                date=as_of.isoformat(),
                sector=sector_code,
                close=last_close,
                rs_5d=rs_values[5],
                rs_20d=rs_values[20],
                rs_60d=rs_values[60],
                regime=_regime_for(rs_values[20]),
            )
        )
    return rows
```

- [ ] **Step 4: Run all data_sector tests**

Run: `uv run pytest tests/test_data_sector.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading/data/sector.py tests/test_data_sector.py
git commit -m "feat(data): sector yfinance fetcher + snapshot orchestrator (Phase 12.6.1)"
```

---

## Task 3: Sector persistence (store/sector_store.py)

**Files:**
- Create: `src/trading/store/sector_store.py`
- Test: `tests/test_store_sector.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_store_sector.py`:

```python
"""Tests for trading.store.sector_store."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from trading.data.sector import SectorRow
from trading.store.migrations import run_migrations
from trading.store.sector_store import get_sector_daily, upsert_sector_daily


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _row(sector: str, *, rs_20d: float | None = 0.01, regime: str | None = "NEUTRAL") -> SectorRow:
    return SectorRow(
        date="2026-05-26",
        sector=sector,
        close=42000.0,
        rs_5d=0.005,
        rs_20d=rs_20d,
        rs_60d=-0.01,
        regime=regime,
    )


def test_upsert_then_get_round_trip(conn: sqlite3.Connection) -> None:
    rows = [_row("IT"), _row("NIFTYBANK", rs_20d=0.035, regime="LEADING")]
    n = upsert_sector_daily(conn, rows)
    assert n == 2
    fetched = get_sector_daily(conn, date(2026, 5, 26))
    assert len(fetched) == 2
    by_sector = {r.sector: r for r in fetched}
    assert by_sector["IT"].rs_5d == 0.005
    assert by_sector["NIFTYBANK"].regime == "LEADING"


def test_upsert_overwrites_on_conflict(conn: sqlite3.Connection) -> None:
    upsert_sector_daily(conn, [_row("IT", rs_20d=0.0, regime="NEUTRAL")])
    upsert_sector_daily(conn, [_row("IT", rs_20d=0.05, regime="LEADING")])
    rows = get_sector_daily(conn, date(2026, 5, 26))
    assert len(rows) == 1
    assert rows[0].rs_20d == 0.05
    assert rows[0].regime == "LEADING"


def test_get_sector_daily_returns_empty_when_no_rows(conn: sqlite3.Connection) -> None:
    assert get_sector_daily(conn, date(2026, 5, 26)) == []
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/test_store_sector.py -v`
Expected: ImportError on `trading.store.sector_store`.

- [ ] **Step 3: Create `src/trading/store/sector_store.py`**

```python
"""Persistence helpers for `sector_daily` (one row per (date, sector))."""

from __future__ import annotations

import sqlite3
from datetime import date

from trading.data.sector import SectorRow


def upsert_sector_daily(conn: sqlite3.Connection, rows: list[SectorRow]) -> int:
    """INSERT ON CONFLICT(date, sector) DO UPDATE per row. Returns count written."""
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO sector_daily (date, sector, close, rs_5d, rs_20d, rs_60d, regime)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, sector) DO UPDATE SET
          close  = excluded.close,
          rs_5d  = excluded.rs_5d,
          rs_20d = excluded.rs_20d,
          rs_60d = excluded.rs_60d,
          regime = excluded.regime
        """,
        [
            (r.date, r.sector, r.close, r.rs_5d, r.rs_20d, r.rs_60d, r.regime)
            for r in rows
        ],
    )
    return len(rows)


def get_sector_daily(conn: sqlite3.Connection, as_of: date) -> list[SectorRow]:
    """All sector_daily rows for one date, ordered by sector. [] if none."""
    cursor = conn.execute(
        "SELECT date, sector, close, rs_5d, rs_20d, rs_60d, regime "
        "FROM sector_daily WHERE date = ? ORDER BY sector",
        (as_of.isoformat(),),
    )
    return [
        SectorRow(
            date=row["date"],
            sector=row["sector"],
            close=row["close"],
            rs_5d=row["rs_5d"],
            rs_20d=row["rs_20d"],
            rs_60d=row["rs_60d"],
            regime=row["regime"],
        )
        for row in cursor.fetchall()
    ]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_store_sector.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading/store/sector_store.py tests/test_store_sector.py
git commit -m "feat(store): sector_daily upsert + read (Phase 12.6.2)"
```

---

## Task 4: Sector map CSV file

**Files:**
- Create: `data/static/sector_map.csv`

- [ ] **Step 1: Write the map file**

Create `data/static/sector_map.csv` with the exact content below:

```
# Symbol -> NSE sectoral index map. One row per symbol.
# Sector codes must match src/trading/data/sector.py::SECTOR_TICKERS keys.
# Symbols not listed here are treated as "no sector" by pre_open_iep
# (no sector bonus, no sector veto).
symbol,sector
HDFCBANK,NIFTYBANK
ICICIBANK,NIFTYBANK
AXISBANK,NIFTYBANK
KOTAKBANK,NIFTYBANK
INDUSINDBK,NIFTYBANK
IDFCFIRSTB,NIFTYBANK
SBIN,PSUBANK
INFY,IT
TCS,IT
HCLTECH,IT
WIPRO,IT
TECHM,IT
LTIM,IT
M&M,AUTO
MARUTI,AUTO
EICHERMOT,AUTO
TATAMOTORS,AUTO
BAJAJ-AUTO,AUTO
HEROMOTOCO,AUTO
ITC,FMCG
NESTLEIND,FMCG
BRITANNIA,FMCG
HINDUNILVR,FMCG
TATACONSUM,FMCG
DRREDDY,PHARMA
SUNPHARMA,PHARMA
CIPLA,PHARMA
APOLLOHOSP,PHARMA
TATASTEEL,METAL
JSWSTEEL,METAL
HINDALCO,METAL
COALINDIA,METAL
NTPC,ENERGY
POWERGRID,ENERGY
TATAPOWER,ENERGY
ONGC,ENERGY
BPCL,ENERGY
RELIANCE,ENERGY
BAJFINANCE,FINSERV
BAJAJFINSV,FINSERV
SHRIRAMFIN,FINSERV
HDFCLIFE,FINSERV
SBILIFE,FINSERV
PFC,FINSERV
RECLTD,FINSERV
JIOFIN,FINSERV
IREDA,FINSERV
LT,INFRA
ULTRACEMCO,INFRA
GRASIM,INFRA
ASIANPAINT,INFRA
ADANIENT,INFRA
ADANIPORTS,INFRA
RVNL,INFRA
IRB,INFRA
BEL,INFRA
MAZDOCK,INFRA
```

- [ ] **Step 2: Smoke-load the file via load_sector_map**

Run: `uv run python -c "from trading.data.sector import load_sector_map; m = load_sector_map(); print(len(m)); print(m['INFY'], m['HDFCBANK'], m['RELIANCE'])"`

Expected output (one number per line):
```
57
IT NIFTYBANK ENERGY
```

(The number `57` is the count of mapped symbols — adjust the expected value in the assertion only if you intentionally edited the file.)

- [ ] **Step 3: Commit**

```bash
git add data/static/sector_map.csv
git commit -m "feat(data): static symbol->sector map (Phase 12.6.3)"
```

---

## Task 5: Wire _step_sector into pre_open job

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Test: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write failing test for _step_sector happy path**

Append to `tests/test_jobs_pre_open.py`:

```python
from trading.data.sector import SectorRow


def test_step_sector_writes_rows_and_returns_true(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading.jobs.pre_open import _step_sector

    rows = [
        SectorRow(
            date="2026-05-26", sector="IT", close=36000.0,
            rs_5d=0.012, rs_20d=0.035, rs_60d=0.02, regime="LEADING",
        ),
        SectorRow(
            date="2026-05-26", sector="METAL", close=9000.0,
            rs_5d=-0.01, rs_20d=-0.03, rs_60d=-0.04, regime="LAGGING",
        ),
    ]
    monkeypatch.setattr("trading.jobs.pre_open.fetch_all_sectors", lambda _as_of: rows)
    warnings: list[str] = []
    ok = _step_sector(conn, date(2026, 5, 26), warnings)
    assert ok is True
    assert warnings == []
    fetched = conn.execute(
        "SELECT sector, regime FROM sector_daily WHERE date = ? ORDER BY sector",
        ("2026-05-26",),
    ).fetchall()
    assert [r["sector"] for r in fetched] == ["IT", "METAL"]
    assert [r["regime"] for r in fetched] == ["LEADING", "LAGGING"]


def test_step_sector_degrades_on_fetch_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading.jobs.pre_open import _step_sector

    def boom(_as_of: date) -> list:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr("trading.jobs.pre_open.fetch_all_sectors", boom)
    warnings: list[str] = []
    ok = _step_sector(conn, date(2026, 5, 26), warnings)
    assert ok is False
    assert any("sector snapshot failed" in w for w in warnings)
    # Nothing written.
    fetched = conn.execute("SELECT COUNT(*) AS n FROM sector_daily").fetchone()
    assert fetched["n"] == 0


def test_step_sector_returns_false_when_no_rows(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Benchmark fetch failure → fetch_all_sectors returns []. We treat as no-data."""
    from trading.jobs.pre_open import _step_sector

    monkeypatch.setattr("trading.jobs.pre_open.fetch_all_sectors", lambda _as_of: [])
    warnings: list[str] = []
    ok = _step_sector(conn, date(2026, 5, 26), warnings)
    assert ok is False
    assert any("no sector rows fetched" in w for w in warnings)
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/test_jobs_pre_open.py::test_step_sector_writes_rows_and_returns_true tests/test_jobs_pre_open.py::test_step_sector_degrades_on_fetch_failure tests/test_jobs_pre_open.py::test_step_sector_returns_false_when_no_rows -v`
Expected: ImportError on `_step_sector`.

- [ ] **Step 3: Add `_step_sector` + import in `src/trading/jobs/pre_open.py`**

Edit `src/trading/jobs/pre_open.py`:

Add to the imports block (next to `from trading.data.macro import …`):
```python
from trading.data.sector import fetch_all_sectors
```
Add (next to `from trading.store.macro_store import upsert_macro_snapshot`):
```python
from trading.store.sector_store import upsert_sector_daily
```

After `_step_macro` (around line 180, the function definition), add:

```python
def _step_sector(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> bool:
    """Pull NSE sectoral indices, compute RS vs Nifty 50, upsert sector_daily.

    Graceful: any error (yfinance down, benchmark missing) yields a warning
    and returns False so the wider pre-open continues.
    """
    try:
        rows = fetch_all_sectors(as_of)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"sector snapshot failed: {e!s}")
        return False
    if not rows:
        warnings.append("no sector rows fetched (benchmark or all sectors failed)")
        return False
    upsert_sector_daily(conn, rows)
    return True
```

- [ ] **Step 4: Add `sector_written` field to `PreOpenResult` and call `_step_sector` in `run_pre_open`**

In `src/trading/jobs/pre_open.py`, modify the `PreOpenResult` dataclass to add the new field (insert after `macro_written`):

```python
@dataclass(frozen=True)
class PreOpenResult:
    """What pre_open produced. Returned by `run_pre_open` for tests + CLI."""

    as_of: date
    bundle_path: Path
    macro_written: bool
    sector_written: bool
    news_inserted: int
    sentiment_rows: int
    candidates_total: int
    candidates_passing: int
    candidates_selected: int
    paper_trades_opened: int
    holdings_scored: int
    warnings: list[str] = field(default_factory=list)
```

In `run_pre_open`, after the macro step (`macro_written, regime = _step_macro(...)`), add the sector step:

```python
        macro_written, regime = _step_macro(conn, as_of, warnings)
        sector_written = _step_sector(conn, as_of, warnings)
```

In the `PreOpenResult(...)` return, add `sector_written=sector_written` between `macro_written` and `news_inserted`:

```python
    return PreOpenResult(
        as_of=as_of,
        bundle_path=bundle_path,
        macro_written=macro_written,
        sector_written=sector_written,
        news_inserted=news_inserted,
        ...
    )
```

- [ ] **Step 5: Update existing tests that construct PreOpenResult or assert on it**

Search-and-fix any test that constructs `PreOpenResult(...)` directly:

Run: `uv run grep -rn "PreOpenResult(" tests/ src/`

For each call site in tests, add `sector_written=False` (or `True` if appropriate). For `cli.py`'s pre-open command, you'll handle the new row in Task 9 — leave it alone for now.

- [ ] **Step 6: Run the new tests + the full pre_open test file**

Run: `uv run pytest tests/test_jobs_pre_open.py -v`
Expected: all tests pass (including new sector tests + any existing tests that needed the field added).

- [ ] **Step 7: Commit**

```bash
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): pre_open _step_sector + sector_written result field (Phase 12.6.4)"
```

---

## Task 6: Wire auto-load into pre_open_iep

**Files:**
- Modify: `src/trading/jobs/pre_open_iep.py`
- Test: `tests/test_jobs_pre_open_iep.py`

The current `run_pre_open_iep` has Optional `sector_map` / `sector_momentum` parameters with no runtime source. After this task, `None` means "auto-load from DB + CSV"; `{}` means "explicitly suppress".

- [ ] **Step 1: Write failing tests for auto-load**

Append to `tests/test_jobs_pre_open_iep.py`:

```python
from trading.data.sector import SectorRow
from trading.store.sector_store import upsert_sector_daily


def _seed_context(paths, as_of, candidates: list[str]) -> None:
    """Write a minimal _context.md with the candidate symbols."""
    p = paths.research_dir / as_of.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Trading context bundle — {as_of.isoformat()}  (mode: pre_open)",
        "",
        "## Today's candidates",
        "",
    ]
    for sym in candidates:
        lines.append(f"### {sym} — passes 10/10 rules")
        lines.append("- close 100.00, RSI 60.0, ATR(14) 1.50")
        lines.append("- SMA20 99.00 · SMA50 95.00 · SMA200 90.00")
        lines.append("")
    (p / "_context.md").write_text("\n".join(lines), encoding="utf-8")


def _write_sector_map(paths, mapping: dict[str, str]) -> None:
    static_dir = paths.project_root / "data" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    body = "symbol,sector\n" + "\n".join(f"{s},{c}" for s, c in mapping.items())
    (static_dir / "sector_map.csv").write_text(body, encoding="utf-8")


def test_pre_open_iep_autoloads_sector_map_and_momentum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.config import get_paths
    from trading.jobs.pre_open_iep import run_pre_open_iep
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations

    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_context(paths, as_of, ["INFY", "TATASTEEL"])
    _write_sector_map(paths, {"INFY": "IT", "TATASTEEL": "METAL"})

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(
            conn,
            [
                SectorRow(date="2026-05-26", sector="IT", close=36000.0,
                          rs_5d=0.02, rs_20d=0.035, rs_60d=0.01, regime="LEADING"),
                SectorRow(date="2026-05-26", sector="METAL", close=9000.0,
                          rs_5d=-0.01, rs_20d=-0.03, rs_60d=-0.02, regime="LAGGING"),
            ],
        )

    result = run_pre_open_iep(as_of)
    # With NEUTRAL regime (no macro snapshot seeded) the sector filter is a
    # no-op but the rerank uses sector percentiles.
    assert result.candidates_input == 2
    assert result.candidates_filtered == 2
    # Warnings should NOT include the "sector data unavailable" message.
    assert not any("Sector data unavailable" in w for w in result.warnings)


def test_pre_open_iep_falls_back_to_d_minus_1_sector_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.config import get_paths
    from trading.jobs.pre_open_iep import run_pre_open_iep
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations

    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_context(paths, as_of, ["INFY"])
    _write_sector_map(paths, {"INFY": "IT"})

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(
            conn,
            [SectorRow(date="2026-05-25", sector="IT", close=36000.0,
                       rs_5d=0.02, rs_20d=0.035, rs_60d=0.01, regime="LEADING")],
        )

    result = run_pre_open_iep(as_of)
    assert any("sector data fallback" in w for w in result.warnings)


def test_pre_open_iep_warns_when_no_sector_data_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.config import get_paths
    from trading.jobs.pre_open_iep import run_pre_open_iep

    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_context(paths, as_of, ["INFY"])
    # No sector_map.csv and no sector_daily rows.

    result = run_pre_open_iep(as_of)
    assert any("Sector data unavailable" in w for w in result.warnings)


def test_pre_open_iep_explicit_empty_dicts_suppress_autoload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing sector_map={} should NOT trigger auto-load."""
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.config import get_paths
    from trading.jobs.pre_open_iep import run_pre_open_iep
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations

    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_context(paths, as_of, ["INFY"])
    _write_sector_map(paths, {"INFY": "IT"})
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(
            conn,
            [SectorRow(date="2026-05-26", sector="IT", close=36000.0,
                       rs_5d=0.02, rs_20d=0.035, rs_60d=0.01, regime="LEADING")],
        )

    result = run_pre_open_iep(as_of, sector_map={}, sector_momentum={})
    # Suppressed: filter+rerank ran without sector axis, so no "Sector data
    # unavailable" warning (caller explicitly opted out).
    assert not any("Sector data unavailable" in w for w in result.warnings)
    assert not any("sector data fallback" in w for w in result.warnings)
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/test_jobs_pre_open_iep.py::test_pre_open_iep_autoloads_sector_map_and_momentum tests/test_jobs_pre_open_iep.py::test_pre_open_iep_falls_back_to_d_minus_1_sector_data tests/test_jobs_pre_open_iep.py::test_pre_open_iep_warns_when_no_sector_data_anywhere tests/test_jobs_pre_open_iep.py::test_pre_open_iep_explicit_empty_dicts_suppress_autoload -v`
Expected: failures (auto-load not yet implemented; tests expect specific warning strings).

- [ ] **Step 3: Add auto-load helper + integrate into `run_pre_open_iep`**

Edit `src/trading/jobs/pre_open_iep.py`:

Add imports (next to existing `from trading.store.macro_store import get_macro_snapshot`):
```python
from datetime import timedelta

from trading.data.sector import load_sector_map
from trading.store.sector_store import get_sector_daily
```

Add a helper after `_load_yesterday_closes`:

```python
def _autoload_sector_inputs(
    paths: Paths,
    as_of: date,
    warnings: list[str],
) -> tuple[dict[str, str], dict[str, float]]:
    """Load sector_map.csv + sector_daily rows. D-1 fallback if today empty.

    Returns ({} , {}) when nothing is available. Appends a warning that the
    caller can match on:
      - "sector data fallback to <D-1 iso date>" when D-1 data is used.
      - "Sector data unavailable; …" when neither today nor D-1 has rows.
    """
    sector_map = load_sector_map(paths)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        rows = get_sector_daily(conn, as_of)
        fallback_used: date | None = None
        if not rows:
            d_minus_1 = as_of - timedelta(days=1)
            rows = get_sector_daily(conn, d_minus_1)
            if rows:
                fallback_used = d_minus_1
    if not rows:
        warnings.append("Sector data unavailable; filtering by gap + regime only.")
        return sector_map, {}
    if fallback_used is not None:
        warnings.append(f"sector data fallback to {fallback_used.isoformat()}")
    momentum = {r.sector: r.rs_5d for r in rows if r.rs_5d is not None}
    return sector_map, momentum
```

Replace the existing block in `run_pre_open_iep` that reads:

```python
    if sector_map and sector_momentum:
        kept, sector_removed = _filter_by_sector(kept, sector_map, sector_momentum, regime)
        removed = [*removed, *sector_removed]
        sector_percentiles = _sector_percentile(sector_momentum)
        candidate_sector_pcts = {
            sym: sector_percentiles.get(sector_map.get(sym, ""), 50.0) for sym in kept
        }
    else:
        candidate_sector_pcts = {}
        if sector_map is None and sector_momentum is None:
            warnings.append("Sector data unavailable; filtering by gap + regime only.")
```

…with:

```python
    if sector_map is None and sector_momentum is None:
        sector_map, sector_momentum = _autoload_sector_inputs(p, as_of, warnings)
    elif sector_map is None:
        sector_map = load_sector_map(p)
    elif sector_momentum is None:
        sector_momentum = {}

    if sector_map and sector_momentum:
        kept, sector_removed = _filter_by_sector(kept, sector_map, sector_momentum, regime)
        removed = [*removed, *sector_removed]
        sector_percentiles = _sector_percentile(sector_momentum)
        candidate_sector_pcts = {
            sym: sector_percentiles.get(sector_map.get(sym, ""), 50.0) for sym in kept
        }
    else:
        candidate_sector_pcts = {}
```

Behavior:
- `sector_map is None and sector_momentum is None` → auto-load. Warning emitted by helper.
- One is None, the other is dict → load the missing one or set empty dict (preserves test injection use case).
- Both are dicts (even `{}`) → callers control; no auto-load, no warning.

- [ ] **Step 4: Run the new tests + the full IEP test file**

Run: `uv run pytest tests/test_jobs_pre_open_iep.py -v`
Expected: all tests pass. (Tests that passed non-None overrides still work.)

- [ ] **Step 5: Commit**

```bash
git add src/trading/jobs/pre_open_iep.py tests/test_jobs_pre_open_iep.py
git commit -m "feat(jobs): pre_open_iep auto-loads sector_map + sector_daily (Phase 12.6.5)"
```

---

## Task 7: Render sector section in context bundle

**Files:**
- Modify: `src/trading/llm/context.py`
- Test: `tests/test_llm_context.py`
- Delete and re-record: `tests/__snapshots__/test_llm_context.ambr`

- [ ] **Step 1: Write failing tests for the sector section + per-candidate bullet**

Append to `tests/test_llm_context.py`:

```python
from trading.data.sector import SectorRow
from trading.store.sector_store import upsert_sector_daily


def _seed_sector(conn: sqlite3.Connection) -> None:
    upsert_sector_daily(
        conn,
        [
            SectorRow(date="2026-05-15", sector="IT", close=36000.0,
                      rs_5d=0.012, rs_20d=0.035, rs_60d=0.02, regime="LEADING"),
            SectorRow(date="2026-05-15", sector="METAL", close=9000.0,
                      rs_5d=-0.01, rs_20d=-0.03, rs_60d=-0.04, regime="LAGGING"),
            SectorRow(date="2026-05-15", sector="FMCG", close=58000.0,
                      rs_5d=0.001, rs_20d=0.005, rs_60d=0.002, regime="NEUTRAL"),
        ],
    )
    conn.commit()


def test_assemble_context_includes_sector_snapshot(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_sector(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Sector momentum" in body
    assert "IT" in body and "+3.50%" in body and "LEADING" in body
    assert "METAL" in body and "-3.00%" in body and "LAGGING" in body


def test_assemble_context_sector_empty_when_no_rows(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Sector momentum" in body
    assert "_(no data)_" in body.split("## Sector momentum")[1].split("##")[0]


def test_assemble_context_per_candidate_sector_bullet(
    conn: sqlite3.Connection, paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a candidate's symbol is in sector_map AND sector_daily, the
    candidate block gains a one-line 'sector: <code> — 20d RS …' bullet."""
    # Re-point paths fixture's project root so load_sector_map sees our file.
    # paths fixture already set TRADING_PROJECT_ROOT; reuse it.
    static_dir = paths.project_root / "data" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "sector_map.csv").write_text(
        "symbol,sector\nINFY,IT\n", encoding="utf-8"
    )
    _seed_sector(conn)
    cand = _candidate("INFY", n_passed=9)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[cand], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "sector: IT — 20d RS +3.50% (LEADING)" in body


def test_assemble_context_no_sector_bullet_when_unmapped(
    conn: sqlite3.Connection, paths, tmp_path: Path
) -> None:
    """Candidate not present in sector_map.csv → no sector bullet rendered."""
    static_dir = paths.project_root / "data" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "sector_map.csv").write_text(
        "symbol,sector\nINFY,IT\n", encoding="utf-8"
    )
    _seed_sector(conn)
    cand = _candidate("BHARTIARTL", n_passed=9)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[cand], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    # BHARTIARTL section exists but contains no "sector:" line.
    assert "BHARTIARTL" in body
    bharti_block = body.split("### BHARTIARTL")[1].split("###")[0]
    assert "sector:" not in bharti_block
```

Note: `_candidate(...)` is the existing helper in `test_llm_context.py` — verify it exists. If not, use the existing `_seed_open_trade` / similar pattern.

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/test_llm_context.py::test_assemble_context_includes_sector_snapshot tests/test_llm_context.py::test_assemble_context_sector_empty_when_no_rows tests/test_llm_context.py::test_assemble_context_per_candidate_sector_bullet tests/test_llm_context.py::test_assemble_context_no_sector_bullet_when_unmapped -v`
Expected: 4 failures (sector section + bullet not yet rendered).

- [ ] **Step 3: Render the sector section + per-candidate bullet in `context.py`**

Edit `src/trading/llm/context.py`:

Add imports at the top:
```python
from trading.data.sector import SectorRow, load_sector_map
from trading.store.sector_store import get_sector_daily
```

Add `_render_sector_snapshot` (place it just after `_render_macro`):

```python
def _render_sector_snapshot(conn: sqlite3.Connection, as_of: date) -> str:
    rows = get_sector_daily(conn, as_of)
    if not rows:
        return "## Sector momentum\n\n_(no data)_"
    # Sort by rs_20d desc; None values last.
    def _key(r: SectorRow) -> tuple[int, float]:
        return (0, -r.rs_20d) if r.rs_20d is not None else (1, 0.0)

    sorted_rows = sorted(rows, key=_key)
    lines = [
        "## Sector momentum",
        "",
        "| Sector | Close | 5d RS | 20d RS | 60d RS | Regime |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for r in sorted_rows:
        lines.append(
            f"| {r.sector} | {r.close:,.0f} | {_fmt_rs(r.rs_5d)} | "
            f"{_fmt_rs(r.rs_20d)} | {_fmt_rs(r.rs_60d)} | {r.regime or '—'} |"
        )
    return "\n".join(lines)


def _fmt_rs(v: float | None) -> str:
    """Format RS as a signed percent string. `None` → em-dash."""
    if v is None:
        return "—"
    return f"{v * 100:+.2f}%"
```

Modify `assemble_context` to include the new section AND to load sector context for candidates. Replace the body of the function:

```python
def assemble_context(
    *,
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    mode: Mode,
    inputs: ContextInputs,
) -> Path:
    """Render `_context.md` for `as_of` and return the written path.

    Sections rendered (in order): header, macro, sector momentum,
    candidates (with per-candidate sector bullet when mapped),
    Layer-B ranker (when scored), holdings health, open paper-trades,
    (post_close only) matured predictions.
    """
    date_dir = paths.research_dir / as_of.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    out_path = date_dir / "_context.md"

    sector_map = load_sector_map(paths)
    sector_rows = {r.sector: r for r in get_sector_daily(conn, as_of)}

    parts: list[str] = []
    parts.append(_render_header(as_of, mode))
    parts.append(_render_macro(conn, as_of))
    parts.append(_render_sector_snapshot(conn, as_of))
    parts.append(_render_candidates(conn, as_of, inputs.candidates, sector_map, sector_rows))
    ranker_section = _render_ranker_section(inputs.scored_candidates)
    if ranker_section:
        parts.append(ranker_section)
    parts.append(_render_holdings_health(inputs.holdings_health))
    parts.append(_render_open_trades(conn))
    if mode == "post_close":
        parts.append(_render_matured_predictions(conn, as_of))

    out_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return out_path
```

Modify `_render_candidates` signature and body to take + use the sector data:

```python
def _render_candidates(
    conn: sqlite3.Connection,
    as_of: date,
    candidates: list[Candidate],
    sector_map: dict[str, str],
    sector_rows: dict[str, SectorRow],
) -> str:
    if not candidates:
        return "## Today's candidates\n\n_(no data)_"

    sorted_cands = sorted(
        candidates,
        key=lambda c: (-sum(1 for r in c.rules if r.passed), c.symbol),
    )
    blocks: list[str] = ["## Today's candidates"]
    for c in sorted_cands[:5]:
        n_passed = sum(1 for r in c.rules if r.passed)
        n_total = len(c.rules)
        blocks.append("")
        blocks.append(f"### {c.symbol} — passes {n_passed}/{n_total} rules")
        blocks.append(
            f"- close {c.close:.2f}, RSI {c.rsi_14:.1f}, ATR(14) {c.atr_14:.2f}"
        )
        blocks.append(
            f"- SMA20 {c.sma_20:.2f} · SMA50 {c.sma_50:.2f} · SMA200 {c.sma_200:.2f}"
        )
        sector_line = _sector_bullet_for(c.symbol, sector_map, sector_rows)
        if sector_line:
            blocks.append(sector_line)
        blocks.extend(_render_news_for_symbol(conn, c.symbol, as_of))
    return "\n".join(blocks)


def _sector_bullet_for(
    symbol: str,
    sector_map: dict[str, str],
    sector_rows: dict[str, SectorRow],
) -> str | None:
    sec = sector_map.get(symbol)
    if sec is None:
        return None
    row = sector_rows.get(sec)
    if row is None:
        return None
    rs = _fmt_rs(row.rs_20d)
    regime = row.regime or "—"
    return f"- sector: {sec} — 20d RS {rs} ({regime})"
```

- [ ] **Step 4: Delete obsolete snapshot file (will be regenerated)**

The two `test_full_*_bundle_snapshot` tests use syrupy snapshots that no longer reflect the new section ordering. Delete the snapshot file so it gets re-recorded:

```bash
rm tests/__snapshots__/test_llm_context.ambr
```

- [ ] **Step 5: Re-record snapshots**

Run: `uv run pytest tests/test_llm_context.py --snapshot-update -v`
Expected: tests pass, new `tests/__snapshots__/test_llm_context.ambr` written.

Inspect the new snapshot file to confirm the `## Sector momentum` section appears with `_(no data)_` for the pre-existing snapshot tests (those don't seed sector data).

- [ ] **Step 6: Run the full llm_context suite**

Run: `uv run pytest tests/test_llm_context.py -v`
Expected: all tests pass including the 4 new ones.

- [ ] **Step 7: Commit**

```bash
git add src/trading/llm/context.py tests/test_llm_context.py tests/__snapshots__/test_llm_context.ambr
git commit -m "feat(llm): sector momentum section + per-candidate sector bullet (Phase 12.6.6)"
```

---

## Task 8: Briefing placeholder + analyst skill rewording

**Files:**
- Modify: `src/trading/llm/briefing.py`
- Modify: `.claude/skills/analyst/SKILL.md`
- Test: `tests/test_llm_briefing.py` (re-record snapshots if needed)

- [ ] **Step 1: Update SECTOR_COMMENTARY_PLACEHOLDER in briefing.py**

In `src/trading/llm/briefing.py`, change:

```python
SECTOR_COMMENTARY_PLACEHOLDER = "_(sector commentary not yet wired — see Phase 12.6)_"
```

to:

```python
SECTOR_COMMENTARY_PLACEHOLDER = "_(analyst did not write a sector commentary for this run)_"
```

- [ ] **Step 2: Update the analyst SKILL.md**

In `.claude/skills/analyst/SKILL.md`, find this sentence:

> `sector_commentary.md` is OPTIONAL while `sector_daily` is unwired (Phase 12.6 will build it). If the bundle has no sector data, you may skip writing this file — `compile_brief` will substitute a placeholder under the `## Sector commentary` header.

Replace with:

> `sector_commentary.md` is optional. Write it when the bundle's `## Sector momentum` section is non-empty — cite specific sector codes and their 20d RS values from that table. When the section says `_(no data)_`, skip writing this file and `compile_brief` will substitute a placeholder under the `## Sector commentary` header.

- [ ] **Step 3: Re-record briefing snapshots if they break**

Run: `uv run pytest tests/test_llm_briefing.py -v`

If any syrupy assertion fails because of the placeholder wording:

```bash
uv run pytest tests/test_llm_briefing.py --snapshot-update -v
```

Inspect the updated `tests/__snapshots__/test_llm_briefing.ambr` to confirm only the placeholder text changed.

- [ ] **Step 4: Commit**

```bash
git add src/trading/llm/briefing.py .claude/skills/analyst/SKILL.md tests/__snapshots__/test_llm_briefing.ambr
git commit -m "docs(analyst): reword sector commentary guidance (Phase 12.6.7)"
```

---

## Task 9: CLI `trading sector` + extend `trading pre-open` table

**Files:**
- Modify: `src/trading/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests for `trading sector`**

Append to `tests/test_cli.py` (find the existing `from trading.cli import app` import and add tests near other CLI happy-path tests):

```python
def test_trading_sector_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`trading sector --date YYYY-MM-DD` writes rows + renders table."""
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.data.sector import SectorRow

    fake_rows = [
        SectorRow(date="2026-05-26", sector="IT", close=36000.0,
                  rs_5d=0.012, rs_20d=0.035, rs_60d=0.02, regime="LEADING"),
        SectorRow(date="2026-05-26", sector="METAL", close=9000.0,
                  rs_5d=-0.01, rs_20d=-0.03, rs_60d=-0.04, regime="LAGGING"),
    ]
    monkeypatch.setattr("trading.cli.fetch_all_sectors", lambda _as_of: fake_rows)

    from typer.testing import CliRunner
    from trading.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["sector", "--date", "2026-05-26"])
    assert result.exit_code == 0, result.output
    assert "IT" in result.output and "METAL" in result.output

    # Rows should have been persisted.
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations
    from trading.config import get_paths
    paths = get_paths()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        n = conn.execute("SELECT COUNT(*) AS n FROM sector_daily").fetchone()["n"]
    assert n == 2


def test_trading_sector_dry_run_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.data.sector import SectorRow

    monkeypatch.setattr(
        "trading.cli.fetch_all_sectors",
        lambda _as_of: [SectorRow(date="2026-05-26", sector="IT", close=36000.0,
                                   rs_5d=0.01, rs_20d=0.02, rs_60d=0.0, regime="NEUTRAL")],
    )

    from typer.testing import CliRunner
    from trading.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["sector", "--date", "2026-05-26", "--dry-run"])
    assert result.exit_code == 0
    assert "Dry run" in result.output

    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations
    from trading.config import get_paths
    paths = get_paths()
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        n = conn.execute("SELECT COUNT(*) AS n FROM sector_daily").fetchone()["n"]
    assert n == 0


def test_trading_sector_exits_nonzero_when_no_rows_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("trading.cli.fetch_all_sectors", lambda _as_of: [])

    from typer.testing import CliRunner
    from trading.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["sector", "--date", "2026-05-26"])
    assert result.exit_code == 1
```

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/test_cli.py::test_trading_sector_happy_path tests/test_cli.py::test_trading_sector_dry_run_does_not_write tests/test_cli.py::test_trading_sector_exits_nonzero_when_no_rows_fetched -v`
Expected: failures (`sector` subcommand doesn't exist yet).

- [ ] **Step 3: Add `sector` command to `src/trading/cli.py`**

Add imports next to `from trading.data.macro import snapshot_and_classify` (around line 51):

```python
from trading.data.sector import fetch_all_sectors
from trading.store.sector_store import upsert_sector_daily
```

After the `macro_cmd` function (around line 657), add:

```python
@app.command("sector")
def sector_cmd(
    as_of: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Snapshot date (YYYY-MM-DD). Defaults to today.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the snapshot without writing to SQLite."),
    ] = False,
) -> None:
    """Pull NSE sectoral indices, compute RS vs Nifty 50, upsert sector_daily."""
    paths = get_paths()
    target_date = date.fromisoformat(as_of) if as_of else date.today()

    console.print(f"[bold]Pulling sector snapshot for {target_date}…[/bold]")
    rows = fetch_all_sectors(target_date)

    if not rows:
        console.print("[red]No sector rows fetched (benchmark or all sectors failed).[/red]")
        raise typer.Exit(code=1)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Sector")
    table.add_column("Close", justify="right")
    table.add_column("5d RS", justify="right")
    table.add_column("20d RS", justify="right")
    table.add_column("60d RS", justify="right")
    table.add_column("Regime", justify="center")
    for r in rows:
        table.add_row(
            r.sector,
            f"{r.close:,.0f}",
            _fmt_rs_pct(r.rs_5d),
            _fmt_rs_pct(r.rs_20d),
            _fmt_rs_pct(r.rs_60d),
            r.regime or "—",
        )
    console.print(table)

    if dry_run:
        console.print("\n[yellow]Dry run — nothing written.[/yellow]")
        return

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(conn, rows)
    console.print(f"\n[green]sector_daily written for {target_date.isoformat()} ({len(rows)} rows).[/green]")


def _fmt_rs_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.2f}%"
```

- [ ] **Step 4: Extend `pre_open_cmd`'s Rich table with the new sector_written row**

In `src/trading/cli.py`, find `pre_open_cmd` (around line 1129) and the line `table.add_row("macro_written", "yes" if result.macro_written else "no")` (around line 1150). Immediately after it add:

```python
    table.add_row("sector_written", "yes" if result.sector_written else "no")
```

- [ ] **Step 5: Run new CLI tests + the full CLI suite**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading sector + pre-open table sector_written row (Phase 12.6.8)"
```

---

## Task 10: Full-suite verification

**Files:** (none modified — verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass (current baseline is 657 passed + 1 skipped; Phase 12.6 adds ~15 tests so expect ~670+ passed). Exact counts in the smoke task.

- [ ] **Step 2: Lint**

Run: `uv run ruff check .`
Expected: All checks passed.

- [ ] **Step 3: Type-check**

Run: `uv run mypy src/`
Expected: Success.

- [ ] **Step 4: If anything fails, fix and re-run**

Common issues to watch for:
- `mypy` complaining about `dict[str, SectorRow]` — add explicit type annotations.
- `ruff` complaining about unused imports left over from refactoring `_render_candidates` signature.
- Existing test files that construct `PreOpenResult(...)` without `sector_written=` — see Task 5 Step 5.

No commit yet — smoke test next.

---

## Task 11: Real-data smoke + PROGRESS.md + final commit

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Smoke `trading sector` against live yfinance**

Run: `uv run trading sector --date 2026-05-26`

Expected output: a Rich table with 11 rows (one per sector), close prices that look plausible (NIFTYBANK in the 40,000s; IT in the 30,000s+; METAL in the 8-10k range), some sectors LEADING, some LAGGING. Final line: `sector_daily written for 2026-05-26 (11 rows).` If only 8-10 sectors come back, that's expected — some yfinance tickers occasionally fail; not a blocker for the smoke.

- [ ] **Step 2: Re-run `trading pre-open`**

Run: `uv run trading pre-open --date 2026-05-26`

(If pre-open requires a Kite snapshot for the date and one isn't present, run `/kite-snapshot` first via the Claude Code skill.)

Verify the printed Rich table now shows a `sector_written` row reading `yes`. Then open `data/research/2026-05-26/_context.md` and confirm:
- A `## Sector momentum` section appears between macro and candidates, with one row per sector
- Each candidate block has a `- sector: <CODE> — 20d RS …` bullet IF the symbol is in `sector_map.csv` (most Nifty 50 names + holdings are).

- [ ] **Step 3: Re-run `trading pre-open-iep`**

Run: `uv run trading pre-open-iep --date 2026-05-26`

Verify:
- No "Sector data unavailable" warning is printed
- Either filter+rerank ran with sector axis active (in RISK_ON/RISK_OFF regimes), or sector percentiles informed the rerank ordering (in NEUTRAL regimes).

- [ ] **Step 4: Update PROGRESS.md**

In `PROGRESS.md`, change:

```
| 12.6 | Sector data | `[ ]` |
```

to:

```
| 12.6 | Sector data | `[x]` |
```

In the same file, replace the entire Phase 12.6 block (currently sub-tasks 12.6.1 through 12.6.5, all `[ ]`) with:

```
## Phase 12.6 — Sector data

> Spec at [`docs/superpowers/specs/2026-05-26-phase-12-6-sector-data-design.md`](docs/superpowers/specs/2026-05-26-phase-12-6-sector-data-design.md).
> Plan at [`docs/superpowers/plans/2026-05-26-phase-12-6-sector-data.md`](docs/superpowers/plans/2026-05-26-phase-12-6-sector-data.md).
> 11 NSE sectoral indices via yfinance, RS vs Nifty 50, wired into pre_open + pre_open_iep + assemble_context.

- [x] 12.6.1 `src/trading/data/sector.py`: 11-sector ticker dict +
       `^NSEI` benchmark + simple-difference `compute_rs` + 5d/20d/60d
       windows + LEADING/NEUTRAL/LAGGING regime thresholds on rs_20d
       (±2%). Defensive yfinance wrapper (`fetch_sector_history`) +
       `fetch_all_sectors(as_of)` orchestrator that skips failed
       tickers + `load_sector_map(paths)` CSV reader. 12 tests in
       `test_data_sector.py`.
- [x] 12.6.2 `src/trading/store/sector_store.py`: `upsert_sector_daily`
       (INSERT ON CONFLICT(date,sector) DO UPDATE, executemany) +
       `get_sector_daily(conn, as_of)` reader. 3 tests in
       `test_store_sector.py`.
- [x] 12.6.3 `data/static/sector_map.csv`: symbol→sector map for 57
       universe symbols (Nifty 50 + holdings). Comment lines tolerated;
       symbols not listed treated as no-sector by pre_open_iep.
- [x] 12.6.4 `src/trading/jobs/pre_open.py`: `_step_sector` inserted
       between `_step_macro` and `_step_news`; graceful degradation
       (warning, returns False) on fetch failure. `PreOpenResult.sector_written`
       added; CLI table renders it. 3 tests in `test_jobs_pre_open.py`.
- [x] 12.6.5 `src/trading/jobs/pre_open_iep.py`: when `sector_map=None
       and sector_momentum=None`, auto-load via `load_sector_map` +
       `get_sector_daily(as_of)` with D-1 fallback. Passing `{}`
       explicitly suppresses auto-load. 4 tests in
       `test_jobs_pre_open_iep.py`.
- [x] 12.6.6 `src/trading/llm/context.py`: `_render_sector_snapshot`
       section between macro and candidates; per-candidate `sector: CODE
       — 20d RS …` bullet rendered when symbol is in sector_map AND
       sector_daily. 4 new tests + snapshot re-record.
- [x] 12.6.7 `briefing.py` SECTOR_COMMENTARY_PLACEHOLDER reworded to
       "analyst did not write a sector commentary for this run".
       `.claude/skills/analyst/SKILL.md` updated: section is optional;
       write when bundle's `## Sector momentum` is non-empty.
- [x] 12.6.8 `trading sector --date YYYY-MM-DD [--dry-run]` CLI: live
       fetch + Rich table + upsert. Exit 1 if zero rows fetched.
       `trading pre-open` table extended with `sector_written` row.
       3 tests in `test_cli.py`.
- [x] 12.6.9 Real-data smoke (2026-05-26): `trading sector` pulled
       11 sectors; `trading pre-open` showed `sector_written: yes` and
       a populated `## Sector momentum` section + per-candidate sector
       bullets; `trading pre-open-iep` ran with sector axis active.
       Full suite green, ruff + mypy clean. Commit `feat(data): sector
       daily + RS (Phase 12.6)` pushed to origin/main.
```

Also update the "Currently working on" / "Next up" lines near the status snapshot.

- [ ] **Step 5: Verify suite is still green**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src/`
Expected: all green.

- [ ] **Step 6: Commit and push**

```bash
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(data): sector daily + RS (Phase 12.6)

11 NSE sectoral indices via yfinance, simple-difference RS vs Nifty 50
over 5d/20d/60d, LEADING/NEUTRAL/LAGGING regime labels, persisted to
sector_daily. Wired into pre_open (_step_sector), pre_open_iep
(auto-load with D-1 fallback), and assemble_context (## Sector momentum
section + per-candidate sector bullet). New CLI: trading sector.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Self-review notes

**Spec coverage check:**
- 12.6.1 (sector fetcher) → Tasks 1 + 2 ✓
- 12.6.2 (persistence) → Task 3 ✓
- 12.6.3 (sector map CSV) → Task 4 ✓
- 12.6.4 (pre_open wiring) → Task 5 ✓
- 12.6.5 (pre_open_iep wiring) → Task 6 ✓
- 12.6.6 (context renderer) → Task 7 ✓
- 12.6.7 (briefing + skill) → Task 8 ✓
- 12.6.8 (CLI) → Task 9 ✓
- 12.6.9 (tests) → folded into each task ✓
- 12.6.10 (smoke + PROGRESS + commit) → Tasks 10 + 11 ✓

**Type/name consistency:**
- `SectorRow` dataclass defined in Task 1, used in Tasks 2, 3, 5, 6, 7, 9 ✓
- `compute_rs(sector_closes, benchmark_closes, *, window)` consistent across all uses ✓
- `_regime_for(rs_20d)` consistent ✓
- `load_sector_map(paths=None)` consistent (paths optional) ✓
- `fetch_all_sectors(as_of)` consistent ✓
- `upsert_sector_daily(conn, rows)` / `get_sector_daily(conn, as_of)` consistent ✓
- `_step_sector(conn, as_of, warnings)` consistent ✓
- `PreOpenResult.sector_written: bool` field added Task 5, rendered Task 9 ✓
