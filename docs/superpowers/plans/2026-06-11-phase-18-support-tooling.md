# Phase 18 Support Tooling (weekly_train + monthly_sip) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the two deferred Phase 18 jobs — `weekly_train` (Sunday LightGBM retrain + weekly performance review, unattended) and `monthly_sip` (1st-of-month ₹1L SIP allocation plan, reminder-driven).

**Architecture:** Two thin job modules in the established `src/trading/jobs/` pattern (`run_*` orchestrator + frozen result dataclass + private helpers). No new engines: weekly_train delegates to Phase 16 `train_walkforward` + model registry and Phase 7 metrics; monthly_sip delegates to Phase 10 `allocate_sip` + `score_holding`. A shared `ranker_io` module extracts the training-input loading currently duplicated in `cli.py`.

**Tech Stack:** Python 3.11, uv, typer/Rich CLI, SQLite, pandas, LightGBM (existing), loguru + Phase 17 notify, pytest (+ syrupy snapshots for the two pure renderers).

**Spec:** `docs/superpowers/specs/2026-06-11-phase-18-support-tooling-design.md`

---

## File structure

| File | Action | Responsibility |
|---|---|---|
| `src/trading/strategy/ranker_io.py` | Create | `TrainingInputs` + `load_training_inputs(paths, conn)` — parquet + SQLite loading shared by `train-ranker` CLI and weekly_train |
| `src/trading/cli.py` | Modify | `train_ranker` uses ranker_io; new `weekly-train` and `sip` commands |
| `src/trading/store/model_registry.py` | Modify | `has_row_for_train_end` idempotency guard |
| `src/trading/jobs/weekly_train.py` | Create | review gathering/rendering + retrain step + `run_weekly_train` |
| `src/trading/jobs/monthly_sip.py` | Create | candidate gathering + health + `run_monthly_sip` + plan renderer |
| `src/trading/jobs/__init__.py` | Modify | export new jobs |
| `src/trading/ops/runner.py` | Modify | `ReminderSlot.gate_holidays` + `monthly_sip` slot |
| `docs/scheduler/trading_weekly_train.xml` | Create | Sunday 10:00 unattended run |
| `docs/scheduler/trading_remind_monthly_sip.xml` | Create | 1st-of-month 09:30 reminder |
| `scripts/weekly_train.bat` | Create | manual launcher |
| `tests/test_ranker_io.py` | Create | loader tests |
| `tests/test_model_registry.py` | Modify | guard test |
| `tests/test_jobs_weekly_train.py` | Create | review + retrain + orchestrator tests |
| `tests/test_jobs_monthly_sip.py` | Create | window + gathering + orchestrator tests |
| `tests/test_ops_runner.py` | Modify | slot count 12→13, gate_holidays |
| `tests/test_cli.py` | Modify | weekly-train + sip CLI tests |
| `PROGRESS.md` | Modify | record the mini-phase (final task) |

Conventions you must follow (from the repo):
- Run everything through `uv run` (e.g. `uv run pytest …`).
- Gates before every commit: the task's tests pass; at the end (Task 10) also `uv run ruff check .`, `uv run mypy src/`, full `uv run pytest -q`.
- Test fixtures: `paths` fixture monkeypatches `TRADING_PROJECT_ROOT` to `tmp_path` (see `tests/test_jobs_pre_open.py:50-53`); in-memory SQLite conns set `row_factory = sqlite3.Row` and call `run_migrations`.

---

### Task 1: `ranker_io.load_training_inputs` (extract from cli.py)

**Files:**
- Create: `src/trading/strategy/ranker_io.py`
- Modify: `src/trading/cli.py` (the `train_ranker` command, currently lines ~1454-1516)
- Test: `tests/test_ranker_io.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ranker_io.py`:

```python
"""Tests for trading.strategy.ranker_io — shared training-input loader."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.store.migrations import run_migrations
from trading.store.ohlcv import write_ohlcv


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


def _frame(n: int) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    idx.name = "date"
    closes = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def test_load_training_inputs_filters_short_history(paths, conn) -> None:
    from trading.strategy.ranker_io import load_training_inputs

    write_ohlcv(_frame(250), "LONGSYM", paths)
    write_ohlcv(_frame(50), "SHORTSYM", paths)

    inputs = load_training_inputs(paths, conn)
    assert set(inputs.enriched) == {"LONGSYM"}
    # enrichment added indicator columns beyond the raw five
    assert inputs.enriched["LONGSYM"].shape[1] > 5


def test_load_training_inputs_builds_macro_and_sentiment(paths, conn) -> None:
    from trading.data.macro import MacroSnapshot
    from trading.store.macro_store import upsert_macro_snapshot
    from trading.strategy.ranker_io import load_training_inputs

    write_ohlcv(_frame(250), "LONGSYM", paths)
    upsert_macro_snapshot(
        conn,
        MacroSnapshot(
            date=date(2026, 6, 1),
            sgx_nifty=None,
            dow_fut=None,
            nasdaq_fut=None,
            sp500=None,
            usdinr=95.0,
            crude=None,
            vix=15.0,
            us_10y=None,
            fii_flow_cr=100.0,
            dii_flow_cr=200.0,
            regime="NEUTRAL",
        ),
    )
    conn.execute(
        "INSERT INTO sentiment_daily "
        "(date, symbol, score_7d, score_30d, news_count, negative_news_count, has_critical) "
        "VALUES ('2026-06-01', 'LONGSYM', 0.2, 0.1, 3, 1, 0)"
    )

    inputs = load_training_inputs(paths, conn)
    assert list(inputs.macro_history.index) == ["2026-06-01"]
    assert list(inputs.macro_history.columns) == ["vix", "usdinr", "fii_flow_cr"]
    assert ("2026-06-01", "LONGSYM") in inputs.sentiment_lookup
    assert inputs.sentiment_lookup[("2026-06-01", "LONGSYM")].score_7d == 0.2


def test_load_training_inputs_empty_universe(paths, conn) -> None:
    from trading.strategy.ranker_io import load_training_inputs

    inputs = load_training_inputs(paths, conn)
    assert inputs.enriched == {}
    assert inputs.macro_history.empty
    assert inputs.sentiment_lookup == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ranker_io.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.strategy.ranker_io'`

- [ ] **Step 3: Implement the module**

Create `src/trading/strategy/ranker_io.py`:

```python
"""Shared loader for ranker training inputs (parquet + SQLite).

Extracted from the `train-ranker` CLI so weekly_train (Phase 18) reuses
the exact same wiring: enriched OHLCV per symbol with 200+ bars, macro
history frame, and a (date, symbol) → SentimentDailyRow lookup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

from trading.config import Paths
from trading.features.technicals import add_indicators
from trading.store.news_store import SentimentDailyRow
from trading.store.ohlcv import list_symbols, read_ohlcv

MIN_BARS = 200


@dataclass(frozen=True)
class TrainingInputs:
    enriched: dict[str, pd.DataFrame]
    macro_history: pd.DataFrame
    sentiment_lookup: dict[tuple[str, str], SentimentDailyRow]


def load_training_inputs(paths: Paths, conn: sqlite3.Connection) -> TrainingInputs:
    """Load every input `train_walkforward` needs from parquet + SQLite."""
    enriched: dict[str, pd.DataFrame] = {}
    for s in list_symbols(paths):
        try:
            df = read_ohlcv(s, paths)
        except FileNotFoundError:
            continue
        if len(df) < MIN_BARS:
            continue
        enriched[s] = add_indicators(df)

    macro_rows = conn.execute(
        "SELECT date, vix, usdinr, fii_flow_cr FROM macro_snapshot ORDER BY date"
    ).fetchall()
    macro_history = pd.DataFrame(
        {
            "vix": [r["vix"] for r in macro_rows],
            "usdinr": [r["usdinr"] for r in macro_rows],
            "fii_flow_cr": [r["fii_flow_cr"] for r in macro_rows],
        },
        index=[r["date"] for r in macro_rows],
    )

    sentiment_lookup: dict[tuple[str, str], SentimentDailyRow] = {}
    for s in enriched:
        for r in conn.execute(
            "SELECT * FROM sentiment_daily WHERE symbol = ?", (s,)
        ).fetchall():
            sentiment_lookup[(r["date"], s)] = SentimentDailyRow(
                date=r["date"],
                symbol=s,
                score_7d=r["score_7d"],
                score_30d=r["score_30d"],
                news_count=r["news_count"],
                negative_news_count=r["negative_news_count"],
                has_critical=bool(r["has_critical"]),
            )
    return TrainingInputs(
        enriched=enriched,
        macro_history=macro_history,
        sentiment_lookup=sentiment_lookup,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ranker_io.py -v`
Expected: 3 PASS

- [ ] **Step 5: Refactor `cli.py` `train_ranker` to use it**

In `src/trading/cli.py`, inside the `train_ranker` command, **replace** the block that builds `enriched`, `macro_history`, and `sentiment_lookup` (everything from `syms = list_symbols(paths)` through the end of the `with get_conn(...)` block that fills `sentiment_lookup`) with:

