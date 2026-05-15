# Phase 12 — LLM Analyst Skill: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the planned Anthropic-SDK analyst layer with a project-level Claude Code skill (`/analyst`) plus deterministic file-IO helpers, so the user-in-the-loop produces narrative outputs without API spend.

**Architecture:** Two-stage handshake via files in `data/research/YYYY-MM-DD/`. Phase 13's `pre_open` job will (1) call `assemble_context` to write `_context.md`, then halt; (2) the user invokes `/analyst` which reads the bundle and writes narrative parts; (3) `pre_open` is re-run with `--compile` to merge parts into final `brief.md`. `context.py` is a pure renderer over a typed `ContextInputs` dataclass; DB-resident state is pulled directly via `conn`.

**Tech Stack:** Python 3.11 · sqlite3 · `dataclasses` · `typer` (CLI) · `syrupy` (snapshot tests) · `pytest`. Existing project conventions: frozen dataclasses, Ruff/mypy strict, in-memory SQLite for tests.

**Spec:** [`docs/superpowers/specs/2026-05-15-phase-12-llm-skill-design.md`](../specs/2026-05-15-phase-12-llm-skill-design.md)

---

## File structure

| Path | Created/Modified | Responsibility |
|------|------------------|----------------|
| `src/trading/llm/__init__.py` | Modify | Re-export `ContextInputs`, `assemble_context`, `compile_brief`, `MissingNarrativeError`, `expected_parts` |
| `src/trading/llm/context.py` | Create | `ContextInputs` dataclass; `assemble_context(conn, paths, as_of, mode, inputs) -> Path`; private `_render_*` section helpers |
| `src/trading/llm/briefing.py` | Create | `compile_brief(date_dir, mode) -> Path`; `expected_parts(mode, candidate_symbols) -> list[str]`; `MissingNarrativeError`; orphan-warning logic |
| `src/trading/cli.py` | Modify | Add `brief assemble-context` and `brief compile` subcommands under a `brief` sub-app |
| `.claude/skills/analyst/SKILL.md` | Create | Skill frontmatter + step-by-step instructions for me-as-analyst |
| `.claude/skills/analyst/references/output-templates.md` | Create | Exact markdown skeletons for the four narrative output files |
| `tests/test_llm_context.py` | Create | Unit + snapshot tests for `assemble_context` |
| `tests/test_llm_briefing.py` | Create | Unit + snapshot tests for `compile_brief` |
| `tests/test_cli.py` | Modify | Add happy-path tests for `trading brief …` |
| `tests/__snapshots__/` | Create (auto by syrupy) | Frozen syrupy snapshots for both modules |
| `PROGRESS.md` | Modify | Mark Phase 11 done in status snapshot + sub-tasks; rewrite Phase 12 sub-tasks per spec §7 |

---

## Task 1: Stub `llm/context.py` with `ContextInputs` + skeleton `assemble_context`

**Files:**
- Create: `src/trading/llm/context.py`
- Modify: `src/trading/llm/__init__.py`
- Test: `tests/test_llm_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_context.py
"""Tests for trading.llm.context — input bundle assembly."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.llm.context import ContextInputs, assemble_context
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


def test_assemble_context_writes_file_with_header(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn,
        paths=paths,
        as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    assert "# Trading context bundle — 2026-05-15" in body
    assert "(mode: pre_open)" in body
    assert out == paths.research_dir / "2026-05-15" / "_context.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_context.py::test_assemble_context_writes_file_with_header -v`
Expected: FAIL — `ImportError: cannot import name 'ContextInputs' from 'trading.llm.context'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/llm/context.py
"""Phase 12 input-bundle renderer.

Reads DB-resident state (macro_snapshot, sentiment_daily, news_items,
paper_trades, predictions) directly via `conn`. Ephemeral upstream outputs
(scanner candidates, portfolio health) arrive via the typed `ContextInputs`
dataclass — Phase 13's pre_open job is responsible for computing them once
and passing them in. Pure renderer: no scanner / portfolio invocation here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from trading.config import Paths
from trading.portfolio.health import HealthScore
from trading.strategy.rules import Candidate

Mode = Literal["pre_open", "post_close"]


@dataclass(frozen=True)
class ContextInputs:
    """Caller-supplied ephemeral inputs that don't live in any DB table."""

    candidates: list[Candidate] = field(default_factory=list)
    holdings_health: list[HealthScore] = field(default_factory=list)


def assemble_context(
    *,
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    mode: Mode,
    inputs: ContextInputs,
) -> Path:
    """Render `_context.md` for `as_of` and return the written path.

    Sections rendered (in order): header, macro, candidates, holdings health,
    open paper-trades, (post_close only) matured predictions. Empty sections
    render as `_(no data)_` so the skill can flag the gap explicitly.
    """
    date_dir = paths.research_dir / as_of.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    out_path = date_dir / "_context.md"

    parts: list[str] = []
    parts.append(_render_header(as_of, mode))
    # Section renderers will be wired in subsequent tasks.

    out_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return out_path


def _render_header(as_of: date, mode: Mode) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    return (
        f"# Trading context bundle — {as_of.isoformat()}  (mode: {mode})\n"
        f"\n"
        f"_Assembled at {ts}._"
    )
```

```python
# src/trading/llm/__init__.py
"""Public surface for the LLM analyst pipeline (spec §4.3)."""

from trading.llm.context import ContextInputs, Mode, assemble_context

__all__ = ["ContextInputs", "Mode", "assemble_context"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_llm_context.py::test_assemble_context_writes_file_with_header -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/context.py src/trading/llm/__init__.py tests/test_llm_context.py
git commit -m "feat(llm): ContextInputs dataclass + assemble_context skeleton (12.1)"
```

---

