# Phase 13 — pre_open Job (MVP): Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `pre_open` job — a single command that runs every upstream phase in dependency order, auto-opens paper-trades for all-pass signals, writes the Phase 12 context bundle, and halts for the user to invoke `/analyst`.

**Architecture:** In-process orchestrator (`src/trading/jobs/pre_open.py`) with one private `_step_*` function per upstream phase + a typed `PreOpenResult` for tests/CLI. Kite-dependent steps degrade gracefully when token absent. Idempotency guard prevents duplicate paper-trades on re-run.

**Tech Stack:** Python 3.11 · sqlite3 · `typer` (CLI) · `pytest` · existing project modules (Phases 1–12).

**Spec:** [`docs/superpowers/specs/2026-05-15-phase-13-pre-open-design.md`](../specs/2026-05-15-phase-13-pre-open-design.md)

---

## File structure

| Path | Created/Modified | Responsibility |
|------|------------------|----------------|
| `src/trading/jobs/__init__.py` | Modify (currently empty) | Re-export `run_pre_open`, `PreOpenResult` |
| `src/trading/jobs/pre_open.py` | Create | `PreOpenResult` dataclass + `run_pre_open(as_of, ...)` orchestrator + 6 private `_step_*` helpers + `_main` typer entry |
| `src/trading/cli.py` | Modify | Add `trading pre-open` subcommand wrapping `run_pre_open` |
| `tests/test_jobs_pre_open.py` | Create | Unit tests per `_step_*` (mocked upstream) + 1 integration test |
| `scripts/pre_open.bat` | Create | Windows launcher invoking `uv run python -m trading.jobs.pre_open` |
| `PROGRESS.md` | Modify | Mark Phase 13 sub-tasks `[x]`; update pointers |

---

## Task 1: Stub `jobs/pre_open.py` with `PreOpenResult` + skeleton

**Files:**
- Modify: `src/trading/jobs/__init__.py`
- Create: `src/trading/jobs/pre_open.py`
- Create: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jobs_pre_open.py
"""Tests for trading.jobs.pre_open — orchestrator + each _step_*."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.jobs.pre_open import PreOpenResult, run_pre_open
from trading.store.migrations import run_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def test_run_pre_open_returns_result_with_bundle_path(
    paths, monkeypatch
) -> None:
    """Skeleton: orchestrator returns a PreOpenResult and writes a bundle.

    Stub every upstream call so the test runs offline. Subsequent tasks
    fill in the real wiring per step.
    """
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_macro",
        lambda conn, as_of, warnings: (False, "NEUTRAL"),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_news",
        lambda conn, as_of, warnings: (0, 0),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_scan",
        lambda paths, as_of, warnings: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_portfolio",
        lambda paths, settings, warnings, skip_kite: [],
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_auto_open",
        lambda conn, as_of, passing, regime, capital, risk_pct, warnings: 0,
    )

    result = run_pre_open(
        date(2026, 5, 15),
        paths=paths,
        skip_news=True,
        skip_kite=True,
    )
    assert isinstance(result, PreOpenResult)
    assert result.as_of == date(2026, 5, 15)
    assert result.bundle_path == paths.research_dir / "2026-05-15" / "_context.md"
    assert result.bundle_path.is_file()
    assert result.candidates_passing == 0
    assert result.paper_trades_opened == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.jobs.pre_open'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/jobs/pre_open.py
"""Phase 13 — pre_open MVP orchestrator.

Runs each upstream phase in dependency order, auto-opens paper-trades
for all-pass signals, writes the Phase 12 context bundle, halts. The
per-step helpers stay private so the orchestrator's body reads as a
narrative. Each `_step_*` either returns a typed result or appends to
`warnings` on graceful-degradation paths.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from trading.config import Paths, Settings, get_paths, get_settings
from trading.features.regime import Regime
from trading.llm.context import ContextInputs, assemble_context
from trading.portfolio.health import HealthScore
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.strategy.rules import Candidate, passing


@dataclass(frozen=True)
class PreOpenResult:
    """What pre_open produced. Returned by `run_pre_open` for tests + CLI."""

    as_of: date
    bundle_path: Path
    macro_written: bool
    news_inserted: int
    sentiment_rows: int
    candidates_total: int
    candidates_passing: int
    paper_trades_opened: int
    holdings_scored: int
    warnings: list[str] = field(default_factory=list)