```python
    syms = list_symbols(paths)
    if not syms:
        console.print("[red]no parquet symbols found — run ingest-history first[/red]")
        raise typer.Exit(code=2)

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        inputs = load_training_inputs(paths, conn)

    if not inputs.enriched:
        console.print("[red]no symbols with sufficient history (200+ bars)[/red]")
        raise typer.Exit(code=2)
```

Add `from trading.strategy.ranker_io import load_training_inputs` to the command's local imports (next to the existing `from trading.strategy.ranker_train import ...`). Remove the now-unused local imports `add_indicators` and `SentimentDailyRow` from this command (they live in ranker_io now). Then change the `train_walkforward(...)` call to use `inputs.*`:

```python
        result = train_walkforward(
            enriched=inputs.enriched,
            macro_history=inputs.macro_history,
            sentiment_lookup=inputs.sentiment_lookup,
            negative_news_lookup={},
            start=start_ts,
            end=end_ts,
        )
```

- [ ] **Step 6: Run the existing ranker CLI tests to confirm no regression**

Run: `uv run pytest tests/test_cli_ranker.py tests/test_ranker_io.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/trading/strategy/ranker_io.py src/trading/cli.py tests/test_ranker_io.py
git commit -m "refactor(strategy): extract ranker training-input loader (Phase 18 prep)"
```

---

### Task 2: `model_registry.has_row_for_train_end`