## Task 2: Render macro snapshot section (DB read)

**Files:**
- Modify: `src/trading/llm/context.py`
- Modify: `tests/test_llm_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_context.py — append
def _seed_macro(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO macro_snapshot
          (date, sgx_nifty, dow_fut, nasdaq_fut, sp500, usdinr, crude,
           vix, us_10y, fii_flow_cr, dii_flow_cr, regime)
        VALUES (?, NULL, NULL, NULL, NULL, ?, NULL, ?, NULL, ?, NULL, ?)
        """,
        ("2026-05-15", 95.76, 19.4, 187.0, "NEUTRAL"),
    )
    conn.commit()


def test_assemble_context_includes_macro_section(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_macro(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "VIX" in body and "19.4" in body
    assert "USDINR" in body and "95.76" in body
    assert "FII flow" in body and "187" in body
    assert "NEUTRAL" in body


def test_assemble_context_macro_no_data_when_missing(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "_(no data)_" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_context.py::test_assemble_context_includes_macro_section tests/test_llm_context.py::test_assemble_context_macro_no_data_when_missing -v`
Expected: FAIL — both tests, "## Macro snapshot" not found.

- [ ] **Step 3: Write minimal implementation**

In `src/trading/llm/context.py`, add the macro renderer and wire it into `assemble_context`:

```python
def _render_macro(conn: sqlite3.Connection, as_of: date) -> str:
    row = conn.execute(
        "SELECT vix, usdinr, fii_flow_cr, dii_flow_cr, regime "
        "FROM macro_snapshot WHERE date = ?",
        (as_of.isoformat(),),
    ).fetchone()
    if row is None:
        return "## Macro snapshot\n\n_(no data)_"
    lines = ["## Macro snapshot", "", "| field | value |", "|---|---|"]
    if row["vix"] is not None:
        lines.append(f"| VIX | {row['vix']:.2f} |")
    if row["usdinr"] is not None:
        lines.append(f"| USDINR | {row['usdinr']:.2f} |")
    if row["fii_flow_cr"] is not None:
        lines.append(f"| FII flow (₹ cr) | {row['fii_flow_cr']:+.0f} |")
    if row["dii_flow_cr"] is not None:
        lines.append(f"| DII flow (₹ cr) | {row['dii_flow_cr']:+.0f} |")
    if row["regime"] is not None:
        lines.append(f"| Regime | {row['regime']} |")
    return "\n".join(lines)
```

Wire it in:

```python
    parts.append(_render_header(as_of, mode))
    parts.append(_render_macro(conn, as_of))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm_context.py -v`
Expected: 3 passing.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/context.py tests/test_llm_context.py
git commit -m "feat(llm): render macro snapshot section in context bundle (12.1)"
```

---

## Task 3: Render candidates section with news join

**Files:**
- Modify: `src/trading/llm/context.py`
- Modify: `tests/test_llm_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_context.py — append
from trading.strategy.rules import Candidate, RuleResult


def _candidate(symbol: str = "RVNL", n_passed: int = 9) -> Candidate:
    rules = tuple(
        RuleResult(name=f"r{i}", passed=(i < n_passed), reason="")
        for i in range(10)
    )
    return Candidate(
        symbol=symbol,
        scan_date=date(2026, 5, 15),
        close=312.5,
        rsi_14=58.0,
        sma_20=305.0,
        sma_50=300.0,
        sma_200=275.0,
        atr_14=8.4,
        rules=rules,
    )


def _seed_news(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute(
        "INSERT INTO news_items (ts, symbol, source, headline, url, "
        "sentiment, category, is_critical) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-05-13T10:00:00",
            symbol,
            "moneycontrol",
            "RVNL bags ₹500cr order from Indian Railways",
            "https://example.com/rvnl",
            0.55,
            "results",
            0,
        ),
    )
    conn.execute(
        "INSERT INTO sentiment_daily (date, symbol, score_7d, score_30d, "
        "news_count, negative_news_count, has_critical) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-15", symbol, 0.32, 0.18, 4, 0, 0),
    )
    conn.commit()


