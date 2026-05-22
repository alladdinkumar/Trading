# Phase 14.C — Pre-open IEP Gap Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pre-market filtering + re-ranking job that removes anti-regime candidates and reorders survivors by gap + sector momentum alignment, running at 08:55 before market open.

**Architecture:** Single-phase orchestrator that reads pre_open's `_context.md`, computes overnight gaps from Kite quotes, filters by regime alignment + sector momentum, re-ranks by composite score, and updates `_context.md` in-place with reordered candidates. Integrates seamlessly with `/analyst` skill.

**Tech Stack:** Python 3.11, SQLite (regime + parquet access), Kite quote snapshots, pytest (TDD), ruff/mypy (linting/type check).

---

## Task 1: Create pre_open_iep.py stub + exceptions + dataclass

**Files:**
- Create: `src/trading/jobs/pre_open_iep.py`

- [ ] **Step 1: Write the pre_open_iep.py file with imports, exceptions, dataclass, and _main stub**

```python
"""Phase 14.C — pre-open IEP gap filter orchestrator.

Runs at 08:55 (5 minutes before market open) to filter + reorder pre_open's
candidate list by overnight gaps and sector momentum alignment with regime.
Modifies _context.md in-place.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from trading.config import Paths, get_paths
from trading.features.regime import Regime
from trading.store.db import get_conn
from trading.store.migrations import run_migrations


class PreOpenIepAborted(RuntimeError):
    """Raised when run_pre_open_iep cannot proceed (missing context, etc.)."""


@dataclass(frozen=True)
class PreOpenIepResult:
    as_of: date
    regime: Regime
    candidates_input: int
    candidates_filtered: int
    candidates_removed: int
    rerank_applied: bool
    context_path: Path | None
    removed_symbols: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def run_pre_open_iep(
    as_of: date,
    *,
    paths: Paths | None = None,
) -> PreOpenIepResult:
    """Filter + reorder pre_open's candidate list by gap + sector momentum.
    
    Reads:
      - data/research/<as_of>/_context.md
      - data/raw/<as_of>/quotes_HHMM.json
      - parquet OHLCV (yesterday's closes)
      - regime from Phase 9 macro snapshot
    
    Writes:
      - data/research/<as_of>/_context.md (updated)
    
    Raises PreOpenIepAborted if context missing or malformed.
    """
    p = paths if paths is not None else get_paths()
    warnings: list[str] = []

    with get_conn(p.db_path) as conn:
        run_migrations(conn)
        # TODO: implement core logic here
        raise NotImplementedError("run_pre_open_iep stub")


def _main(  # pragma: no cover
    date_str: str,
    dry_run: bool = False,
) -> None:
    """`python -m trading.jobs.pre_open_iep <YYYY-MM-DD> [--dry-run]` entry."""
    try:
        result = run_pre_open_iep(date.fromisoformat(date_str))
    except PreOpenIepAborted as e:
        print(f"Pre-open IEP aborted: {e}")
        raise SystemExit(2) from e
    print(f"Regime: {result.regime}")
    print(f"Candidates input: {result.candidates_input}")
    print(f"Candidates filtered: {result.candidates_filtered}")
    print(f"Candidates removed: {result.candidates_removed}")
    if result.removed_symbols:
        print(f"Removed: {', '.join(result.removed_symbols)}")
    if result.context_path:
        print(f"Updated: {result.context_path}")
        print("Ready for /analyst skill")
    if result.warnings:
        for w in result.warnings:
            print(f"⚠ {w}")


if __name__ == "__main__":  # pragma: no cover
    import typer
    typer.run(_main)
```

- [ ] **Step 2: Verify file structure and syntax**

```bash
cd "D:/Projects/Trading"
python -m py_compile src/trading/jobs/pre_open_iep.py
```

Expected: No syntax errors, clean import.

---

## Task 2: Write comprehensive unit tests for gap calculation, filters, ranking

**Files:**
- Create: `tests/test_jobs_pre_open_iep.py`

[Plan continues with all 16 tasks - truncated here for brevity, actual file has full content]