def run_pre_open(
    as_of: date,
    *,
    paths: Paths | None = None,
    settings: Settings | None = None,
    skip_news: bool = False,
    skip_kite: bool = False,
    capital_per_trade: float = 100_000.0,
    risk_pct: float = 0.02,
) -> PreOpenResult:
    """Orchestrate Phases 1–12 for `as_of` and write the analyst bundle.

    Each step runs in dependency order. Failures in graceful-degradation
    steps (macro, news, portfolio) are collected as warnings; the bundle
    is written either way. Auto-opened paper-trades use D-1 close as
    entry price (the most recent bar in the parquet).
    """
    p = paths if paths is not None else get_paths()
    s = settings if settings is not None else get_settings()
    warnings: list[str] = []

    with get_conn(p.db_path) as conn:
        run_migrations(conn)

        macro_written, regime = _step_macro(conn, as_of, warnings)

        if skip_news:
            news_inserted, sentiment_rows = 0, 0
            warnings.append("skip_news=True — news ingest skipped")
        else:
            news_inserted, sentiment_rows = _step_news(conn, as_of, warnings)

        candidates = _step_scan(p, as_of, warnings)
        passing_candidates = passing(candidates)

        holdings = _step_portfolio(p, s, warnings, skip_kite=skip_kite)

        opened = _step_auto_open(
            conn, as_of, passing_candidates, regime,
            capital_per_trade, risk_pct, warnings,
        )

        bundle_path = _step_assemble(
            conn, p, as_of, candidates, holdings,
        )

    return PreOpenResult(
        as_of=as_of,
        bundle_path=bundle_path,
        macro_written=macro_written,
        news_inserted=news_inserted,
        sentiment_rows=sentiment_rows,
        candidates_total=len(candidates),
        candidates_passing=len(passing_candidates),
        paper_trades_opened=opened,
        holdings_scored=len(holdings),
        warnings=warnings,
    )


def _step_macro(
    conn: sqlite3.Connection, as_of: date, warnings: list[str]
) -> tuple[bool, Regime]:
    """Stub — Task 2 wires snapshot_and_classify."""
    return False, "NEUTRAL"


def _step_news(
    conn: sqlite3.Connection, as_of: date, warnings: list[str]
) -> tuple[int, int]:
    """Stub — Task 3 wires fetch_all_news + score + aggregate."""
    return 0, 0


def _step_scan(
    paths: Paths, as_of: date, warnings: list[str]
) -> list[Candidate]:
    """Stub — Task 4 wires the scanner."""
    return []


def _step_portfolio(
    paths: Paths,
    settings: Settings,
    warnings: list[str],
    *,
    skip_kite: bool,
) -> list[HealthScore]:
    """Stub — Task 5 wires Kite holdings + score_holding."""
    return []


def _step_auto_open(
    conn: sqlite3.Connection,
    as_of: date,
    passing: list[Candidate],
    regime: Regime,
    capital: float,
    risk_pct: float,
    warnings: list[str],
) -> int:
    """Stub — Task 6 wires sizing + log_signal_and_open_trade."""
    return 0


def _step_assemble(
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    candidates: list[Candidate],
    holdings: list[HealthScore],
) -> Path:
    """Render the input bundle. Real wiring; no upstream calls."""
    return assemble_context(
        conn=conn, paths=paths, as_of=as_of, mode="pre_open",
        inputs=ContextInputs(candidates=candidates, holdings_health=holdings),
    )
```

```python
# src/trading/jobs/__init__.py
"""Top-level jobs package — orchestrators that wire phases together."""

from trading.jobs.pre_open import PreOpenResult, run_pre_open

__all__ = ["PreOpenResult", "run_pre_open"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/__init__.py src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): pre_open skeleton + PreOpenResult + step stubs (13.1.a)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Wire `_step_macro`

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs_pre_open.py`:

```python
from unittest.mock import patch

from trading.data.macro import MacroSnapshot
from trading.features.regime import RegimeResult
from trading.jobs.pre_open import _step_macro


def test_step_macro_writes_snapshot_and_returns_regime(
    conn: sqlite3.Connection,
) -> None:
    snap = MacroSnapshot(
        date=date(2026, 5, 15), sgx_nifty=None, dow_fut=None,
        nasdaq_fut=None, sp500=None, usdinr=95.0, crude=None,
        vix=18.0, us_10y=None, fii_flow_cr=200.0, dii_flow_cr=500.0,
        regime="RISK_ON",
    )
    rr = RegimeResult(
        regime="RISK_ON", composite_score=2,
        vix_vote=1, futures_vote=0, fii_vote=1, usdinr_vote=0,
        reasons=["VIX low", "FII positive"],
    )
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.snapshot_and_classify", return_value=(snap, rr)
    ):
        ok, regime = _step_macro(conn, date(2026, 5, 15), warnings)
    assert ok is True
    assert regime == "RISK_ON"
    row = conn.execute(
        "SELECT vix, regime FROM macro_snapshot WHERE date = ?",
        ("2026-05-15",),
    ).fetchone()
    assert row is not None
    assert row["vix"] == 18.0
    assert row["regime"] == "RISK_ON"
    assert warnings == []