def test_assemble_context_includes_candidates_section(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_news(conn, "RVNL")
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[_candidate("RVNL", n_passed=9)],
                             holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Today's candidates" in body
    assert "### RVNL" in body
    assert "9/10" in body
    assert "RSI" in body and "58" in body
    assert "ATR" in body and "8.4" in body
    assert "RVNL bags ₹500cr" in body
    assert "Critical news flag: NO" in body


def test_assemble_context_candidates_no_data_when_empty(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Today's candidates" in body
    assert "_(no data)_" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm_context.py::test_assemble_context_includes_candidates_section tests/test_llm_context.py::test_assemble_context_candidates_no_data_when_empty -v`
Expected: FAIL — "## Today's candidates" not found.

- [ ] **Step 3: Write minimal implementation**

In `src/trading/llm/context.py`:

```python
def _render_candidates(
    conn: sqlite3.Connection, as_of: date, candidates: list[Candidate]
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
        blocks.extend(_render_news_for_symbol(conn, c.symbol, as_of))
    return "\n".join(blocks)


def _render_news_for_symbol(
    conn: sqlite3.Connection, symbol: str, as_of: date
) -> list[str]:
    """Last 7 days of headlines + sentiment_daily summary + critical flag."""
    cutoff = (as_of - _timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT ts, headline, sentiment, category, is_critical "
        "FROM news_items WHERE symbol = ? AND ts >= ? "
        "ORDER BY ts DESC LIMIT 5",
        (symbol, cutoff),
    ).fetchall()
    sd = conn.execute(
        "SELECT score_7d, news_count, negative_news_count, has_critical "
        "FROM sentiment_daily WHERE date = ? AND symbol = ?",
        (as_of.isoformat(), symbol),
    ).fetchone()
    out: list[str] = []
    if sd is not None:
        out.append(
            f"- sentiment 7d {sd['score_7d']:+.2f} · "
            f"{sd['news_count']} headlines ({sd['negative_news_count']} negative)"
        )
        out.append(
            f"- Critical news flag: {'YES' if sd['has_critical'] else 'NO'}"
        )
    else:
        out.append("- sentiment: _(no daily aggregate)_")
        out.append("- Critical news flag: NO")
    if rows:
        out.append("- Recent headlines:")
        for r in rows:
            score = f"{r['sentiment']:+.2f}" if r["sentiment"] is not None else "—"
            cat = r["category"] or "—"
            out.append(f"  - {r['ts'][:10]} · [{cat}] {r['headline']} ({score})")
    return out
```

Add the import for `timedelta`:

```python
from datetime import date, datetime, timedelta as _timedelta
```

Wire it in:

```python
    parts.append(_render_macro(conn, as_of))
    parts.append(_render_candidates(conn, as_of, inputs.candidates))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm_context.py -v`
Expected: 5 passing.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/context.py tests/test_llm_context.py
git commit -m "feat(llm): render candidates section with news join (12.1)"
```

---

## Task 4: Render holdings_health section

**Files:**
- Modify: `src/trading/llm/context.py`
- Modify: `tests/test_llm_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_context.py — append
from trading.portfolio.health import HealthScore


def test_assemble_context_includes_holdings_health(
    conn: sqlite3.Connection, paths
) -> None:
    health = HealthScore(
        symbol="TATAPOWER",
        verdict="TRIM",
        score=22,
        net_votes=-2,
        votes_cast=8,
        reasons=["below 200-DMA", "RSI 38", "dist to 52w high 28%"],
        pnl_pct=-3.2,
    )
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[health]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Holdings health" in body
    assert "### TATAPOWER" in body
    assert "TRIM" in body
    assert "22/100" in body
    assert "below 200-DMA" in body


def test_assemble_context_holdings_health_no_data_when_empty(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Holdings health" in body
    assert "_(no data)_" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm_context.py::test_assemble_context_includes_holdings_health tests/test_llm_context.py::test_assemble_context_holdings_health_no_data_when_empty -v`
Expected: FAIL — "## Holdings health" not found.

- [ ] **Step 3: Write minimal implementation**

In `src/trading/llm/context.py`:

```python
def _render_holdings_health(rows: list[HealthScore]) -> str:
    if not rows:
        return "## Holdings health\n\n_(no data)_"
    sorted_rows = sorted(rows, key=lambda h: (h.score, h.symbol))
    blocks: list[str] = ["## Holdings health"]
    for h in sorted_rows[:10]:
        blocks.append("")
        blocks.append(
            f"### {h.symbol} — verdict {h.verdict} (score {h.score}/100, "
            f"net votes {h.net_votes:+d}/{h.votes_cast})"
        )
        if h.pnl_pct is not None:
            blocks.append(f"- unrealised P&L: {h.pnl_pct:+.2f}%")
        for reason in h.reasons:
            blocks.append(f"- {reason}")
    return "\n".join(blocks)
```

Wire it in:

```python
    parts.append(_render_candidates(conn, as_of, inputs.candidates))
    parts.append(_render_holdings_health(inputs.holdings_health))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm_context.py -v`
Expected: 7 passing.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/context.py tests/test_llm_context.py
git commit -m "feat(llm): render holdings health section in context bundle (12.1)"
```

---

## Task 5: Render open paper-trades + matured predictions (mode-conditional)

**Files:**
- Modify: `src/trading/llm/context.py`
- Modify: `tests/test_llm_context.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_context.py — append
def _seed_open_trade(conn: sqlite3.Connection) -> None:
    sig_id = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, "
        "horizon_days) VALUES (?, ?, 'BUY', ?, ?, ?, 25)",
        ("2026-05-11T15:30:00", "RVNL", 305.0, 290.0, 360.0),
    ).lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, "
        "current_stop, atr_at_entry) VALUES (?, ?, ?, ?, ?, ?)",
        (sig_id, "2026-05-12T09:15:00", 305.0, 32, 295.0, 8.4),
    )
    conn.commit()


def _seed_matured_prediction(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO predictions (ts, symbol, predicted_return_pct, "
        "predicted_horizon_days, actual_return_at_horizon, error_pct, "
        "evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-10T15:30:00", "RVNL", 5.0, 25, 6.2, 1.2, "2026-05-15T16:00:00"),
    )
    conn.commit()


def test_assemble_context_includes_open_trades(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_open_trade(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Open paper-trades" in body
    assert "RVNL" in body
    assert "305.0" in body or "305.00" in body


def test_assemble_context_pre_open_omits_matured_predictions(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_matured_prediction(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Matured predictions" not in body


def test_assemble_context_post_close_includes_matured_predictions(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_matured_prediction(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="post_close",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Matured predictions" in body
    assert "RVNL" in body
    assert "5.00" in body  # predicted
    assert "6.20" in body  # actual
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm_context.py -v -k "open_trades or matured"`
Expected: 3 failures — sections not present.

- [ ] **Step 3: Write minimal implementation**

In `src/trading/llm/context.py`:

```python
def _render_open_trades(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT pt.id, s.symbol, pt.entry_price, pt.qty, pt.ts_entry, "
        "pt.current_stop "
        "FROM paper_trades pt JOIN signals s ON s.id = pt.signal_id "
        "WHERE pt.ts_exit IS NULL ORDER BY pt.ts_entry"
    ).fetchall()
    if not rows:
        return "## Open paper-trades\n\n_(no data)_"
    out = [
        "## Open paper-trades",
        "",
        "| symbol | entry | qty | entered | trailing stop |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        stop = f"{r['current_stop']:.2f}" if r["current_stop"] is not None else "—"
        out.append(
            f"| {r['symbol']} | {r['entry_price']:.2f} | {r['qty']} | "
            f"{r['ts_entry'][:10]} | {stop} |"
        )
    return "\n".join(out)


def _render_matured_predictions(conn: sqlite3.Connection, as_of: date) -> str:
    rows = conn.execute(
        "SELECT symbol, predicted_return_pct, actual_return_at_horizon, "
        "error_pct, predicted_horizon_days "
        "FROM predictions "
        "WHERE evaluated_at IS NOT NULL AND substr(evaluated_at, 1, 10) = ? "
        "ORDER BY symbol",
        (as_of.isoformat(),),
    ).fetchall()
    if not rows:
        return "## Matured predictions\n\n_(no data)_"
    out = [
        "## Matured predictions",
        "",
        "| symbol | predicted % | actual % | error % | horizon (d) |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['symbol']} | {r['predicted_return_pct']:.2f} | "
            f"{r['actual_return_at_horizon']:.2f} | "
            f"{r['error_pct']:+.2f} | {r['predicted_horizon_days']} |"
        )
    return "\n".join(out)
```

Wire it in:

```python
    parts.append(_render_holdings_health(inputs.holdings_health))
    parts.append(_render_open_trades(conn))
    if mode == "post_close":
        parts.append(_render_matured_predictions(conn, as_of))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm_context.py -v`
Expected: 10 passing.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/context.py tests/test_llm_context.py
git commit -m "feat(llm): render open-trades + matured-predictions sections (12.1)"
```

---

## Task 6: Syrupy snapshot tests for full bundle (both modes)

**Files:**
- Modify: `tests/test_llm_context.py`
- Created (auto): `tests/__snapshots__/test_llm_context.ambr`

- [ ] **Step 1: Write the failing snapshot tests**

```python
# tests/test_llm_context.py — append
from freezegun import freeze_time


@freeze_time("2026-05-15T08:30:00")
def test_full_pre_open_bundle_snapshot(
    conn: sqlite3.Connection, paths, snapshot
) -> None:
    _seed_macro(conn)
    _seed_news(conn, "RVNL")
    _seed_open_trade(conn)
    health = HealthScore(
        symbol="TATAPOWER", verdict="TRIM", score=22, net_votes=-2,
        votes_cast=8, reasons=["below 200-DMA", "RSI 38"], pnl_pct=-3.2,
    )
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(
            candidates=[_candidate("RVNL", n_passed=9)],
            holdings_health=[health],
        ),
    )
    assert out.read_text(encoding="utf-8") == snapshot


@freeze_time("2026-05-15T16:30:00")
def test_full_post_close_bundle_snapshot(
    conn: sqlite3.Connection, paths, snapshot
) -> None:
    _seed_macro(conn)
    _seed_open_trade(conn)
    _seed_matured_prediction(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="post_close",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    assert out.read_text(encoding="utf-8") == snapshot
```

- [ ] **Step 2: Run to record snapshots**

Run: `pytest tests/test_llm_context.py -k "snapshot" --snapshot-update -v`
Expected: 2 passing, snapshots written under `tests/__snapshots__/test_llm_context.ambr`. Inspect the diff before committing — the snapshot text is the contract.

- [ ] **Step 3: Re-run without `--snapshot-update`**

Run: `pytest tests/test_llm_context.py -v`
Expected: all 12 passing.

- [ ] **Step 4: Verify lint + types**

Run: `ruff check src/trading/llm tests/test_llm_context.py && mypy src/trading/llm`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/test_llm_context.py tests/__snapshots__/test_llm_context.ambr
git commit -m "test(llm): syrupy snapshots for full pre_open + post_close bundles (12.4)"
```

---

## Task 7: Stub `llm/briefing.py` — `MissingNarrativeError`, `expected_parts`

**Files:**
- Create: `src/trading/llm/briefing.py`
- Modify: `src/trading/llm/__init__.py`
- Create: `tests/test_llm_briefing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_briefing.py
"""Tests for trading.llm.briefing — narrative-part assembly into brief.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading.llm.briefing import (
    MissingNarrativeError,
    compile_brief,
    expected_parts,
)


def test_expected_parts_pre_open() -> None:
    parts = expected_parts("pre_open", candidate_symbols=["RVNL", "NTPC"])
    assert parts == [
        "macro_brief.md",
        "sector_commentary.md",
        "candidates/RVNL.md",
        "candidates/NTPC.md",
    ]


def test_expected_parts_post_close() -> None:
    parts = expected_parts("post_close", candidate_symbols=["RVNL"])
    assert parts == [
        "macro_brief.md",
        "sector_commentary.md",
        "candidates/RVNL.md",
        "post_close_recap.md",
    ]


def test_compile_brief_raises_when_parts_missing(tmp_path: Path) -> None:
    date_dir = tmp_path / "2026-05-15"
    date_dir.mkdir()
    (date_dir / "_context.md").write_text(
        "# Trading context bundle — 2026-05-15  (mode: pre_open)\n"
        "\n## Today's candidates\n\n### RVNL — passes 9/10 rules\n",
        encoding="utf-8",
    )
    with pytest.raises(MissingNarrativeError) as exc_info:
        compile_brief(date_dir, mode="pre_open")
    msg = str(exc_info.value)
    assert "macro_brief.md" in msg
    assert "sector_commentary.md" in msg
    assert "candidates/RVNL.md" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm_briefing.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/llm/briefing.py
"""Phase 12 narrative-part assembler.

Reads analyst-produced narrative files from `data/research/YYYY-MM-DD/`
and concatenates them into a single `brief.md`. Symbol list is parsed from
the bundle's "## Today's candidates" section so orphan candidate files
(symbols not in the bundle) are skipped with a warning.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from trading.llm.context import Mode


class MissingNarrativeError(RuntimeError):
    """Raised by `compile_brief` when one or more expected parts are absent."""


def expected_parts(mode: Mode, candidate_symbols: list[str]) -> list[str]:
    """Return the relative paths the analyst is expected to have written."""
    parts = ["macro_brief.md", "sector_commentary.md"]
    parts.extend(f"candidates/{sym}.md" for sym in candidate_symbols)
    if mode == "post_close":
        parts.append("post_close_recap.md")
    return parts


_CANDIDATE_HEADING = re.compile(r"^### ([A-Z0-9_]+) — passes \d+/\d+ rules", re.MULTILINE)


def _parse_candidate_symbols(context_md: str) -> list[str]:
    return _CANDIDATE_HEADING.findall(context_md)


def _infer_mode(context_md: str) -> Mode:
    if "(mode: post_close)" in context_md:
        return "post_close"
    return "pre_open"


def compile_brief(date_dir: Path, *, mode: Mode | None = None) -> Path:
    """Read narrative parts in `date_dir`, write `brief.md`, return its path.

    Raises `MissingNarrativeError` listing any expected parts that are
    absent. Orphan candidate files (symbols not in the bundle) are skipped
    with a stderr warning. If `mode` is None, it is inferred from the
    bundle header.
    """
    context_path = date_dir / "_context.md"
    if not context_path.is_file():
        raise MissingNarrativeError(
            f"Cannot compile brief: bundle is missing at {context_path}. "
            "Run `trading brief assemble-context` first."
        )
    context_md = context_path.read_text(encoding="utf-8")
    if mode is None:
        mode = _infer_mode(context_md)
    symbols = _parse_candidate_symbols(context_md)
    expected = expected_parts(mode, symbols)
    missing = [p for p in expected if not (date_dir / p).is_file()]
    if missing:
        raise MissingNarrativeError(
            "Missing analyst narrative files: " + ", ".join(missing)
        )

    # Concatenation logic added in Task 8.
    out_path = date_dir / "brief.md"
    out_path.write_text("", encoding="utf-8")
    return out_path
```

```python
# src/trading/llm/__init__.py — replace
"""Public surface for the LLM analyst pipeline (spec §4.3)."""

from trading.llm.briefing import (
    MissingNarrativeError,
    compile_brief,
    expected_parts,
)
from trading.llm.context import ContextInputs, Mode, assemble_context

__all__ = [
    "ContextInputs",
    "MissingNarrativeError",
    "Mode",
    "assemble_context",
    "compile_brief",
    "expected_parts",
]
```

Note: `Mode` is defined once in `context.py` and re-imported by `briefing.py` so there's a single source of truth.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm_briefing.py -v && pytest tests/test_llm_context.py -v`
Expected: briefing 3 passing, context still 12 passing.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/briefing.py src/trading/llm/__init__.py tests/test_llm_briefing.py
git commit -m "feat(llm): MissingNarrativeError + expected_parts + compile_brief stub (12.3)"
```

---

## Task 8: `compile_brief` happy path with snapshot

**Files:**
- Modify: `src/trading/llm/briefing.py`
- Modify: `tests/test_llm_briefing.py`

- [ ] **Step 1: Write the failing snapshot test**

```python
# tests/test_llm_briefing.py — append
from freezegun import freeze_time


def _write_part(date_dir: Path, rel: str, body: str) -> None:
    p = date_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@freeze_time("2026-05-15T09:00:00")
def test_compile_brief_pre_open_happy_path_snapshot(
    tmp_path: Path, snapshot
) -> None:
    date_dir = tmp_path / "2026-05-15"
    date_dir.mkdir()
    _write_part(
        date_dir,
        "_context.md",
        "# Trading context bundle — 2026-05-15  (mode: pre_open)\n"
        "\n_Assembled at 2026-05-15T08:30:00._\n"
        "\n## Today's candidates\n\n"
        "### RVNL — passes 9/10 rules\n"
        "### NTPC — passes 8/10 rules\n",
    )
    _write_part(date_dir, "macro_brief.md", "Regime is NEUTRAL. VIX 19.4 …\n")
    _write_part(date_dir, "sector_commentary.md", "PSU/Infra LEADING …\n")
    _write_part(date_dir, "candidates/RVNL.md",
        "# RVNL — Conviction: HIGH\n\n## Bullish case\n…\n\n"
        "## Bearish case / risks\n…\n\n"
        "## Event risks in 25-day horizon\n- 2026-05-22: results — …\n")
    _write_part(date_dir, "candidates/NTPC.md",
        "# NTPC — Conviction: MEDIUM\n\n## Bullish case\n…\n\n"
        "## Bearish case / risks\n…\n\n"
        "## Event risks in 25-day horizon\n- (none)\n")

    out = compile_brief(date_dir, mode="pre_open")
    assert out == date_dir / "brief.md"
    assert out.read_text(encoding="utf-8") == snapshot
```

- [ ] **Step 2: Run to verify it fails (empty body)**

Run: `pytest tests/test_llm_briefing.py::test_compile_brief_pre_open_happy_path_snapshot -v`
Expected: FAIL — `brief.md` is empty.

- [ ] **Step 3: Implement concatenation in `compile_brief`**

Replace the placeholder write in `compile_brief`:

```python
    from datetime import datetime
    sections: list[str] = [
        f"# Daily brief — {date_dir.name}",
        f"_Compiled at {datetime.now().isoformat(timespec='seconds')} from "
        f"{len(expected)} narrative parts._",
        "",
        "## Macro",
        (date_dir / "macro_brief.md").read_text(encoding="utf-8").strip(),
        "",
        "## Sector commentary",
        (date_dir / "sector_commentary.md").read_text(encoding="utf-8").strip(),
        "",
        "## Candidates",
    ]
    for sym in symbols:
        body = (date_dir / "candidates" / f"{sym}.md").read_text(encoding="utf-8")
        sections.append("")
        sections.append(body.strip())
    if mode == "post_close":
        sections.append("")
        sections.append("## Post-close recap")
        sections.append(
            (date_dir / "post_close_recap.md").read_text(encoding="utf-8").strip()
        )
    out_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return out_path
```

- [ ] **Step 4: Record snapshot, then verify**

Run: `pytest tests/test_llm_briefing.py -k snapshot --snapshot-update -v && pytest tests/test_llm_briefing.py -v`
Expected: snapshot recorded, all 4 tests passing on second run.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/briefing.py tests/test_llm_briefing.py tests/__snapshots__/test_llm_briefing.ambr
git commit -m "feat(llm): compile_brief concatenation + snapshot test (12.3)"
```

---

## Task 9: `compile_brief` orphan candidate warning + post_close mode

**Files:**
- Modify: `src/trading/llm/briefing.py`
- Modify: `tests/test_llm_briefing.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_briefing.py — append
def test_compile_brief_warns_on_orphan_candidate_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    date_dir = tmp_path / "2026-05-15"
    date_dir.mkdir()
    _write_part(
        date_dir, "_context.md",
        "# Trading context bundle — 2026-05-15  (mode: pre_open)\n"
        "\n## Today's candidates\n\n### RVNL — passes 9/10 rules\n",
    )
    _write_part(date_dir, "macro_brief.md", "x")
    _write_part(date_dir, "sector_commentary.md", "x")
    _write_part(date_dir, "candidates/RVNL.md", "# RVNL — Conviction: HIGH\n")
    _write_part(date_dir, "candidates/IRCTC.md", "# IRCTC — Conviction: HIGH\n")
    compile_brief(date_dir, mode="pre_open")
    err = capsys.readouterr().err
    assert "IRCTC.md" in err and "orphan" in err.lower()


@freeze_time("2026-05-15T17:00:00")
def test_compile_brief_post_close_includes_recap(
    tmp_path: Path, snapshot
) -> None:
    date_dir = tmp_path / "2026-05-15"
    date_dir.mkdir()
    _write_part(
        date_dir, "_context.md",
        "# Trading context bundle — 2026-05-15  (mode: post_close)\n"
        "\n## Today's candidates\n\n_(no data)_\n",
    )
    _write_part(date_dir, "macro_brief.md", "Regime closed at NEUTRAL.\n")
    _write_part(date_dir, "sector_commentary.md", "PSU/Infra strong.\n")
    _write_part(date_dir, "post_close_recap.md",
        "Day's market: flat. Predictions averaged 1.2% error.\n")
    out = compile_brief(date_dir, mode="post_close")
    assert out.read_text(encoding="utf-8") == snapshot
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_llm_briefing.py -v -k "orphan or post_close"`
Expected: orphan test FAIL (no warning emitted yet); post_close FAIL (snapshot not recorded).

- [ ] **Step 3: Add orphan-warning logic**

In `compile_brief`, after `if missing: raise …`:

```python
    candidates_dir = date_dir / "candidates"
    if candidates_dir.is_dir():
        expected_syms = set(symbols)
        for f in sorted(candidates_dir.iterdir()):
            if f.suffix == ".md" and f.stem not in expected_syms:
                print(
                    f"warning: orphan candidate file {f.relative_to(date_dir)} "
                    f"(symbol not in bundle) — skipped",
                    file=sys.stderr,
                )
```

- [ ] **Step 4: Record snapshot, re-run all briefing tests**

Run: `pytest tests/test_llm_briefing.py -k snapshot --snapshot-update -v && pytest tests/test_llm_briefing.py -v`
Expected: all 6 tests passing.

- [ ] **Step 5: Commit**

```bash
git add src/trading/llm/briefing.py tests/test_llm_briefing.py tests/__snapshots__/test_llm_briefing.ambr
git commit -m "feat(llm): compile_brief orphan-warning + post_close recap (12.3)"
```

---

## Task 10: CLI subcommands `trading brief assemble-context` and `trading brief compile`

**Files:**
- Modify: `src/trading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py — append
import sqlite3
from datetime import date


def _init_db(tmp_path: Path) -> Path:
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations
    db_path = tmp_path / "data" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        run_migrations(conn)
    return db_path


def test_brief_assemble_context_writes_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app,
        ["brief", "assemble-context", "--date", "2026-05-15", "--mode", "pre_open"],
    )
    assert result.exit_code == 0, result.stdout
    out = tmp_path / "data" / "research" / "2026-05-15" / "_context.md"
    assert out.is_file()
    assert "now run /analyst" in result.stdout


def test_brief_compile_assembles_brief(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    date_dir = tmp_path / "data" / "research" / "2026-05-15"
    date_dir.mkdir(parents=True)
    (date_dir / "_context.md").write_text(
        "# Trading context bundle — 2026-05-15  (mode: pre_open)\n"
        "\n## Today's candidates\n\n_(no data)_\n",
        encoding="utf-8",
    )
    (date_dir / "macro_brief.md").write_text("x\n", encoding="utf-8")
    (date_dir / "sector_commentary.md").write_text("x\n", encoding="utf-8")
    result = runner.invoke(
        app, ["brief", "compile", "--date", "2026-05-15"]
    )
    assert result.exit_code == 0, result.stdout
    assert (date_dir / "brief.md").is_file()
    assert "brief.md" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k "brief_"`
Expected: FAIL — no such subcommand.

- [ ] **Step 3: Implement the CLI**

In `src/trading/cli.py`, after the existing imports add:

```python
from trading.llm.briefing import MissingNarrativeError, compile_brief
from trading.llm.context import ContextInputs, assemble_context as _assemble_context
```

Define a sub-app and two commands at the bottom of the file (before the `if __name__` block, if any):

```python
brief_app = typer.Typer(help="Daily-brief context assembly + compilation (Phase 12).")
app.add_typer(brief_app, name="brief")


@brief_app.command("assemble-context")
def brief_assemble_context_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
    mode: Annotated[str, typer.Option("--mode", help="pre_open or post_close")] = "pre_open",
) -> None:
    """Write data/research/YYYY-MM-DD/_context.md for the analyst skill."""
    if mode not in ("pre_open", "post_close"):
        console.print(f"[red]invalid --mode: {mode}[/red]")
        raise typer.Exit(code=2)
    as_of = date.fromisoformat(date_str)
    paths = get_paths()
    with get_conn(paths.db_path) as conn:
        out = _assemble_context(
            conn=conn, paths=paths, as_of=as_of, mode=mode,
            inputs=ContextInputs(),
        )
    console.print(f"[green]wrote[/green] {out}")
    console.print("[bold]now run /analyst skill in Claude Code[/bold]")


@brief_app.command("compile")
def brief_compile_cmd(
    date_str: Annotated[str, typer.Option("--date", help="ISO date YYYY-MM-DD")],
) -> None:
    """Concatenate analyst-produced narrative parts into brief.md."""
    paths = get_paths()
    date_dir = paths.research_dir / date_str
    try:
        out = compile_brief(date_dir)
    except MissingNarrativeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    console.print(f"[green]wrote[/green] {out}")
```

- [ ] **Step 4: Run tests + smoke `--help`**

Run: `pytest tests/test_cli.py -v && uv run trading brief --help`
Expected: tests pass; `trading brief --help` lists `assemble-context` and `compile`.

- [ ] **Step 5: Commit**

```bash
git add src/trading/cli.py tests/test_cli.py
git commit -m "feat(cli): trading brief assemble-context + compile (12.1, 12.3)"
```

---

## Task 11: Create the `/analyst` skill

**Files:**
- Create: `.claude/skills/analyst/SKILL.md`
- Create: `.claude/skills/analyst/references/output-templates.md`

No tests — the skill prompt is not unit-tested per spec §6.

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: analyst
description: Use when invoked at /analyst or when the user asks for the daily LLM analyst brief on a context bundle written by `trading brief assemble-context`. Reads data/research/YYYY-MM-DD/_context.md and writes macro_brief.md, sector_commentary.md, candidates/{SYMBOL}.md, and (post_close mode only) post_close_recap.md.
---

# /analyst — daily LLM analyst

You are the LLM analyst layer for this trading system. Phase 13's `pre_open` job has already written a context bundle. Your job is to read it and produce narrative outputs.

## Inputs

Find the most recent `_context.md` under `data/research/YYYY-MM-DD/` (use today's date in `Asia/Kolkata`). The header tells you `mode` (`pre_open` or `post_close`) and the assembly timestamp.

## Refuse-stale check

If the bundle's "_Assembled at_" timestamp is more than 12 hours old relative to now, do NOT write outputs. Tell the user to re-run `trading brief assemble-context --date <today>` first.

## Outputs

Write each file under the same `data/research/YYYY-MM-DD/` directory. Follow the skeletons in `references/output-templates.md` exactly — `compile_brief` parses fixed headings.

| Mode       | Files to write |
|------------|----------------|
| pre_open   | `macro_brief.md`, `sector_commentary.md`, `candidates/{SYMBOL}.md` for every symbol in the bundle's "## Today's candidates" section |
| post_close | `macro_brief.md`, `sector_commentary.md`, `candidates/{SYMBOL}.md` for every candidate (if any), `post_close_recap.md` |

## Style rules

- Evidence-first. Cite numbers from the bundle (RSI, ATR, sentiment scores, regime votes). Never invent data.
- Concise. `macro_brief.md` ≤ 120 words; per-stock cases 3-4 sentences each.
- If a section's source was `_(no data)_` in the bundle, write a one-line "not classified today — review needed" rather than fabricating prose.
- Conviction (HIGH / MEDIUM / LOW) on each candidate must be justified in the bullish or bearish case body. HIGH means ≥ 8/10 rules pass + non-negative sentiment + no critical news.

## After writing

Print a summary to the user listing every file written. Then suggest:

> "Now run `trading brief compile --date YYYY-MM-DD` to assemble brief.md."

## When the bundle is missing

If `_context.md` is absent for the requested date, do not guess. Tell the user to run `trading brief assemble-context --date YYYY-MM-DD --mode {pre_open|post_close}` first.
```

- [ ] **Step 2: Write `references/output-templates.md`**

```markdown
# Output templates for /analyst

`compile_brief` parses fixed headings. Use these skeletons exactly.

## `macro_brief.md`

```
Regime is **{RISK_ON|NEUTRAL|RISK_OFF}** ({score} / 4). {1-2 sentences citing
VIX, FII flow, USDINR Δ from the bundle.} {1 sentence on what this implies
for new positions today.}
```

If macro section was `_(no data)_`, write only:

```
Macro: not classified today — review needed.
```

## `sector_commentary.md`

One block per active sector:

```
### {SECTOR_NAME} — {LEADING|NEUTRAL|LAGGING}
- 5d relative strength: {value}%
- Driver: {one-line explanation}
```

## `candidates/{SYMBOL}.md`

```
# {SYMBOL} — Conviction: {HIGH|MEDIUM|LOW}

## Bullish case
{3-4 sentences citing rule pass count, sector strength, sentiment score.}

## Bearish case / risks
{3-4 sentences citing failed rules, drawdowns, negative news.}

## Event risks in 25-day horizon
- {YYYY-MM-DD}: {event} — {impact note}
- (or: "(none in horizon)" if nothing)
```

## `post_close_recap.md` (post_close mode only)

```
## Day's market
{2-3 sentences on the day's price action, regime moves, kill-switch firings.}

## Prediction-error analysis
{Commentary on the matured-predictions table from the bundle: average error,
notable hits and misses, calibration drift.}

## Kill-switch notes
- {Switch X}: fired / close-but-no-fire / quiet
```
````

- [ ] **Step 3: Verify the skill is discovered**

Manually inspect: `ls .claude/skills/analyst/` shows both files. The skill will be loaded by Claude Code on next session start; no test asserts its presence in this plan.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/analyst/SKILL.md .claude/skills/analyst/references/output-templates.md
git commit -m "feat(skill): analyst skill + output templates (12.2)"
```

---

## Task 12: Update PROGRESS.md and final commit + push

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Mark Phase 11 done in status table + sub-tasks**

In `PROGRESS.md`, change:

```
| 11 | Paper-trade ledger | `[ ]` |
| 12 | LLM analyst | `[ ]` |
```

to:

```
| 11 | Paper-trade ledger | `[x]` |
| 12 | LLM analyst | `[x]` |
```

And update the pointer:

```
**Currently working on:** _Phase 12 — LLM analyst_
**Next up:** _Phase 13 — pre_open job (MVP ⭐)_
```

Mark every Phase 11 sub-task `[x]` (commit `bd7dbd9` already shipped them).

- [ ] **Step 2: Rewrite the Phase 12 section**

Replace the existing `## Phase 12 — LLM analyst` block with:

```
## Phase 12 — LLM analyst (Claude Code skill version)

> **Spec deviation:** the original Anthropic-SDK plan was replaced by a
> project-level Claude Code skill (`/analyst`). User has Claude Pro plan
> but no API credits — paying twice for the same model wasn't worth it.
> See [`docs/superpowers/specs/2026-05-15-phase-12-llm-skill-design.md`]
> for full rationale.

- [x] 12.1 `src/trading/llm/context.py`: `ContextInputs` dataclass +
       `assemble_context(conn, paths, as_of, mode, inputs)` writes
       `_context.md` from DB (macro/sentiment/news/paper-trades/predictions)
       + ephemeral inputs (candidates from scanner, holdings_health from
       portfolio analyzer). Pure renderer; mode-conditional matured
       predictions section.
- [x] 12.2 `.claude/skills/analyst/SKILL.md` + `references/output-templates.md`
       — project-level skill that reads `_context.md`, refuses if stale > 12h,
       writes macro_brief.md, sector_commentary.md, candidates/{SYMBOL}.md,
       and (post_close) post_close_recap.md.
- [x] 12.3 `src/trading/llm/briefing.py`: `compile_brief(date_dir, mode)` +
       `expected_parts(mode, candidate_symbols)` + `MissingNarrativeError`.
       Concatenates parts into `brief.md` in fixed order; orphan candidate
       files printed as warnings.
- [x] 12.4 Tests: ~14 across `test_llm_context.py` (10 unit + 2 syrupy
       snapshots for both modes), `test_llm_briefing.py` (4 unit + 2 syrupy
       snapshots), `test_cli.py` (2 happy-path).
- [x] 12.5 N/A — cost tracking dropped (Claude Pro plan, no per-call cost).
- [x] 12.6 PROGRESS.md updated → commit `feat(llm): analyst skill + briefing
       pipeline (Phase 12)` and push to origin/main.
```

- [ ] **Step 3: Run full suite + lint + types**

Run: `ruff check . && mypy src/ && pytest -q`
Expected: clean. Report counts (e.g. "399 passed, 2 deselected") in the commit body.

- [ ] **Step 4: Commit**

```bash
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(llm): analyst skill + briefing pipeline (Phase 12)

Replaces the planned Anthropic-SDK layer with a project-level
Claude Code skill (/analyst) plus deterministic file-IO helpers.
User has Claude Pro plan covering Claude Code; building the SDK
wrapper would mean paying twice for the same model.

Phase 13 pre_open job will:
  1. trading brief assemble-context --date YYYY-MM-DD --mode pre_open
  2. user invokes /analyst skill (writes narrative parts)
  3. trading brief compile --date YYYY-MM-DD (writes brief.md)

Tests: <count> passed.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Push to origin/main (per standing rule)**

```bash
git push origin main
```

Expected: pushes Phase 12 commits + the earlier `c68758d` spec commit.

---

## Self-review notes

- **Spec coverage:** every spec §3 component → task; every §4 file → task; §5 error modes → tasks 7, 9; §6 testing matrix → tasks 6, 8, 9, 10; §7 sub-task rewrite → task 12. ✓
- **Type consistency:** `Mode` is a `Literal["pre_open", "post_close"]` declared in `context.py` and re-imported in `briefing.py`/`__init__.py`. `ContextInputs.candidates` is `list[Candidate]`, `holdings_health` is `list[HealthScore]` — both already-public types. CLI param name `date_str` (not `date`) avoids shadowing `datetime.date`. ✓
- **Placeholder scan:** no TBDs, every code block is complete. The bundle snapshot text itself isn't shown in the plan because it's auto-recorded by syrupy on first run; the plan calls for explicit inspection of the diff before committing. ✓
- **Frozen-time:** snapshot tests wrap `assemble_context` and `compile_brief` in `freeze_time` so the assembly/compile timestamps in the rendered output are deterministic. ✓

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-15-phase-12-llm-skill.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

**Which approach?**