**Files:**
- Modify: `src/trading/store/model_registry.py`
- Test: `tests/test_model_registry.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_model_registry.py` (self-contained — does not depend on that file's existing fixtures):

```python
def test_has_row_for_train_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    from trading.config import get_paths
    from trading.store.model_registry import (
        RegistryRow,
        has_row_for_train_end,
        register,
    )

    p = get_paths()
    assert has_row_for_train_end(p, "2026-06-14") is False

    register(
        p,
        row=RegistryRow(
            version="2026-06-14",
            trained_at="2026-06-14T05:00:00+00:00",
            train_start="2023-06-14",
            train_end="2026-06-14",
            oos_sharpe=float("nan"),
            oos_hit_rate=float("nan"),
            n_train_examples=40,
            n_features=20,
            path="models/ranker_2026-06-14.pkl",
            active=False,
            notes="",
        ),
        promote=False,
    )
    assert has_row_for_train_end(p, "2026-06-14") is True
    assert has_row_for_train_end(p, "2026-06-21") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_model_registry.py::test_has_row_for_train_end -v`
Expected: FAIL — `ImportError: cannot import name 'has_row_for_train_end'`

- [ ] **Step 3: Implement**

Append to `src/trading/store/model_registry.py` (after `all_rows`):

```python
def has_row_for_train_end(paths: Paths, train_end: str) -> bool:
    """True iff any registry row was trained on a window ending `train_end`.

    Weekly idempotency guard: a Sunday re-run of weekly_train must not
    append a duplicate training row for the same window.
    """
    return any(r.train_end == train_end for r in all_rows(paths))
```

Note: `Paths` is only imported under `TYPE_CHECKING` in this module — that is fine for an annotation; no import change needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_model_registry.py -v`
Expected: all PASS (existing + 1 new)

- [ ] **Step 5: Commit**

```bash
git add src/trading/store/model_registry.py tests/test_model_registry.py
git commit -m "feat(store): registry has_row_for_train_end guard (Phase 18)"
```

---

### Task 3: weekly review — data gathering + renderer

**Files:**
- Create: `src/trading/jobs/weekly_train.py` (review half of the module)
- Test: `tests/test_jobs_weekly_train.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jobs_weekly_train.py`:

```python
"""Tests for trading.jobs.weekly_train."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.store.migrations import run_migrations

AS_OF = date(2026, 6, 14)  # a Sunday


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


@pytest.fixture
def notify_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "trading.jobs.weekly_train.notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )
    return calls


def _memdb() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


def _seed_week(conn: sqlite3.Connection) -> None:
    """One closed trade in-window, one open trade, one matured prediction,
    two portfolio snapshots."""
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, created_by) "
        "VALUES ('2026-06-08T08:30:00', 'RVNL', 'LONG', 100.0, 95.0, 120.0, 25, 'test')"
    )
    sig1 = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, ts_exit, "
        "exit_price, exit_reason, pnl, pnl_pct, days_held) "
        "VALUES (?, '2026-06-08T09:15:00', 100.0, 10, '2026-06-12T15:30:00', 106.0, "
        "'TARGET', 60.0, 6.0, 4)",
        (sig1,),
    )
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, created_by) "
        "VALUES ('2026-06-10T08:30:00', 'NTPC', 'LONG', 300.0, 290.0, 360.0, 25, 'test')"
    )
    sig2 = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, current_stop) "
        "VALUES (?, '2026-06-10T09:15:00', 300.0, 5, 292.0)",
        (sig2,),
    )
    conn.execute(
        "INSERT INTO predictions (ts, symbol, predicted_return_pct, predicted_horizon_days, "
        "actual_return_at_horizon, error_pct, evaluated_at) "
        "VALUES ('2026-05-01T08:30:00', 'RVNL', 20.0, 25, 6.0, 14.0, '2026-06-05T16:00:00')"
    )
    conn.execute(
        "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity) "
        "VALUES ('2026-06-11', 100000, '{}', 100000)"
    )
    conn.execute(
        "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity) "
        "VALUES ('2026-06-12', 100060, '{}', 100060)"
    )
    conn.commit()


def test_gather_review_data_empty() -> None:
    from trading.jobs.weekly_train import gather_review_data

    data = gather_review_data(_memdb(), AS_OF)
    assert data.closed_week == []
    assert data.closed_all == []
    assert data.open_trades == []
    assert data.equity.empty
    assert data.calibration == []


def test_gather_review_data_seeded() -> None:
    from trading.jobs.weekly_train import gather_review_data

    conn = _memdb()
    _seed_week(conn)
    data = gather_review_data(conn, AS_OF)
    assert len(data.closed_week) == 1
    assert data.closed_week[0].symbol == "RVNL"
    assert data.closed_week[0].net_pnl == 60.0
    assert data.closed_week[0].initial_stop == 95.0
    assert len(data.closed_all) == 1
    assert len(data.open_trades) == 1
    assert data.open_trades[0].current_stop == 292.0
    assert list(data.equity) == [100000.0, 100060.0]
    assert len(data.calibration) == 1
    assert data.calibration[0].predicted_pct == 20.0
    assert data.calibration[0].n == 1


def test_week_window_boundaries() -> None:
    """Exit exactly on week_start (as_of − 7) excluded; on as_of included."""
    from trading.jobs.weekly_train import gather_review_data

    conn = _memdb()
    for i, ts_exit in enumerate(["2026-06-07T15:30:00", "2026-06-14T15:30:00"]):
        cur = conn.execute(
            "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, "
            "created_by) VALUES (?, ?, 'LONG', 100.0, 95.0, 120.0, 25, 'test')",
            (f"2026-06-0{i + 1}T08:30:00", f"SYM{i}"),
        )
        conn.execute(
            "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, ts_exit, "
            "exit_price, exit_reason, pnl, pnl_pct, days_held) "
            "VALUES (?, '2026-06-01T09:15:00', 100.0, 10, ?, 101.0, 'TIME', 10.0, 1.0, 5)",
            (cur.lastrowid, ts_exit),
        )
    conn.commit()

    data = gather_review_data(conn, AS_OF)
    assert len(data.closed_all) == 2
    assert len(data.closed_week) == 1
    assert data.closed_week[0].symbol == "SYM1"


def test_render_review_empty_placeholders() -> None:
    from trading.jobs.weekly_train import (
        RetrainOutcome,
        ReviewData,
        render_weekly_review,
    )

    data = ReviewData(
        week_start=date(2026, 6, 7),
        as_of=AS_OF,
        closed_week=[],
        closed_all=[],
        open_trades=[],
        equity=pd.Series(dtype=float),
        calibration=[],
    )
    text = render_weekly_review(
        data, RetrainOutcome(False, "skip_train requested", None, None, False, None)
    )
    assert text.count("_(no data)_") >= 3
    assert "# Weekly review — 2026-06-14" in text
    assert "Trained: no — skip_train requested" in text


def test_render_review_seeded_snapshot(snapshot) -> None:
    from trading.jobs.weekly_train import (
        RetrainOutcome,
        ReviewData,
        gather_review_data,
        render_weekly_review,
    )

    conn = _memdb()
    _seed_week(conn)
    data = gather_review_data(conn, AS_OF)
    text = render_weekly_review(
        data,
        RetrainOutcome(True, None, 42, float("nan"), False, "models/ranker_2026-06-14.pkl"),
    )
    assert "| RVNL | 100.00 | 106.00 |" in text
    assert "| NTPC |" in text
    assert "Trained: yes — 42 examples" in text
    assert text == snapshot
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jobs_weekly_train.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.jobs.weekly_train'`

- [ ] **Step 3: Implement the review half of the module**

Create `src/trading/jobs/weekly_train.py`:

```python
"""Phase 18 — weekly_train job: Sunday retrain + weekly performance review.

Spec: docs/superpowers/specs/2026-06-11-phase-18-support-tooling-design.md
Runs unattended via Task Scheduler every Sunday 10:00 IST. The retrain
step is graceful (guarded, InsufficientDataError continues); the review
markdown is always written.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

from trading.backtest.metrics import (
    avg_r_multiple,
    expectancy,
    hit_rate,
    profit_factor,
    sharpe,
)
from trading.config import Paths, get_paths
from trading.ops.notify import notify
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.model_registry import (
    RegistryRow,
    has_row_for_train_end,
    register,
    save_model,
)
from trading.strategy.ranker_features import FEATURE_NAMES
from trading.strategy.ranker_io import load_training_inputs
from trading.strategy.ranker_train import InsufficientDataError, train_walkforward

_IST = timezone(timedelta(hours=5, minutes=30))

TRAIN_WINDOW_YEARS = 3
REVIEW_WINDOW_DAYS = 7

_NO_DATA = "_(no data)_"


# ---------------------------------------------------------------------------
# Result / data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosedTrade:
    """TradeLike adapter over one paper_trades ⋈ signals row."""

    symbol: str
    ts_entry: str
    ts_exit: str
    entry_price: float
    exit_price: float
    qty: int
    exit_reason: str
    days_held: int
    net_pnl: float
    gross_pnl: float
    initial_stop: float
    pnl_pct: float


@dataclass(frozen=True)
class OpenTradeRow:
    symbol: str
    ts_entry: str
    entry_price: float
    qty: int
    current_stop: float | None


@dataclass(frozen=True)
class CalibrationBucket:
    predicted_pct: float
    n: int
    actual_mean_pct: float | None
    realized_hit_rate: float


@dataclass(frozen=True)
class ReviewData:
    week_start: date
    as_of: date
    closed_week: list[ClosedTrade]
    closed_all: list[ClosedTrade]
    open_trades: list[OpenTradeRow]
    equity: pd.Series
    calibration: list[CalibrationBucket]


@dataclass(frozen=True)
class RetrainOutcome:
    ran: bool
    skip_reason: str | None
    examples: int | None
    oos_sharpe: float | None
    promoted: bool
    model_path: str | None


@dataclass(frozen=True)
class WeeklyTrainResult:
    as_of: date
    window_start: date
    window_end: date
    retrain_ran: bool
    retrain_skip_reason: str | None
    examples: int | None
    oos_sharpe: float | None
    promoted: bool
    model_path: str | None
    review_path: Path


# ---------------------------------------------------------------------------
# Review — gathering
# ---------------------------------------------------------------------------

_CLOSED_SQL = (
    "SELECT s.symbol, pt.ts_entry, pt.ts_exit, pt.entry_price, pt.exit_price, "
    "       pt.qty, pt.exit_reason, pt.days_held, pt.pnl, pt.pnl_pct, s.stop "
    "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
    "WHERE pt.ts_exit IS NOT NULL{extra} "
    "ORDER BY pt.ts_exit"
)


def _row_to_closed(r: sqlite3.Row) -> ClosedTrade:
    return ClosedTrade(
        symbol=r["symbol"],
        ts_entry=r["ts_entry"],
        ts_exit=r["ts_exit"],
        entry_price=r["entry_price"],
        exit_price=r["exit_price"],
        qty=r["qty"],
        exit_reason=r["exit_reason"] or "",
        days_held=r["days_held"] or 0,
        net_pnl=r["pnl"] or 0.0,
        gross_pnl=r["pnl"] or 0.0,
        initial_stop=r["stop"],
        pnl_pct=r["pnl_pct"] or 0.0,
    )


def gather_review_data(conn: sqlite3.Connection, as_of: date) -> ReviewData:
    """Pull the week's + cumulative ledger state from SQLite. Pure read."""
    week_start = as_of - timedelta(days=REVIEW_WINDOW_DAYS)
    closed_week = [
        _row_to_closed(r)
        for r in conn.execute(
            _CLOSED_SQL.format(
                extra=" AND substr(pt.ts_exit, 1, 10) > ? AND substr(pt.ts_exit, 1, 10) <= ?"
            ),
            (week_start.isoformat(), as_of.isoformat()),
        )
    ]
    closed_all = [_row_to_closed(r) for r in conn.execute(_CLOSED_SQL.format(extra=""))]
    open_trades = [
        OpenTradeRow(
            symbol=r["symbol"],
            ts_entry=r["ts_entry"],
            entry_price=r["entry_price"],
            qty=r["qty"],
            current_stop=r["current_stop"],
        )
        for r in conn.execute(
            "SELECT s.symbol, pt.ts_entry, pt.entry_price, pt.qty, pt.current_stop "
            "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
            "WHERE pt.ts_exit IS NULL ORDER BY pt.ts_entry"
        )
    ]
    eq_rows = conn.execute(
        "SELECT date, equity FROM portfolio_snapshots ORDER BY date"
    ).fetchall()
    equity = pd.Series(
        [r["equity"] for r in eq_rows],
        index=pd.to_datetime([r["date"] for r in eq_rows]),
        dtype=float,
    )
    calibration = [
        CalibrationBucket(
            predicted_pct=r["predicted_return_pct"],
            n=r["n"],
            actual_mean_pct=r["avg_actual"],
            realized_hit_rate=r["hit"],
        )
        for r in conn.execute(
            "SELECT predicted_return_pct, COUNT(*) AS n, "
            "       AVG(actual_return_at_horizon) AS avg_actual, "
            "       AVG(CASE WHEN actual_return_at_horizon > 0 THEN 1.0 ELSE 0.0 END) AS hit "
            "FROM predictions WHERE evaluated_at IS NOT NULL "
            "GROUP BY predicted_return_pct ORDER BY predicted_return_pct"
        )
    ]
    return ReviewData(
        week_start=week_start,
        as_of=as_of,
        closed_week=closed_week,
        closed_all=closed_all,
        open_trades=open_trades,
        equity=equity,
        calibration=calibration,
    )


# ---------------------------------------------------------------------------
# Review — rendering
# ---------------------------------------------------------------------------


def _fmt_pf(v: float) -> str:
    return "∞" if math.isinf(v) else f"{v:.2f}"


def _stats_row(label: str, trades: list[ClosedTrade]) -> str:
    if not trades:
        return f"| {label} | 0 | — | — | — | — |"
    return (
        f"| {label} | {len(trades)} | {hit_rate(trades):.0%} "
        f"| {_fmt_pf(profit_factor(trades))} | ₹{expectancy(trades):,.0f} "
        f"| {avg_r_multiple(trades):.2f} |"
    )


def render_weekly_review(data: ReviewData, retrain: RetrainOutcome) -> str:
    """Pure markdown renderer. Empty sources render `_(no data)_`."""
    lines: list[str] = [
        f"# Weekly review — {data.as_of.isoformat()}",
        "",
        f"_Window: {data.week_start.isoformat()} → {data.as_of.isoformat()}_",
        "",
        "## Week's closed trades",
        "",
    ]
    if data.closed_week:
        lines += [
            "| Symbol | Entry | Exit | P&L | P&L % | Reason | Days |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {t.symbol} | {t.entry_price:.2f} | {t.exit_price:.2f} "
            f"| ₹{t.net_pnl:,.0f} | {t.pnl_pct:+.1f}% | {t.exit_reason} | {t.days_held} |"
            for t in data.closed_week
        ]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Stats — week vs cumulative", ""]
    if data.closed_all:
        lines += [
            "| Scope | Trades | Hit rate | Profit factor | Expectancy | Avg R |",
            "|---|---|---|---|---|---|",
            _stats_row("Week", data.closed_week),
            _stats_row("All time", data.closed_all),
        ]
        if len(data.equity) >= 3:
            eq_sharpe = sharpe(data.equity.pct_change().dropna())
            lines += ["", f"Cumulative Sharpe (portfolio snapshots): {eq_sharpe:.2f}"]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Open positions", ""]
    if data.open_trades:
        lines += [
            "| Symbol | Entered | Entry | Qty | Current stop |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {t.symbol} | {t.ts_entry[:10]} | {t.entry_price:.2f} | {t.qty} "
            f"| {t.current_stop if t.current_stop is not None else '—'} |"
            for t in data.open_trades
        ]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Prediction calibration", ""]
    if data.calibration:
        lines += [
            "| Predicted % | N | Mean actual % | Realized hit rate |",
            "|---|---|---|---|",
        ]
        for b in data.calibration:
            actual = f"{b.actual_mean_pct:+.2f}" if b.actual_mean_pct is not None else "—"
            lines.append(
                f"| {b.predicted_pct:+.1f} | {b.n} | {actual} | {b.realized_hit_rate:.0%} |"
            )
        lines += ["", "_Scatter plot: dashboard → Paper Journal → calibration._"]
    else:
        lines.append(_NO_DATA)

    lines += ["", "## Retrain outcome", ""]
    if retrain.ran:
        sharpe_txt = (
            f"{retrain.oos_sharpe:.3f}"
            if retrain.oos_sharpe is not None and not math.isnan(retrain.oos_sharpe)
            else "n/a"
        )
        lines += [
            f"- Trained: yes — {retrain.examples} examples, OOS Sharpe {sharpe_txt}",
            f"- Promoted: {'yes' if retrain.promoted else 'no (deadband held)'}",
            f"- Model: `{retrain.model_path}`",
        ]
    else:
        lines.append(f"- Trained: no — {retrain.skip_reason}")
    lines.append("")
    return "\n".join(lines)
```

(The imports of `notify`, `get_conn`, `run_migrations`, registry functions, ranker_io, and `train_walkforward` are used by Task 4 — leaving them in place now is fine; ruff will not flag them once Task 4 lands, but if you commit Task 3 separately remove the not-yet-used imports here and re-add them in Task 4. Simplest: keep Tasks 3 and 4 as one branch of work and commit after Task 4's tests pass — the commit checkpoints below assume that.)

- [ ] **Step 4: Run tests (with snapshot update on first run)**

Run: `uv run pytest tests/test_jobs_weekly_train.py -v --snapshot-update`
Then: `uv run pytest tests/test_jobs_weekly_train.py -v`
Expected: 5 PASS (no commit yet — Task 4 completes the module)

---

### Task 4: `run_weekly_train` orchestrator

**Files:**
- Modify: `src/trading/jobs/weekly_train.py` (append)
- Test: `tests/test_jobs_weekly_train.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs_weekly_train.py`:

```python
def test_run_skip_train_writes_review(paths, notify_calls) -> None:
    from trading.jobs.weekly_train import run_weekly_train

    result = run_weekly_train(AS_OF, paths=paths, skip_train=True)
    assert result.retrain_ran is False
    assert result.retrain_skip_reason == "skip_train requested"
    assert result.window_end == AS_OF
    assert result.window_start == date(2023, 6, 14)
    assert result.review_path.is_file()
    text = result.review_path.read_text(encoding="utf-8")
    assert text.count("_(no data)_") >= 3
    assert len(notify_calls) == 1
    level, title, _body = notify_calls[0]
    assert level == "info"
    assert "2026-06-14" in title


def test_retrain_guard_skips_duplicate_window(paths, notify_calls, monkeypatch) -> None:
    from trading.jobs import weekly_train as wt
    from trading.store.model_registry import RegistryRow, register

    register(
        paths,
        row=RegistryRow(
            version="2026-06-14",
            trained_at="2026-06-14T05:00:00+00:00",
            train_start="2023-06-14",
            train_end="2026-06-14",
            oos_sharpe=float("nan"),
            oos_hit_rate=float("nan"),
            n_train_examples=40,
            n_features=20,
            path="models/ranker_2026-06-14.pkl",
            active=False,
            notes="",
        ),
        promote=False,
    )
    monkeypatch.setattr(
        wt,
        "load_training_inputs",
        lambda p, c: pytest.fail("guard must short-circuit before loading inputs"),
    )

    result = wt.run_weekly_train(AS_OF, paths=paths)
    assert result.retrain_ran is False
    assert "2026-06-14" in (result.retrain_skip_reason or "")
    assert result.review_path.is_file()


def test_insufficient_data_still_writes_review(paths, notify_calls, monkeypatch) -> None:
    from trading.jobs import weekly_train as wt
    from trading.strategy.ranker_io import TrainingInputs
    from trading.strategy.ranker_train import InsufficientDataError

    monkeypatch.setattr(
        wt,
        "load_training_inputs",
        lambda p, c: TrainingInputs(
            enriched={"RVNL": pd.DataFrame({"close": [1.0]})},
            macro_history=pd.DataFrame(),
            sentiment_lookup={},
        ),
    )

    def _raise(**_kw):
        raise InsufficientDataError("only 3 examples")

    monkeypatch.setattr(wt, "train_walkforward", _raise)

    result = wt.run_weekly_train(AS_OF, paths=paths)
    assert result.retrain_ran is False
    assert "only 3 examples" in (result.retrain_skip_reason or "")
    assert result.review_path.is_file()
    assert len(notify_calls) == 1


def test_retrain_success_registers_and_saves_model(paths, notify_calls, monkeypatch) -> None:
    import lightgbm as lgb

    from trading.jobs import weekly_train as wt
    from trading.store.model_registry import all_rows
    from trading.strategy.ranker_features import FEATURE_NAMES
    from trading.strategy.ranker_io import TrainingInputs
    from trading.strategy.ranker_train import TrainResult

    monkeypatch.setattr(
        wt,
        "load_training_inputs",
        lambda p, c: TrainingInputs(
            enriched={"RVNL": pd.DataFrame({"close": [1.0]})},
            macro_history=pd.DataFrame(),
            sentiment_lookup={},
        ),
    )
    stub = TrainResult(
        folds=(),
        final_model=lgb.LGBMClassifier(),
        final_train_start=pd.Timestamp("2023-06-14"),
        final_train_end=pd.Timestamp("2026-06-14"),
        n_final_examples=42,
        oos_sharpe_mean=float("nan"),
        oos_hit_rate_mean=float("nan"),
        feature_names=FEATURE_NAMES,
    )
    monkeypatch.setattr(wt, "train_walkforward", lambda **_kw: stub)

    result = wt.run_weekly_train(AS_OF, paths=paths)
    assert result.retrain_ran is True
    assert result.examples == 42
    assert result.promoted is False  # NaN sharpe never promotes
    assert result.model_path == "models/ranker_2026-06-14.pkl"
    assert (paths.project_root / "models" / "ranker_2026-06-14.pkl").is_file()
    rows = all_rows(paths)
    assert len(rows) == 1
    assert rows[0].train_end == "2026-06-14"
    assert rows[0].notes == "weekly_train"
    text = result.review_path.read_text(encoding="utf-8")
    assert "Trained: yes — 42 examples" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jobs_weekly_train.py -v`
Expected: 4 new tests FAIL — `ImportError: cannot import name 'run_weekly_train'`

- [ ] **Step 3: Implement the orchestrator**

Append to `src/trading/jobs/weekly_train.py`:

```python
# ---------------------------------------------------------------------------
# Retrain step
# ---------------------------------------------------------------------------


def _step_retrain(
    paths: Paths,
    conn: sqlite3.Connection,
    window_start: date,
    window_end: date,
    skip_train: bool,
) -> RetrainOutcome:
    """Rolling 3y retrain via Phase 16 train_walkforward. Graceful — never
    blocks the review. The registry guard makes Sunday re-runs idempotent."""
    if skip_train:
        return RetrainOutcome(False, "skip_train requested", None, None, False, None)
    end_iso = window_end.isoformat()
    if has_row_for_train_end(paths, end_iso):
        return RetrainOutcome(
            False, f"registry already has a run for {end_iso}", None, None, False, None
        )
    inputs = load_training_inputs(paths, conn)
    if not inputs.enriched:
        return RetrainOutcome(
            False, "no parquet symbols with 200+ bars", None, None, False, None
        )
    try:
        result = train_walkforward(
            enriched=inputs.enriched,
            macro_history=inputs.macro_history,
            sentiment_lookup=inputs.sentiment_lookup,
            negative_news_lookup={},
            start=pd.Timestamp(window_start),
            end=pd.Timestamp(window_end),
        )
    except InsufficientDataError as e:
        logger.warning(f"weekly retrain skipped: {e}")
        return RetrainOutcome(False, str(e), None, None, False, None)

    pkl_rel = f"models/ranker_{end_iso}.pkl"
    save_model(paths.project_root / pkl_rel, result.final_model, FEATURE_NAMES)
    row = RegistryRow(
        version=end_iso,
        trained_at=datetime.now(UTC).isoformat(),
        train_start=str(result.final_train_start.date()),
        train_end=end_iso,
        oos_sharpe=result.oos_sharpe_mean,
        oos_hit_rate=result.oos_hit_rate_mean,
        n_train_examples=result.n_final_examples,
        n_features=len(FEATURE_NAMES),
        path=pkl_rel,
        active=False,
        notes="weekly_train",
    )
    promoted = register(paths, row=row, promote=True)
    return RetrainOutcome(
        True, None, result.n_final_examples, result.oos_sharpe_mean, promoted, pkl_rel
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _today_ist() -> date:
    return datetime.now(_IST).date()


def _slack_body(data: ReviewData, retrain: RetrainOutcome) -> str:
    week_pnl = sum(t.net_pnl for t in data.closed_week)
    lines = [f"Week: {len(data.closed_week)} closed, P&L ₹{week_pnl:,.0f}"]
    if data.closed_all:
        lines.append(
            f"All time: hit {hit_rate(data.closed_all):.0%}, "
            f"PF {_fmt_pf(profit_factor(data.closed_all))}, "
            f"expectancy ₹{expectancy(data.closed_all):,.0f}"
        )
    lines.append(f"Open positions: {len(data.open_trades)}")
    if retrain.ran:
        lines.append(
            f"Retrain: {retrain.examples} examples, "
            f"{'PROMOTED' if retrain.promoted else 'not promoted'}"
        )
    else:
        lines.append(f"Retrain: skipped — {retrain.skip_reason}")
    return "\n".join(lines)


def run_weekly_train(
    as_of: date | None = None,
    *,
    paths: Paths | None = None,
    skip_train: bool = False,
) -> WeeklyTrainResult:
    """Sunday job: rolling 3y retrain (graceful) + weekly review markdown
    + Slack summary. The review is always written, even when the retrain
    is skipped or fails on insufficient data."""
    p = paths if paths is not None else get_paths()
    d = as_of or _today_ist()
    window_start = (pd.Timestamp(d) - pd.DateOffset(years=TRAIN_WINDOW_YEARS)).date()

    with get_conn(p.db_path) as conn:
        run_migrations(conn)
        retrain = _step_retrain(p, conn, window_start, d, skip_train)
        data = gather_review_data(conn, d)

    review_dir = p.research_dir / "weekly"
    review_dir.mkdir(parents=True, exist_ok=True)
    review_path = review_dir / f"{d.isoformat()}_review.md"
    review_path.write_text(render_weekly_review(data, retrain), encoding="utf-8")

    notify("info", f"📊 Weekly review {d.isoformat()}", _slack_body(data, retrain))

    return WeeklyTrainResult(
        as_of=d,
        window_start=window_start,
        window_end=d,
        retrain_ran=retrain.ran,
        retrain_skip_reason=retrain.skip_reason,
        examples=retrain.examples,
        oos_sharpe=retrain.oos_sharpe,
        promoted=retrain.promoted,
        model_path=retrain.model_path,
        review_path=review_path,
    )


def _main(date_str: str = "", skip_train: bool = False) -> None:
    """`python -m trading.jobs.weekly_train [YYYY-MM-DD]` entry."""
    from trading.ops.logging_setup import configure_logging

    configure_logging("weekly_train")
    try:
        result = run_weekly_train(
            date.fromisoformat(date_str) if date_str else None,
            skip_train=skip_train,
        )
    except Exception:
        logger.exception("weekly_train failed")
        raise
    print(f"wrote {result.review_path}")


if __name__ == "__main__":  # pragma: no cover
    import typer

    typer.run(_main)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_jobs_weekly_train.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit (Tasks 3 + 4 together)**

```bash
git add src/trading/jobs/weekly_train.py tests/test_jobs_weekly_train.py tests/__snapshots__/
git commit -m "feat(jobs): weekly_train — Sunday retrain + weekly review (Phase 18)"
```

(If the snapshot file landed somewhere other than `tests/__snapshots__/`, check `git status` and add the generated `.ambr` file wherever syrupy wrote it.)

---

### Task 5: `trading weekly-train` CLI + scheduler XML + launcher

**Files:**
- Modify: `src/trading/cli.py`, `src/trading/jobs/__init__.py`
- Create: `docs/scheduler/trading_weekly_train.xml`, `scripts/weekly_train.bat`
- Test: `tests/test_cli.py` (append)

- [ ] **Step 1: Write the failing CLI test**

Append to `tests/test_cli.py`:

```python
def test_cli_weekly_train_happy(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from trading.cli import app

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("trading.jobs.weekly_train.notify", lambda *a, **kw: None)
    runner = CliRunner()
    result = runner.invoke(app, ["weekly-train", "--date", "2026-06-14", "--skip-train"])
    assert result.exit_code == 0
    assert "weekly_train" in result.output
    assert "skip_train requested" in result.output
```

(If `tests/test_cli.py` already imports `CliRunner`/`app` at module level, drop the local imports and reuse the module-level ones — match the file's existing style.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_cli_weekly_train_happy -v`
Expected: FAIL — exit code 2, "No such command 'weekly-train'"

- [ ] **Step 3: Implement the CLI command**

Add to `src/trading/cli.py` (before the `if __name__ == "__main__":` block):

```python
@app.command("weekly-train")
def weekly_train_cmd(
    date_str: Annotated[
        str, typer.Option("--date", help="Review date YYYY-MM-DD (default: today IST).")
    ] = "",
    skip_train: Annotated[
        bool, typer.Option("--skip-train", help="Skip retraining; write the review only.")
    ] = False,
) -> None:
    """Sunday weekly_train job (Phase 18): rolling retrain + weekly review."""
    from datetime import date as _date

    from trading.jobs.weekly_train import run_weekly_train
    from trading.ops.logging_setup import configure_logging

    configure_logging("weekly_train")
    result = run_weekly_train(
        _date.fromisoformat(date_str) if date_str else None,
        skip_train=skip_train,
    )

    table = Table(title=f"weekly_train — {result.as_of.isoformat()}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("window", f"{result.window_start} → {result.window_end}")
    table.add_row(
        "retrain_ran",
        "yes" if result.retrain_ran else f"no — {result.retrain_skip_reason}",
    )
    table.add_row("examples", str(result.examples) if result.examples is not None else "—")
    table.add_row("promoted", "yes" if result.promoted else "no")
    table.add_row("model", result.model_path or "—")
    table.add_row("review", str(result.review_path))
    console.print(table)
```

- [ ] **Step 4: Export from the jobs package**

In `src/trading/jobs/__init__.py`, add the import and `__all__` entries (keep alphabetical order):

```python
from trading.jobs.weekly_train import WeeklyTrainResult, run_weekly_train
```

and add `"WeeklyTrainResult"` and `"run_weekly_train"` to `__all__`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::test_cli_weekly_train_happy -v`
Expected: PASS

- [ ] **Step 6: Create the scheduler XML**

Create `docs/scheduler/trading_weekly_train.xml` (UTF-8 file is fine; the `encoding="UTF-16"` header matches the existing exported files and Task Scheduler accepts both):

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Phase 18: weekly_train — Sunday retrain + weekly review (runs the job directly, unattended)</Description>
    <Author>trading-bot</Author>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-06-14T10:00:00+05:30</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <Sunday/>
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c cd /d "D:\Projects\Trading" &amp;&amp; uv run trading weekly-train</Arguments>
    </Exec>
  </Actions>
</Task>
```

- [ ] **Step 7: Create the launcher**

Create `scripts/weekly_train.bat`:

```bat
@echo off
REM Phase 18 weekly_train launcher (Sunday retrain + review).
REM Usage: weekly_train.bat [YYYY-MM-DD]
cd /d "%~dp0\.."
if "%~1"=="" (
  uv run trading weekly-train
) else (
  uv run trading weekly-train --date %1
)
```

- [ ] **Step 8: Commit**

```bash
git add src/trading/cli.py src/trading/jobs/__init__.py tests/test_cli.py docs/scheduler/trading_weekly_train.xml scripts/weekly_train.bat
git commit -m "feat(cli): weekly-train command + Sunday scheduler entry (Phase 18)"
```

---

### Task 6: `ReminderSlot.gate_holidays` + `monthly_sip` slot

**Files:**
- Modify: `src/trading/ops/runner.py`
- Modify: `tests/test_ops_runner.py`
- Create: `docs/scheduler/trading_remind_monthly_sip.xml`

- [ ] **Step 1: Update + write tests**

In `tests/test_ops_runner.py`:

Change `test_schedule_has_12_slots` to expect 13:

```python
def test_schedule_has_13_slots():
    from trading.ops.runner import SCHEDULE

    assert len(SCHEDULE) == 13
```

Add `"monthly_sip"` to the `expected` set in `test_schedule_slot_names`.

Append two new tests:

```python
def test_monthly_sip_slot_bypasses_holiday_gate(monkeypatch):
    from trading.ops import runner

    calls = []
    monkeypatch.setattr(runner, "is_trading_day", lambda d: False)
    monkeypatch.setattr(
        runner,
        "notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )
    runner.fire_reminder("monthly_sip", today=date(2026, 8, 1))  # a Saturday
    assert len(calls) == 1
    assert "2026-08-01" in calls[0][2]


def test_gate_holidays_defaults_true():
    from trading.ops.runner import SCHEDULE

    gated = [name for name, slot in SCHEDULE.items() if slot.gate_holidays]
    assert "monthly_sip" not in gated
    assert len(gated) == 12  # every pre-existing slot stays gated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ops_runner.py -v`
Expected: 3 FAIL (count 12→13, KeyError monthly_sip, no gate_holidays attribute)

- [ ] **Step 3: Implement**

In `src/trading/ops/runner.py`:

(a) Add the field to `ReminderSlot`:

```python
@dataclass(frozen=True)
class ReminderSlot:
    """..."""  # keep existing docstring

    when: str  # "HH:MM" IST
    title: str
    body: str = ""
    gate_holidays: bool = True
```

(b) Insert the new slot into `SCHEDULE` **between** `iep_filter` ("09:00") and `mid_day_prepare` ("12:25") so `test_schedule_times_are_sorted` stays green:

```python
    "monthly_sip": ReminderSlot(
        "09:30",
        "\U0001f4b0 Monthly SIP",
        "Run /kite-snapshot, then `trading sip --date <date>`",
        gate_holidays=False,
    ),
```

(c) In `fire_reminder`, change the gate line:

```python
    if spec.gate_holidays and not is_trading_day(today):
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ops_runner.py -v`
Expected: all PASS

- [ ] **Step 5: Create the monthly scheduler XML**

Create `docs/scheduler/trading_remind_monthly_sip.xml`:

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Phase 18 reminder slot: monthly_sip (1st of month, fires even on holidays)</Description>
    <Author>trading-bot</Author>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-07-01T09:30:00+05:30</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth>
          <Day>1</Day>
        </DaysOfMonth>
        <Months>
          <January/><February/><March/><April/><May/><June/>
          <July/><August/><September/><October/><November/><December/>
        </Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c cd /d "D:\Projects\Trading" &amp;&amp; uv run trading remind --slot monthly_sip</Arguments>
    </Exec>
  </Actions>
</Task>
```

- [ ] **Step 6: Commit**

```bash
git add src/trading/ops/runner.py tests/test_ops_runner.py docs/scheduler/trading_remind_monthly_sip.xml
git commit -m "feat(ops): gate_holidays flag + monthly_sip reminder slot (Phase 18)"
```

---

### Task 7: monthly_sip — window + candidate gathering + health

**Files:**
- Create: `src/trading/jobs/monthly_sip.py` (data half)
- Test: `tests/test_jobs_monthly_sip.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jobs_monthly_sip.py`:

```python
"""Tests for trading.jobs.monthly_sip."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.store.migrations import run_migrations
from trading.store.ohlcv import write_ohlcv

AS_OF = date(2026, 7, 1)  # a Wednesday


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


@pytest.fixture
def weekday_calendar(monkeypatch: pytest.MonkeyPatch):
    """Deterministic, offline trading-day calendar (no holiday fetch)."""
    monkeypatch.setattr(
        "trading.jobs.monthly_sip.is_trading_day", lambda d: d.weekday() < 5
    )


@pytest.fixture
def notify_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "trading.jobs.monthly_sip.notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )
    return calls


def _frame(n: int) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    idx.name = "date"
    closes = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def _holding_row(
    symbol: str, qty: int = 10, last_price: float = 100.0, avg: float = 90.0
) -> dict:
    return {
        "tradingsymbol": symbol,
        "exchange": "NSE",
        "isin": None,
        "quantity": qty,
        "average_price": avg,
        "last_price": last_price,
        "close_price": last_price,
        "pnl": (last_price - avg) * qty,
        "day_change": 0.0,
        "day_change_percentage": 0.0,
    }


def _memdb() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


def _insert_signal(
    conn: sqlite3.Connection, ts: str, symbol: str, ml_score: float | None
) -> None:
    conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, "
        "ml_score, created_by) VALUES (?, ?, 'LONG', 100.0, 95.0, 120.0, 25, ?, 'test')",
        (ts, symbol, ml_score),
    )


def test_trailing_trading_window(weekday_calendar) -> None:
    from trading.jobs.monthly_sip import trailing_trading_window

    oldest, newest = trailing_trading_window(AS_OF)
    assert newest == AS_OF
    assert oldest == date(2026, 6, 18)  # 10 weekdays back, inclusive of as_of


def test_gather_candidates_window_priority_and_health(paths, weekday_calendar) -> None:
    from trading.jobs.monthly_sip import gather_candidates

    write_ohlcv(_frame(250), "COALINDIA", paths)
    write_ohlcv(_frame(250), "CANDSYM", paths)
    conn = _memdb()
    _insert_signal(conn, "2026-06-25T08:30:00", "COALINDIA", 0.5)
    _insert_signal(conn, "2026-06-25T08:30:00", "CANDSYM", None)  # NULL → priority 0
    _insert_signal(conn, "2026-06-01T08:30:00", "OLDSYM", 0.9)  # outside window
    _insert_signal(conn, "2026-06-26T08:30:00", "NOPARQUET", 0.8)  # no parquet → dropped

    warnings: list[str] = []
    cands = gather_candidates(
        conn,
        paths,
        AS_OF,
        window=(date(2026, 6, 18), AS_OF),
        sector_map={"COALINDIA": "METAL"},
        held={"COALINDIA"},
        verdicts={"COALINDIA": "HOLD"},
        warnings=warnings,
    )
    by_sym = {c.symbol: c for c in cands}
    assert set(by_sym) == {"COALINDIA", "CANDSYM"}
    assert by_sym["COALINDIA"].health == "HOLD"
    assert by_sym["COALINDIA"].sector == "METAL"
    assert by_sym["COALINDIA"].priority == 0.5
    assert by_sym["CANDSYM"].health is None  # not held → NEW bucket
    assert by_sym["CANDSYM"].sector == "UNKNOWN"
    assert by_sym["CANDSYM"].priority == 0.0
    assert any("NOPARQUET" in w for w in warnings)


def test_score_holdings_skips_missing_parquet(paths) -> None:
    from trading.data.kite import Holding
    from trading.jobs.monthly_sip import _score_holdings

    write_ohlcv(_frame(250), "COALINDIA", paths)
    holdings = [
        Holding(**_holding_row("COALINDIA", qty=20, last_price=124.0, avg=100.0)),
        Holding(**_holding_row("GHOST")),
    ]
    warnings: list[str] = []
    verdicts = _score_holdings(paths, holdings, warnings)
    assert set(verdicts) == {"COALINDIA"}
    assert verdicts["COALINDIA"] in ("HOLD", "TRIM", "EXIT")
    assert any("GHOST" in w for w in warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jobs_monthly_sip.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.jobs.monthly_sip'`

- [ ] **Step 3: Implement the data half**

Create `src/trading/jobs/monthly_sip.py`:

```python
"""Phase 18 — monthly_sip job: 1st-of-month ₹1L SIP allocation plan.

Spec: docs/superpowers/specs/2026-06-11-phase-18-support-tooling-design.md
Reminder-driven: the user runs /kite-snapshot, then `trading sip --date`.
The plan is a markdown menu (data/research/<date>/sip_plan.md) the user
executes manually over the month — no orders are placed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from trading.config import Paths, get_paths
from trading.data.kite import Holding
from trading.data.kite_snapshot import (
    KiteSnapshotMissingError,
    KiteSnapshotStaleError,
    read_holdings,
)
from trading.data.sector import load_sector_map
from trading.ops.calendar import is_trading_day
from trading.ops.notify import notify
from trading.portfolio.allocator import (
    HoldingSnapshot,
    SipCandidate,
    SipPlan,
    allocate_sip,
)
from trading.portfolio.health import (
    FundamentalsSnapshot,
    HoldingContext,
    SentimentSnapshot,
    Verdict,
    score_holding,
    technicals_from_history,
)
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.store.ohlcv import read_ohlcv

CANDIDATE_WINDOW_TRADING_DAYS = 10
DEFAULT_SIP_BUDGET = 100_000.0
SECTOR_WARN_PCT = 0.30
UNKNOWN_SECTOR = "UNKNOWN"
_MAX_LOOKBACK_CALENDAR_DAYS = 40


class MonthlySipAborted(RuntimeError):  # noqa: N818 — "Aborted" is a state
    """Missing/stale Kite snapshot — /kite-snapshot must run first."""


@dataclass(frozen=True)
class MonthlySipResult:
    as_of: date
    budget: float
    holdings_count: int
    candidates_considered: int
    deployed: float
    cash_reserve: float
    allocations: int  # non-CASH allocation lines
    plan_path: Path | None  # None on dry_run
    warnings: list[str]


def trailing_trading_window(
    as_of: date, n: int = CANDIDATE_WINDOW_TRADING_DAYS
) -> tuple[date, date]:
    """(oldest, as_of): the last `n` trading days ending at and including
    `as_of` (when `as_of` itself is a trading day). Bounded at 40 calendar
    days so a broken calendar can't loop forever."""
    found: list[date] = []
    d = as_of
    while len(found) < n and (as_of - d).days <= _MAX_LOOKBACK_CALENDAR_DAYS:
        if is_trading_day(d):
            found.append(d)
        d = d - timedelta(days=1)
    oldest = found[-1] if found else as_of
    return oldest, as_of


def _score_holdings(
    paths: Paths, holdings: list[Holding], warnings: list[str]
) -> dict[str, Verdict]:
    """HOLD/TRIM/EXIT per holding — same scoring path pre_open uses
    (enriched parquet technicals; fundamentals/sentiment default-empty)."""
    verdicts: dict[str, Verdict] = {}
    for h in holdings:
        try:
            history = read_ohlcv(h.tradingsymbol, paths)
        except FileNotFoundError:
            warnings.append(f"no parquet for holding {h.tradingsymbol} — health unknown")
            continue
        ctx = HoldingContext(
            symbol=h.tradingsymbol,
            qty=h.quantity,
            avg_price=h.average_price,
            last_price=h.last_price,
            technicals=technicals_from_history(history),
            fundamentals=FundamentalsSnapshot(),
            sentiment=SentimentSnapshot(),
        )
        verdicts[h.tradingsymbol] = score_holding(ctx).verdict
    return verdicts


def gather_candidates(
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    *,
    window: tuple[date, date],
    sector_map: dict[str, str],
    held: set[str],
    verdicts: dict[str, Verdict],
    warnings: list[str],
) -> list[SipCandidate]:
    """Distinct symbols with a signals row inside `window`. Priority =
    max ml_score (NULL → 0), entry = latest parquet close ≤ as_of."""
    oldest, newest = window
    rows = conn.execute(
        "SELECT symbol, MAX(COALESCE(ml_score, 0.0)) AS priority "
        "FROM signals "
        "WHERE substr(ts, 1, 10) >= ? AND substr(ts, 1, 10) <= ? "
        "GROUP BY symbol ORDER BY symbol",
        (oldest.isoformat(), newest.isoformat()),
    ).fetchall()
    out: list[SipCandidate] = []
    for r in rows:
        symbol = r["symbol"]
        try:
            df = read_ohlcv(symbol, paths, end=as_of)
        except FileNotFoundError:
            warnings.append(f"candidate {symbol}: no parquet — dropped")
            continue
        if df.empty:
            warnings.append(f"candidate {symbol}: no history ≤ {as_of} — dropped")
            continue
        out.append(
            SipCandidate(
                symbol=symbol,
                sector=sector_map.get(symbol, UNKNOWN_SECTOR),
                entry_price=float(df["close"].iloc[-1]),
                health=verdicts.get(symbol) if symbol in held else None,
                priority=float(r["priority"]),
            )
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_jobs_monthly_sip.py -v`
Expected: 3 PASS (no commit yet — Task 8 completes the module)

---

### Task 8: monthly_sip — renderer + `run_monthly_sip`

**Files:**
- Modify: `src/trading/jobs/monthly_sip.py` (append)
- Test: `tests/test_jobs_monthly_sip.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs_monthly_sip.py`:

```python
def _write_sector_map(paths, rows: dict[str, str]) -> None:
    p = paths.project_root / "data" / "static"
    p.mkdir(parents=True, exist_ok=True)
    body = "symbol,sector\n" + "\n".join(f"{s},{sec}" for s, sec in rows.items())
    (p / "sector_map.csv").write_text(body + "\n", encoding="utf-8")


def _seed_db(paths) -> None:
    from trading.store.db import get_conn

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        _insert_signal(conn, "2026-06-25T08:30:00", "CANDSYM", 0.7)
        _insert_signal(conn, "2026-06-01T08:30:00", "OLDSYM", 0.9)  # outside window
        conn.commit()


def test_aborts_without_kite_snapshot(paths, weekday_calendar) -> None:
    from trading.jobs.monthly_sip import MonthlySipAborted, run_monthly_sip

    with pytest.raises(MonthlySipAborted):
        run_monthly_sip(AS_OF, paths=paths)


def test_happy_path_writes_plan_and_notifies(
    paths, weekday_calendar, notify_calls, snapshot
) -> None:
    from trading.jobs.monthly_sip import run_monthly_sip

    seed_kite_snapshot(
        paths,
        AS_OF,
        holdings=[_holding_row("COALINDIA", qty=20, last_price=460.0, avg=400.0)],
        gtts=[],
    )
    write_ohlcv(_frame(250), "COALINDIA", paths)
    write_ohlcv(_frame(250), "CANDSYM", paths)
    _write_sector_map(paths, {"COALINDIA": "METAL", "CANDSYM": "AUTO"})
    _seed_db(paths)

    result = run_monthly_sip(AS_OF, paths=paths)
    assert result.holdings_count == 1
    assert result.candidates_considered == 1  # CANDSYM in window; OLDSYM outside
    assert result.deployed > 0
    assert result.plan_path is not None and result.plan_path.is_file()

    text = result.plan_path.read_text(encoding="utf-8")
    assert "# SIP plan — 2026-07-01" in text
    assert "CANDSYM" in text
    assert "## Post-plan sector weights" in text
    assert "⚠️" in text  # METAL holding dominates → >30% flag
    assert text == snapshot

    assert len(notify_calls) == 1
    level, title, body = notify_calls[0]
    assert level == "info"
    assert "2026-07-01" in title
    assert "Deployed" in body


def test_dry_run_writes_nothing(paths, weekday_calendar, notify_calls) -> None:
    from trading.jobs.monthly_sip import run_monthly_sip

    seed_kite_snapshot(paths, AS_OF, holdings=[_holding_row("COALINDIA")], gtts=[])
    write_ohlcv(_frame(250), "COALINDIA", paths)

    result = run_monthly_sip(AS_OF, paths=paths, dry_run=True)
    assert result.plan_path is None
    assert notify_calls == []
    assert not (paths.research_dir / "2026-07-01" / "sip_plan.md").exists()


def test_unmapped_symbols_get_unknown_sector(paths, weekday_calendar, notify_calls) -> None:
    """No sector_map.csv at all → everything lands in UNKNOWN, job still works."""
    from trading.jobs.monthly_sip import run_monthly_sip

    seed_kite_snapshot(paths, AS_OF, holdings=[_holding_row("COALINDIA")], gtts=[])
    write_ohlcv(_frame(250), "COALINDIA", paths)

    result = run_monthly_sip(AS_OF, paths=paths)
    assert result.plan_path is not None
    assert "UNKNOWN" in result.plan_path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jobs_monthly_sip.py -v`
Expected: 4 new FAIL — `ImportError: cannot import name 'run_monthly_sip'`

- [ ] **Step 3: Implement renderer + orchestrator**

Append to `src/trading/jobs/monthly_sip.py`:

```python
# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def sector_weights_after_plan(
    holdings: list[HoldingSnapshot],
    plan: SipPlan,
    symbol_sector: dict[str, str],
) -> list[tuple[str, float, float]]:
    """[(sector, value, weight)] for the post-plan portfolio, weight desc.
    Spec §7.4 concentration warning input."""
    by_sector: dict[str, float] = {}
    for h in holdings:
        by_sector[h.sector] = by_sector.get(h.sector, 0.0) + h.current_value
    for a in plan.allocations:
        if a.action in ("TOPUP", "NEW") and a.symbol:
            sec = symbol_sector.get(a.symbol, UNKNOWN_SECTOR)
            by_sector[sec] = by_sector.get(sec, 0.0) + a.amount
    total = sum(by_sector.values())
    if total <= 0:
        return []
    return sorted(
        ((sec, val, val / total) for sec, val in by_sector.items()),
        key=lambda t: t[2],
        reverse=True,
    )


def render_sip_plan(
    as_of: date,
    budget: float,
    plan: SipPlan,
    weights: list[tuple[str, float, float]],
    holdings_count: int,
    window: tuple[date, date],
) -> str:
    """Pure markdown renderer for sip_plan.md."""
    lines: list[str] = [
        f"# SIP plan — {as_of.isoformat()}",
        "",
        f"_Budget ₹{budget:,.0f} · deployed ₹{plan.deployed:,.0f} · "
        f"cash reserve ₹{plan.cash_reserve:,.0f}_",
        "",
        "## Allocations",
        "",
    ]
    if plan.allocations:
        lines += ["| Action | Symbol | Amount | Rationale |", "|---|---|---|---|"]
        lines += [
            f"| {a.action} | {a.symbol or '—'} | ₹{a.amount:,.0f} | {a.rationale} |"
            for a in plan.allocations
        ]
    else:
        lines.append("_(no allocations)_")

    lines += ["", "## Skipped", ""]
    if plan.skipped:
        lines += ["| Symbol | Reason |", "|---|---|"]
        lines += [f"| {sym} | {reason} |" for sym, reason in plan.skipped]
    else:
        lines.append("_(none)_")

    lines += ["", "## Post-plan sector weights", ""]
    if weights:
        lines += ["| Sector | Value | Weight | Flag |", "|---|---|---|---|"]
        lines += [
            f"| {sec} | ₹{val:,.0f} | {w:.0%} "
            f"| {'⚠️ over 30%' if w > SECTOR_WARN_PCT else ''} |"
            for sec, val, w in weights
        ]
    else:
        lines.append("_(no data)_")

    lines += [
        "",
        f"_Inputs: {holdings_count} holdings; candidate window "
        f"{window[0].isoformat()} → {window[1].isoformat()} "
        f"({CANDIDATE_WINDOW_TRADING_DAYS} trading days)._",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_monthly_sip(
    as_of: date,
    *,
    paths: Paths | None = None,
    budget: float = DEFAULT_SIP_BUDGET,
    dry_run: bool = False,
) -> MonthlySipResult:
    """Compute and persist the month's SIP plan.

    Hard dependency: today's Kite snapshot (raises MonthlySipAborted).
    Everything else degrades gracefully into `warnings`.
    """
    p = paths if paths is not None else get_paths()
    warnings: list[str] = []

    try:
        holdings = read_holdings(p, as_of)
    except (KiteSnapshotMissingError, KiteSnapshotStaleError) as e:
        raise MonthlySipAborted(str(e)) from e

    sector_map = load_sector_map(p)
    held = {h.tradingsymbol for h in holdings}
    verdicts = _score_holdings(p, holdings, warnings)
    holding_snaps = [
        HoldingSnapshot(
            symbol=h.tradingsymbol,
            sector=sector_map.get(h.tradingsymbol, UNKNOWN_SECTOR),
            current_value=h.quantity * h.last_price,
        )
        for h in holdings
    ]

    window = trailing_trading_window(as_of)
    with get_conn(p.db_path) as conn:
        run_migrations(conn)
        candidates = gather_candidates(
            conn,
            p,
            as_of,
            window=window,
            sector_map=sector_map,
            held=held,
            verdicts=verdicts,
            warnings=warnings,
        )

    plan = allocate_sip(candidates, holding_snaps, budget=budget)
    weights = sector_weights_after_plan(holding_snaps, plan, sector_map)

    plan_path: Path | None = None
    if not dry_run:
        out_dir = p.research_dir / as_of.isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)
        plan_path = out_dir / "sip_plan.md"
        plan_path.write_text(
            render_sip_plan(as_of, budget, plan, weights, len(holdings), window),
            encoding="utf-8",
        )
        top = [a for a in plan.allocations if a.action != "CASH"][:3]
        top_txt = ", ".join(f"{a.symbol} ₹{a.amount:,.0f}" for a in top) or "none"
        notify(
            "info",
            f"💰 SIP plan {as_of.isoformat()}",
            f"Deployed ₹{plan.deployed:,.0f} of ₹{budget:,.0f} "
            f"(cash ₹{plan.cash_reserve:,.0f})\nTop: {top_txt}",
        )

    return MonthlySipResult(
        as_of=as_of,
        budget=budget,
        holdings_count=len(holdings),
        candidates_considered=len(candidates),
        deployed=plan.deployed,
        cash_reserve=plan.cash_reserve,
        allocations=sum(1 for a in plan.allocations if a.action != "CASH"),
        plan_path=plan_path,
        warnings=warnings,
    )
```

- [ ] **Step 4: Run tests (snapshot update first)**

Run: `uv run pytest tests/test_jobs_monthly_sip.py -v --snapshot-update`
Then: `uv run pytest tests/test_jobs_monthly_sip.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit (Tasks 7 + 8 together)**

```bash
git add src/trading/jobs/monthly_sip.py tests/test_jobs_monthly_sip.py tests/__snapshots__/
git commit -m "feat(jobs): monthly_sip — SIP plan job (Phase 18)"
```

---

### Task 9: `trading sip` CLI

**Files:**
- Modify: `src/trading/cli.py`, `src/trading/jobs/__init__.py`
- Test: `tests/test_cli.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_cli_sip_aborts_without_snapshot(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from trading.cli import app

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "trading.jobs.monthly_sip.is_trading_day", lambda d: d.weekday() < 5
    )
    runner = CliRunner()
    result = runner.invoke(app, ["sip", "--date", "2026-07-01"])
    assert result.exit_code == 2
    assert "kite-snapshot" in result.output


def test_cli_sip_happy(tmp_path, monkeypatch) -> None:
    from datetime import date as _date

    from typer.testing import CliRunner

    from conftest import seed_kite_snapshot
    from trading.cli import app
    from trading.config import get_paths

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "trading.jobs.monthly_sip.is_trading_day", lambda d: d.weekday() < 5
    )
    monkeypatch.setattr("trading.jobs.monthly_sip.notify", lambda *a, **kw: None)
    paths = get_paths()
    seed_kite_snapshot(
        paths,
        _date(2026, 7, 1),
        holdings=[
            {
                "tradingsymbol": "COALINDIA",
                "exchange": "NSE",
                "isin": None,
                "quantity": 20,
                "average_price": 400.0,
                "last_price": 460.0,
                "close_price": 460.0,
                "pnl": 1200.0,
                "day_change": 0.0,
                "day_change_percentage": 0.0,
            }
        ],
        gtts=[],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["sip", "--date", "2026-07-01"])
    assert result.exit_code == 0
    assert "monthly_sip" in result.output
    assert (paths.research_dir / "2026-07-01" / "sip_plan.md").is_file()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_cli_sip_aborts_without_snapshot tests/test_cli.py::test_cli_sip_happy -v`
Expected: FAIL — "No such command 'sip'"

- [ ] **Step 3: Implement the CLI command**

Add to `src/trading/cli.py` (after the `weekly_train_cmd` from Task 5):

```python
@app.command("sip")
def sip_cmd(
    date_str: Annotated[str, typer.Option("--date", help="Plan date (YYYY-MM-DD).")],
    budget: Annotated[
        float, typer.Option("--budget", help="Monthly SIP budget in ₹.")
    ] = 100_000.0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print only — no file write, no Slack.")
    ] = False,
) -> None:
    """Monthly ₹1L SIP allocation plan (Phase 18). Needs today's /kite-snapshot."""
    from datetime import date as _date

    from trading.jobs.monthly_sip import MonthlySipAborted, run_monthly_sip
    from trading.ops.logging_setup import configure_logging

    configure_logging("monthly_sip")
    try:
        result = run_monthly_sip(
            _date.fromisoformat(date_str), budget=budget, dry_run=dry_run
        )
    except MonthlySipAborted as e:
        console.print(f"[red]sip aborted:[/red] {e}")
        console.print("Run /kite-snapshot in Claude Code first, then retry.")
        raise typer.Exit(code=2) from e

    table = Table(title=f"monthly_sip — {result.as_of.isoformat()}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("budget", f"₹{result.budget:,.0f}")
    table.add_row("holdings", str(result.holdings_count))
    table.add_row("candidates", str(result.candidates_considered))
    table.add_row("deployed", f"₹{result.deployed:,.0f}")
    table.add_row("cash_reserve", f"₹{result.cash_reserve:,.0f}")
    table.add_row("allocations", str(result.allocations))
    table.add_row("plan", str(result.plan_path) if result.plan_path else "— (dry run)")
    console.print(table)
    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")
```

- [ ] **Step 4: Export from the jobs package**

In `src/trading/jobs/__init__.py`, add:

```python
from trading.jobs.monthly_sip import (
    MonthlySipAborted,
    MonthlySipResult,
    run_monthly_sip,
)
```

and add `"MonthlySipAborted"`, `"MonthlySipResult"`, `"run_monthly_sip"` to `__all__` (alphabetical).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS (existing + 3 new across Tasks 5/9)

- [ ] **Step 6: Commit**

```bash
git add src/trading/cli.py src/trading/jobs/__init__.py tests/test_cli.py
git commit -m "feat(cli): sip command — monthly SIP plan (Phase 18)"
```

---

### Task 10: Full verification + PROGRESS.md + push

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Run the full gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest -q
```

Expected: ruff clean, mypy clean, full suite green (657+ passed, 1 skipped live). Fix anything that fails before proceeding — common candidates: unused imports flagged by ruff in `cli.py` after the Task 1 refactor, line-length on long SQL strings (wrap them), mypy `sqlite3.Row` indexing (already used elsewhere in the repo — follow existing patterns).

- [ ] **Step 2: Manual smoke (optional but recommended)**

```bash
uv run trading weekly-train --date 2026-06-14 --skip-train
uv run trading sip --date 2026-06-11 --dry-run
```

Expected: weekly review written to `data/research/weekly/2026-06-14_review.md` with real ledger data (0 closed trades → placeholders); sip dry-run prints the table using today's real snapshot (2026-06-11 exists in `data/raw/`).

- [ ] **Step 3: Update PROGRESS.md**

In the Phase 18 section, replace the 18.3 and 18.4 lines:

```markdown
- [ ] 18.3 Weekly LightGBM retrain — **tooling shipped** (`trading
       weekly-train`, Sunday Task Scheduler XML, registry guard +
       soft-promotion gate; spec 2026-06-11). Ongoing: import
       `docs/scheduler/trading_weekly_train.xml`, observe Sunday runs.
- [ ] 18.4 Monthly SIP allocator dry-run — **tooling shipped** (`trading
       sip`, monthly_sip reminder slot with gate_holidays=False; spec
       2026-06-11). Ongoing: run on the 1st, compare vs actual decisions.
```

(Keep them `[ ]` — the *ongoing* monthly/weekly practice is what 18.3/18.4 track; the blocker note is gone.)

Also update the snapshot fields:

```markdown
**Currently working on:** _Phase 18 — live run day 1 done; weekly_train + monthly_sip support tooling shipped (spec + plan 2026-06-11)_
**Next up:** _Import the 2 new Task Scheduler XMLs; weekly cadence per 18.2-18.4_
```

- [ ] **Step 4: Commit + push**

```bash
git add PROGRESS.md
git commit -m "docs: PROGRESS — Phase 18 support tooling shipped"
git push origin main
```

---

## Self-review notes (already applied)

- **Spec coverage:** hybrid execution (T5 XML direct / T6 reminder), weekly retrain + guard + graceful InsufficientDataError (T4), review markdown + placeholders + Slack (T3/T4), `--skip-train` (T4/T5), bat launcher (T5), SIP abort/health/window/allocate/render/notify (T7/T8), `--budget`/`--dry-run` (T9), `gate_holidays` (T6), all spec §5 tests present (T1-T9), gates + push (T10).
- **Type consistency:** `gather_candidates(conn, paths, as_of, *, window, sector_map, held, verdicts, warnings)` is identical in T7 implementation, T7 test, and T8 orchestrator call. `RetrainOutcome` defined in T3, constructed in T4. `TrainingInputs` from T1 used in T4 tests.
- **Known judgment calls:** `MonthlySipResult.warnings` added beyond the spec's field list (matches `PreOpenResult` convention); calibration buckets group by exact `predicted_return_pct` (auto-open writes a constant 20.0 today — ranged buckets would be empty theater); weekly review Sharpe needs ≥3 portfolio snapshots before it renders.