def test_step_macro_degrades_gracefully_on_fetch_error(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.snapshot_and_classify",
        side_effect=RuntimeError("yfinance down"),
    ):
        ok, regime = _step_macro(conn, date(2026, 5, 15), warnings)
    assert ok is False
    assert regime == "NEUTRAL"
    assert any("macro" in w.lower() for w in warnings)
    # Nothing persisted
    assert conn.execute(
        "SELECT COUNT(*) FROM macro_snapshot"
    ).fetchone()[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v -k step_macro`
Expected: FAIL — current stub returns `(False, "NEUTRAL")` always; first test expects True/RISK_ON.

- [ ] **Step 3: Implement `_step_macro`**

In `src/trading/jobs/pre_open.py`, add the import:

```python
from trading.data.macro import snapshot_and_classify
from trading.store.macro_store import upsert_macro_snapshot
```

Replace the `_step_macro` stub:

```python
def _step_macro(
    conn: sqlite3.Connection, as_of: date, warnings: list[str]
) -> tuple[bool, Regime]:
    """Pull macro inputs, classify regime, upsert snapshot. Degrade on error."""
    try:
        snap, rr = snapshot_and_classify(as_of)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"macro snapshot failed: {e!s}")
        return False, "NEUTRAL"
    upsert_macro_snapshot(conn, snap)
    return True, rr.regime
```

- [ ] **Step 4: Run tests**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: 3 passing (skeleton + 2 step_macro).

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): wire _step_macro (13.1.b)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Wire `_step_news`

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs_pre_open.py`:

```python
from datetime import UTC, datetime as _dt

from trading.data.news import RawHeadline
from trading.features.sentiment import ScoreResult
from trading.jobs.pre_open import _step_news


def _raw_headline(symbol: str = "RVNL") -> RawHeadline:
    return RawHeadline(
        ts=_dt(2026, 5, 14, 10, 0, tzinfo=UTC),
        source="moneycontrol",
        headline=f"{symbol} test headline",
        url=f"https://example.com/{symbol.lower()}",
    )


def test_step_news_inserts_headlines_and_aggregates(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.fetch_all_news",
        return_value=[_raw_headline("RVNL")],
    ), patch(
        "trading.jobs.pre_open.score_news_items",
        side_effect=lambda items: [
            __import__("trading.data.news", fromlist=["NewsItem"]).NewsItem(
                ts=i.ts.isoformat(),
                symbol="RVNL",
                source=i.source,
                headline=i.headline,
                url=i.url,
                sentiment=0.5,
                category="results",
                is_critical=False,
            )
            for i in items
        ],
    ):
        inserted, rollups = _step_news(
            conn, date(2026, 5, 15), warnings
        )
    assert inserted == 1
    assert rollups == 1  # RVNL is in DEFAULT_ALIASES
    assert warnings == []


def test_step_news_degrades_gracefully_on_fetch_error(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.fetch_all_news",
        side_effect=RuntimeError("RSS down"),
    ):
        inserted, rollups = _step_news(
            conn, date(2026, 5, 15), warnings
        )
    assert inserted == 0
    assert rollups == 0
    assert any("news" in w.lower() for w in warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v -k step_news`
Expected: FAIL — stub returns `(0, 0)`; first test expects `(1, 1)`.

- [ ] **Step 3: Implement `_step_news`**

In `src/trading/jobs/pre_open.py`, add imports:

```python
from trading.data.news import DEFAULT_ALIASES, fetch_all_news
from trading.features.sentiment import aggregate_daily, score_news_items
from trading.store.news_store import insert_news_items
```

Replace the `_step_news` stub:

```python
def _step_news(
    conn: sqlite3.Connection, as_of: date, warnings: list[str]
) -> tuple[int, int]:
    """Fetch RSS + NSE events, score with FinBERT, insert + aggregate.

    Returns (news_inserted, sentiment_rollups). Degrades gracefully:
    a top-level failure (e.g. all RSS sources down) returns (0, 0)
    with a warning. Per-source failures are isolated by Phase 8 already.
    """
    try:
        items = fetch_all_news()
        scored = score_news_items(items)
    except Exception as e:  # pragma: no cover — defensive
        warnings.append(f"news fetch/score failed: {e!s}")
        return 0, 0

    inserted = insert_news_items(conn, scored)
    watched = sorted(DEFAULT_ALIASES.keys())
    rollups = aggregate_daily(conn, watched, as_of)
    return inserted, len(rollups)
```

- [ ] **Step 4: Run tests**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: 5 passing.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): wire _step_news (13.1.c)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Wire `_step_scan`

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_jobs_pre_open.py`:

```python
from trading.jobs.pre_open import _step_scan
from trading.strategy.rules import Candidate, RuleResult


def _candidate(symbol: str, n_passed: int) -> Candidate:
    rules = tuple(
        RuleResult(name=f"r{i}", passed=(i < n_passed), reason="")
        for i in range(10)
    )
    return Candidate(
        symbol=symbol, scan_date=date(2026, 5, 15),
        close=100.0, rsi_14=40.0, sma_20=100.0, sma_50=100.0,
        sma_200=100.0, atr_14=2.0, rules=rules,
    )


def test_step_scan_delegates_to_strategy(paths) -> None:
    warnings: list[str] = []
    fake = [_candidate("RVNL", 9), _candidate("NTPC", 7)]
    with patch("trading.jobs.pre_open.scan", return_value=fake):
        out = _step_scan(paths, date(2026, 5, 15), warnings)
    assert out == fake
    assert warnings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v -k step_scan`
Expected: FAIL — stub returns `[]`.

- [ ] **Step 3: Implement `_step_scan`**

In `src/trading/jobs/pre_open.py`, add import:

```python
from trading.strategy.rules import ScanContext, scan
```

Replace the `_step_scan` stub:

```python
def _step_scan(
    paths: Paths, as_of: date, warnings: list[str]
) -> list[Candidate]:
    """Run Layer A scanner over the parquet universe."""
    ctx = ScanContext(scan_date=as_of)
    return scan(paths, as_of, ctx=ctx)
```

- [ ] **Step 4: Run tests**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: 6 passing.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): wire _step_scan (13.1.d)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Wire `_step_portfolio` (graceful Kite degradation)

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs_pre_open.py`:

```python
from trading.config import Settings
from trading.data.kite import Holding, KiteAuthError
from trading.jobs.pre_open import _step_portfolio


def _settings(token: str | None = None) -> Settings:
    return Settings(
        anthropic_api_key=None, kite_api_key="k",
        kite_api_secret="s", kite_access_token=token,
        log_level="INFO", news_user_agent="test",
    )


def test_step_portfolio_returns_empty_when_skip_kite(paths) -> None:
    warnings: list[str] = []
    out = _step_portfolio(paths, _settings(token="x"), warnings, skip_kite=True)
    assert out == []
    assert any("kite" in w.lower() for w in warnings)


def test_step_portfolio_returns_empty_when_no_token(paths) -> None:
    warnings: list[str] = []
    out = _step_portfolio(paths, _settings(token=None), warnings, skip_kite=False)
    assert out == []
    assert any("kite token" in w.lower() for w in warnings)


def test_step_portfolio_degrades_on_kite_auth_error(paths) -> None:
    warnings: list[str] = []
    with patch(
        "trading.jobs.pre_open.make_client", side_effect=KiteAuthError("expired")
    ):
        out = _step_portfolio(paths, _settings(token="x"), warnings, skip_kite=False)
    assert out == []
    assert any("kite auth" in w.lower() for w in warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v -k step_portfolio`
Expected: FAIL — stub returns `[]` with no warning emitted.

- [ ] **Step 3: Implement `_step_portfolio`**

In `src/trading/jobs/pre_open.py`, add imports:

```python
from trading.data.kite import KiteAuthError, get_holdings, make_client
from trading.portfolio.health import (
    FundamentalsSnapshot,
    HoldingContext,
    SentimentSnapshot,
    score_holding,
    technicals_from_history,
)
from trading.store.ohlcv import read_ohlcv
```

Replace the `_step_portfolio` stub:

```python
def _step_portfolio(
    paths: Paths,
    settings: Settings,
    warnings: list[str],
    *,
    skip_kite: bool,
) -> list[HealthScore]:
    """Pull live Kite holdings, score each. Empty if Kite token absent."""
    if skip_kite:
        warnings.append("skip_kite=True — portfolio health skipped")
        return []
    if not settings.kite_access_token or not settings.kite_api_key:
        warnings.append("kite token absent — portfolio health skipped")
        return []
    try:
        client = make_client(
            settings.kite_api_key, access_token=settings.kite_access_token
        )
        holdings = get_holdings(client)
    except KiteAuthError as e:
        warnings.append(f"kite auth failed: {e!s} — portfolio health skipped")
        return []

    results: list[HealthScore] = []
    for h in holdings:
        try:
            history = read_ohlcv(h.tradingsymbol, paths)
        except FileNotFoundError:
            warnings.append(f"no parquet for holding {h.tradingsymbol} — skipped")
            continue
        # add_indicators isn't strictly needed — technicals_from_history
        # works directly on raw OHLCV (computes 200-DMA / RSI internally).
        ctx = HoldingContext(
            symbol=h.tradingsymbol,
            technicals=technicals_from_history(history),
            fundamentals=FundamentalsSnapshot(),
            sentiment=SentimentSnapshot(),
        )
        results.append(score_holding(ctx))
    return results
```

- [ ] **Step 4: Run tests**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: 9 passing.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): wire _step_portfolio with graceful Kite degradation (13.1.e)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Wire `_step_auto_open` with idempotency guard (13.3)

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs_pre_open.py`:

```python
from trading.jobs.pre_open import _already_opened_today, _step_auto_open


def test_step_auto_open_creates_signal_and_paper_trade(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    cand = _candidate("RVNL", 10)  # all-pass
    opened = _step_auto_open(
        conn, date(2026, 5, 15), [cand], "NEUTRAL",
        capital=100_000.0, risk_pct=0.02, warnings=warnings,
    )
    assert opened == 1
    sig_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    pt_count = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE ts_exit IS NULL"
    ).fetchone()[0]
    assert sig_count == 1
    assert pt_count == 1


def test_step_auto_open_idempotent_on_rerun(
    conn: sqlite3.Connection,
) -> None:
    warnings: list[str] = []
    cand = _candidate("RVNL", 10)
    _step_auto_open(
        conn, date(2026, 5, 15), [cand], "NEUTRAL",
        capital=100_000.0, risk_pct=0.02, warnings=warnings,
    )
    # Re-run: no new paper-trade
    opened2 = _step_auto_open(
        conn, date(2026, 5, 15), [cand], "NEUTRAL",
        capital=100_000.0, risk_pct=0.02, warnings=warnings,
    )
    assert opened2 == 0
    pt_count = conn.execute(
        "SELECT COUNT(*) FROM paper_trades"
    ).fetchone()[0]
    assert pt_count == 1


def test_already_opened_today_detects_open_trade(
    conn: sqlite3.Connection,
) -> None:
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'LONG', ?, ?, ?, 25)",
        ("2026-05-15T08:30:00", "RVNL", 100.0, 95.0, 120.0),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty) "
        "VALUES (?, ?, ?, ?)",
        (sig_id, "2026-05-15T08:30:00", 100.0, 10),
    )
    conn.commit()
    assert _already_opened_today(conn, "RVNL", date(2026, 5, 15)) is True
    assert _already_opened_today(conn, "NTPC", date(2026, 5, 15)) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v -k auto_open`
Expected: FAIL — `_step_auto_open` returns 0 always; `_already_opened_today` doesn't exist.

- [ ] **Step 3: Implement `_step_auto_open` and `_already_opened_today`**

In `src/trading/jobs/pre_open.py`, add imports:

```python
import json

from trading.paper.ledger import log_signal_and_open_trade
from trading.store.repo import Signal
from trading.strategy.sizing import SizingInput, position_size
```

Replace the `_step_auto_open` stub and add the helper:

```python
def _step_auto_open(
    conn: sqlite3.Connection,
    as_of: date,
    passing: list[Candidate],
    regime: Regime,
    capital: float,
    risk_pct: float,
    warnings: list[str],
) -> int:
    """For each all-pass candidate: size + open paper-trade.

    Entry price = `cand.close` (D-1's close — the most recent bar in the
    parquet at pre_open time, per spec §4.4 'limit order at close').
    Skips if (a) idempotency guard finds an open trade for symbol+date,
    or (b) sizing returns qty=0 (caps bound to zero).
    """
    opened = 0
    for cand in passing:
        if _already_opened_today(conn, cand.symbol, as_of):
            continue
        stop_price = cand.close - 1.5 * cand.atr_14
        target_price = cand.close * 1.20
        if cand.close <= stop_price:
            warnings.append(
                f"{cand.symbol}: ATR={cand.atr_14:.2f} ≥ close — skip"
            )
            continue
        sizing = position_size(SizingInput(
            capital=capital, risk_pct=risk_pct,
            entry=cand.close, stop=stop_price, regime=regime,
        ))
        if sizing.qty == 0:
            warnings.append(
                f"{cand.symbol}: sizing bound to zero ({', '.join(sizing.reasons)})"
            )
            continue
        signal = Signal(
            id=None,
            ts=f"{as_of.isoformat()}T08:30:00",
            symbol=cand.symbol,
            side="LONG",
            entry=cand.close,
            stop=stop_price,
            target=target_price,
            horizon_days=25,
            rules_passed_json=json.dumps(
                [r.name for r in cand.rules if r.passed]
            ),
            created_by="pre_open",
        )
        log_signal_and_open_trade(
            conn, signal=signal,
            entry_ts=signal.ts, entry_price=cand.close, qty=sizing.qty,
            atr_at_entry=cand.atr_14, predicted_return_pct=20.0,
        )
        opened += 1
    return opened


def _already_opened_today(
    conn: sqlite3.Connection, symbol: str, as_of: date
) -> bool:
    """True if `symbol` has an OPEN paper-trade entered on `as_of`."""
    row = conn.execute(
        "SELECT 1 FROM paper_trades pt "
        "JOIN signals s ON s.id = pt.signal_id "
        "WHERE s.symbol = ? AND substr(pt.ts_entry, 1, 10) = ? "
        "  AND pt.ts_exit IS NULL "
        "LIMIT 1",
        (symbol, as_of.isoformat()),
    ).fetchone()
    return row is not None
```

- [ ] **Step 4: Run tests**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py -v`
Expected: 12 passing.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/pre_open.py tests/test_jobs_pre_open.py
git commit -m "feat(jobs): wire _step_auto_open + idempotency guard (13.1.f, 13.3)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Add CLI command `trading pre-open`

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

`_step_assemble` is already wired in Task 1 (it's just the `assemble_context` call) and the orchestrator already calls every step in order. So Task 7 layers the CLI on top — no changes to `pre_open.py`'s body needed.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_pre_open_cli_writes_bundle_and_prints_next_step(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    # Stub every upstream call so the CLI runs offline + fast.
    from trading.jobs import pre_open as po
    monkeypatch.setattr(po, "_step_macro",
                        lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr(po, "_step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr(po, "_step_scan", lambda p, d, w: [])
    monkeypatch.setattr(po, "_step_portfolio",
                        lambda p, s, w, *, skip_kite: [])
    monkeypatch.setattr(po, "_step_auto_open",
                        lambda *a, **kw: 0)
    result = runner.invoke(
        app,
        ["pre-open", "--date", "2026-05-15", "--skip-news", "--skip-kite"],
    )
    assert result.exit_code == 0, result.stdout
    out_path = tmp_path / "data" / "research" / "2026-05-15" / "_context.md"
    assert out_path.is_file()
    assert "/analyst" in result.stdout
    assert "trading brief compile" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_cli.py -v -k pre_open_cli`
Expected: FAIL — no such subcommand.

- [ ] **Step 3: Add the CLI command**

In `src/trading/cli.py`, add the import near the existing `from trading.llm.briefing` line:

```python
from trading.jobs.pre_open import run_pre_open
```

After the `brief_app` block, before `if __name__ == "__main__":`, add:

```python
@app.command("pre-open")
def pre_open_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    skip_news: Annotated[bool, typer.Option("--skip-news")] = False,
    skip_kite: Annotated[bool, typer.Option("--skip-kite")] = False,
    capital: Annotated[float, typer.Option(help="Capital per trade.")] = 100_000.0,
    risk_pct: Annotated[float, typer.Option(help="Risk per trade.")] = 0.02,
) -> None:
    """Phase 13 MVP — orchestrate Phases 1-12 and write the analyst bundle."""
    as_of = date.fromisoformat(date_str)
    result = run_pre_open(
        as_of, skip_news=skip_news, skip_kite=skip_kite,
        capital_per_trade=capital, risk_pct=risk_pct,
    )
    table = Table(title=f"pre_open {as_of.isoformat()}", show_header=True)
    table.add_column("step")
    table.add_column("count", justify="right")
    table.add_row("macro_written", "yes" if result.macro_written else "no")
    table.add_row("news_inserted", str(result.news_inserted))
    table.add_row("sentiment_rows", str(result.sentiment_rows))
    table.add_row("candidates_total", str(result.candidates_total))
    table.add_row("candidates_passing", str(result.candidates_passing))
    table.add_row("paper_trades_opened", str(result.paper_trades_opened))
    table.add_row("holdings_scored", str(result.holdings_scored))
    console.print(table)
    if result.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")
    console.print(f"[green]wrote[/green] {result.bundle_path}")
    console.print(
        f"[bold]Now run /analyst skill, then "
        f"`trading brief compile --date {date_str}`[/bold]"
    )
```

- [ ] **Step 4: Run tests + smoke `--help`**

```bash
cd D:/Projects/Trading
uv run pytest tests/test_cli.py -v
uv run trading pre-open --help
```

Expected: tests pass; `pre-open` listed in `trading --help`.

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading pre-open command (13.1.g, 13.2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Add `__main__` entry + `scripts/pre_open.bat` (13.4)

**Files:**
- Modify: `src/trading/jobs/pre_open.py`
- Create: `scripts/pre_open.bat`

- [ ] **Step 1: Add `__main__` block to `pre_open.py`**

Append to `src/trading/jobs/pre_open.py`:

```python
def _main(  # pragma: no cover — manual entry
    date_str: str,
    skip_news: bool = False,
    skip_kite: bool = False,
) -> None:
    """`python -m trading.jobs.pre_open --date YYYY-MM-DD` entry."""
    result = run_pre_open(
        date.fromisoformat(date_str),
        skip_news=skip_news, skip_kite=skip_kite,
    )
    print(f"wrote {result.bundle_path}")
    if result.warnings:
        print(f"warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  - {w}")


if __name__ == "__main__":  # pragma: no cover
    import typer
    typer.run(_main)
```

- [ ] **Step 2: Create the .bat launcher**

```bash
cd D:/Projects/Trading && mkdir -p scripts
```

Create `scripts/pre_open.bat`:

```bat
@echo off
REM Phase 13 MVP launcher. Phase 17 will wire this into Windows Task Scheduler.
REM Usage: pre_open.bat YYYY-MM-DD
cd /d "%~dp0\.."
if "%~1"=="" (
  echo Usage: pre_open.bat YYYY-MM-DD
  exit /b 2
)
uv run python -m trading.jobs.pre_open --date-str %1
```

- [ ] **Step 3: Smoke-test the `__main__` invocation**

```bash
cd D:/Projects/Trading
uv run python -m trading.jobs.pre_open --date-str 2026-05-15 --skip-news --skip-kite
```

Expected: writes `data/research/2026-05-15/_context.md`, prints "wrote …".

- [ ] **Step 4: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/jobs/pre_open.py scripts/pre_open.bat
git commit -m "feat(jobs): __main__ entry + pre_open.bat launcher (13.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Integration test — full happy path (13.5)

**Files:**
- Modify: `tests/test_jobs_pre_open.py`

- [ ] **Step 1: Write the integration test**

Append to `tests/test_jobs_pre_open.py`:

```python
import pandas as pd

from trading.features.technicals import add_indicators
from trading.store.ohlcv import write_ohlcv


def _all_pass_frame() -> pd.DataFrame:
    """Construct an enriched OHLCV frame engineered to pass every rule.

    Trick: use a long flat history at price 100, then a recent rally to
    115, settling back to 110 at the last bar. SMA-200 ≈ 100, SMA-50
    rises above 200, RSI ~40 (pullback band), close above SMA-200,
    ATR small, volume normal. The exact numbers are tuned; verify by
    re-running and adjusting if scanner output changes.
    """
    n = 260
    idx = pd.date_range("2025-09-01", periods=n, freq="B")
    idx.name = "date"
    closes = [100.0] * (n - 30) + list(range(101, 116)) + [110.0] * 15
    df = pd.DataFrame(
        {
            "open":  [c - 0.5 for c in closes],
            "high":  [c + 1.0 for c in closes],
            "low":   [c - 1.0 for c in closes],
            "close": closes,
            "volume": [2_000_000] * n,
        },
        index=idx,
    )
    return df


def test_run_pre_open_full_happy_path_integration(
    paths, monkeypatch
) -> None:
    """End-to-end with synthetic parquet + stubbed network calls.

    Intent: prove the pipeline wires together. We don't assert that any
    specific symbol passes all 10 rules — that's brittle to threshold
    tweaks. We assert structural invariants: bundle exists, counts make
    sense, idempotency holds.
    """
    write_ohlcv(_all_pass_frame(), "TESTSYM", paths)

    # Stub network-bound steps for offline run
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_macro",
        lambda c, d, w: (True, "RISK_ON"),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_news",
        lambda c, d, w: (0, 0),
    )
    monkeypatch.setattr(
        "trading.jobs.pre_open._step_portfolio",
        lambda p, s, w, *, skip_kite: [],
    )

    result = run_pre_open(
        date(2026, 5, 15), paths=paths,
        skip_news=False,  # _step_news is stubbed already
        skip_kite=True,
    )
    # Bundle written
    assert result.bundle_path.is_file()
    body = result.bundle_path.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "## Today's candidates" in body
    # Scanner ran on our 1 symbol
    assert result.candidates_total == 1
    # No regression on counts (passing may be 0 or 1 depending on rule
    # interplay; either is fine for this structural test)
    assert result.paper_trades_opened == result.candidates_passing

    # Idempotency: re-run produces 0 NEW paper-trades regardless of
    # whether the first run opened any
    result2 = run_pre_open(
        date(2026, 5, 15), paths=paths,
        skip_news=False, skip_kite=True,
    )
    assert result2.paper_trades_opened == 0
```

- [ ] **Step 2: Run test**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_jobs_pre_open.py::test_run_pre_open_full_happy_path_integration -v`
Expected: PASS.

- [ ] **Step 3: Run full suite + lint + types**

```bash
cd D:/Projects/Trading
uv run ruff check . && uv run mypy src/ && uv run pytest -q
```

Expected: all clean. Test count rises by ~13 (12 pre_open + 1 CLI = 13 new).

- [ ] **Step 4: Commit**

```bash
cd D:/Projects/Trading
git add tests/test_jobs_pre_open.py
git commit -m "test(jobs): full pre_open happy-path integration (13.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Real-data smoke (13.6)

**Files:**
- Mutates: `data/app.db` (gitignored), `data/research/YYYY-MM-DD/` (gitignored)

- [ ] **Step 1: Capture before-state**

```bash
cd D:/Projects/Trading
uv run python -c "
import sqlite3
c = sqlite3.connect('data/app.db'); c.row_factory = sqlite3.Row
print('signals:', c.execute('select count(*) from signals').fetchone()[0])
print('open paper-trades:', c.execute('select count(*) from paper_trades where ts_exit is null').fetchone()[0])
"
```

Record the counts.

- [ ] **Step 2: Run the real smoke**

```bash
cd D:/Projects/Trading
uv run trading pre-open --date 2026-05-15 --skip-kite
```

Expected: a Rich table with non-zero counts (macro_written: yes, news_inserted: 500+, candidates_total: 12 or so, candidates_passing: 0+); bundle path printed; `/analyst` instruction printed.

If `paper_trades_opened > 0`, inspect the new rows:

```bash
cd D:/Projects/Trading && uv run python -c "
import sqlite3
c = sqlite3.connect('data/app.db'); c.row_factory = sqlite3.Row
for r in c.execute(\"select s.symbol, s.entry, s.stop, s.target, pt.qty from paper_trades pt join signals s on s.id = pt.signal_id where pt.ts_exit is null order by pt.id desc limit 5\"):
    print(' ', dict(r))
"
```

- [ ] **Step 3: Inspect the bundle**

```bash
cd D:/Projects/Trading && head -80 data/research/2026-05-15/_context.md
```

Expected: macro section populated with real values, candidates section with real symbols+RSI+ATR, holdings section `_(no data)_` (Kite skipped), open paper-trades section reflects any auto-opened trades from Step 2.

- [ ] **Step 4: Re-run for idempotency**

```bash
cd D:/Projects/Trading && uv run trading pre-open --date 2026-05-15 --skip-kite
```

Expected: same Rich table but `paper_trades_opened: 0` (the guard kicks in).

- [ ] **Step 5: Record findings (no commit yet)**

Note any observations: how many candidates passed, did the bundle look reasonable, any new warnings. These get included in Task 11's PROGRESS.md entry.

---

## Task 11: PROGRESS.md + final commit + push (13.7)

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Mark Phase 13 sub-tasks complete**

Edit the status snapshot table in `PROGRESS.md`:

```
| 13 | pre_open job (MVP ⭐) | `[x]` |
```

Update pointers:

```
**Currently working on:** _Phase 14 — mid_day + post_close jobs_
**Next up:** _Phase 12.6 — Sector data (deferred)_
```

- [ ] **Step 2: Replace Phase 13 body block**

Replace the existing `## Phase 13 — pre_open job (E2E) ⭐ MVP milestone` block with:

```markdown
## Phase 13 — pre_open job (E2E) ⭐ MVP milestone

> Spec at [`docs/superpowers/specs/2026-05-15-phase-13-pre-open-design.md`](docs/superpowers/specs/2026-05-15-phase-13-pre-open-design.md).
> Plan at [`docs/superpowers/plans/2026-05-15-phase-13-pre-open.md`](docs/superpowers/plans/2026-05-15-phase-13-pre-open.md).

- [x] 13.1 `src/trading/jobs/pre_open.py`: `run_pre_open(as_of, ...)` orchestrator
       + `PreOpenResult` dataclass + 6 private `_step_*` helpers
       (`_step_macro` / `_step_news` / `_step_scan` / `_step_portfolio` /
       `_step_auto_open` / `_step_assemble`). In-process invocation of every
       upstream phase (1-12), each step degrades gracefully when its data
       source is unavailable.
- [x] 13.2 Bundle written to `data/research/YYYY-MM-DD/_context.md` via
       Phase 12's `assemble_context`; CLI prints next-step instruction
       (run `/analyst`, then `trading brief compile`).
- [x] 13.3 Auto-log: `_step_auto_open` opens one paper-trade per all-pass
       candidate at D-1's close (per spec §4.4 'limit order at close').
       Position sizing per Phase 6 with regime multiplier from `_step_macro`.
       Idempotency guard (`_already_opened_today`) prevents duplicate
       paper-trades on re-run.
- [x] 13.4 `scripts/pre_open.bat` Windows launcher invoking
       `uv run python -m trading.jobs.pre_open --date-str <iso>`. Phase 17
       will wire this into Task Scheduler.
- [x] 13.5 12 unit tests in `test_jobs_pre_open.py` (per-step + idempotency)
       + 1 end-to-end integration test on synthetic parquet + 1 CLI happy-path
       test. All offline / deterministic.
- [x] 13.6 Manual smoke run on real data — see commit body for findings.
- [x] 13.7 PROGRESS.md updated → commit `feat(jobs): pre_open end-to-end (MVP)`
       and pushed to origin/main.
```

- [ ] **Step 3: Run full verification one more time**

```bash
cd D:/Projects/Trading
uv run ruff check . && uv run mypy src/ && uv run pytest -q
```

Expected: clean. Test count: ~462 passed, 1 skipped (live).

- [ ] **Step 4: Commit + push**

```bash
cd D:/Projects/Trading
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(jobs): pre_open end-to-end (MVP) (Phase 13)

The first end-to-end "it works" moment. One command runs every
upstream phase in dependency order, auto-opens paper-trades for
all-pass signals at D-1 close, writes the analyst bundle, halts
for the user to invoke /analyst.

Wires in-process: macro snapshot → news ingest + FinBERT scoring →
scanner → portfolio health (Kite-conditional) → auto-open paper-
trades (with idempotency guard) → context bundle.

CLI: trading pre-open --date YYYY-MM-DD [--skip-news] [--skip-kite]
Module entry: python -m trading.jobs.pre_open --date-str YYYY-MM-DD
Launcher: scripts/pre_open.bat (Phase 17 Task Scheduler hook)

Spec: docs/superpowers/specs/2026-05-15-phase-13-pre-open-design.md
Plan: docs/superpowers/plans/2026-05-15-phase-13-pre-open.md

Tests: <count> passed, 1 skipped (live).

Smoke findings (record from Task 10): <fill in>.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push origin main
```

Expected: push succeeds; the MVP is shipped.

---

## Self-review notes

- **Spec coverage:** spec §3 components → Tasks 1-7 (orchestrator, dataclass, CLI). spec §4 `_step_*` functions → Tasks 2-6. spec §5 error handling → graceful-degradation tests in 2, 3, 5; idempotency in 6. spec §6 testing → 12 unit tests + 1 integration + 1 CLI. spec §7 sub-task breakdown → mapped 1:1 to Tasks 2-7+9-11. ✓
- **Type consistency:** `PreOpenResult` field names used identically across Tasks 1, 7, 10, 11. `_step_*` signatures match between stubs (Task 1) and real implementations (Tasks 2-6). `Regime` literal threaded macro→auto_open. ✓
- **Placeholder scan:** every code block is concrete; commit messages written out (one TBD: smoke findings in Task 11 commit body, which is a deliberate runtime placeholder filled in only after Task 10 runs). ✓
- **Idempotency arithmetic:** `_already_opened_today` keys on `(symbol, date(ts_entry))` so a re-run of the same date produces zero new opens. Verified with explicit test.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-15-phase-13-pre-open.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

**Which approach?**
