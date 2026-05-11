# Phase 0 — Project Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold the trading project so that `uv sync`, `ruff check .`, `mypy src/`, and `pytest -q` all run clean from a fresh clone, ready for Phase 1.

**Architecture:** Single-package Python project (`src/trading/`) managed by `uv`, with `ruff` + `mypy` + `pytest` all configured in `pyproject.toml`. Empty module tree mirrors spec Section 11. One smoke test confirms the package imports.

**Tech Stack:** Python 3.11 · `uv` · `ruff` · `mypy` · `pytest` · `hatchling` (build backend)

**Reference:** [docs/superpowers/specs/2026-05-11-trading-system-design.md](../specs/2026-05-11-trading-system-design.md) — Sections 11 (repo structure), 12 (tech stack), 14 (Phase 0 row)

---

## File Structure

Files this plan creates or modifies:

| Action | Path | Responsibility |
|---|---|---|
| Create | `.python-version` | Pin Python 3.11 for uv |
| Create | `pyproject.toml` | Project metadata, dependencies, ruff/mypy/pytest config |
| Modify | `.gitignore` | Append project-specific ignores |
| Create | `.env.example` | Template for required env vars |
| Create | `src/trading/__init__.py` | Package root, exposes `__version__` |
| Create | `src/trading/data/__init__.py` | Empty namespace marker |
| Create | `src/trading/store/__init__.py` | Empty namespace marker |
| Create | `src/trading/features/__init__.py` | Empty namespace marker |
| Create | `src/trading/strategy/__init__.py` | Empty namespace marker |
| Create | `src/trading/backtest/__init__.py` | Empty namespace marker |
| Create | `src/trading/paper/__init__.py` | Empty namespace marker |
| Create | `src/trading/portfolio/__init__.py` | Empty namespace marker |
| Create | `src/trading/llm/__init__.py` | Empty namespace marker |
| Create | `src/trading/jobs/__init__.py` | Empty namespace marker |
| Create | `src/trading/ui/__init__.py` | Empty namespace marker |
| Create | `src/trading/ui/pages/__init__.py` | Empty namespace marker |
| Create | `tests/__init__.py` | Empty namespace marker |
| Create | `tests/conftest.py` | Shared fixtures (just `fixtures_dir` for now) |
| Create | `tests/fixtures/.gitkeep` | Empty marker so the dir is tracked |
| Create | `tests/test_smoke.py` | Confirms `import trading` works |
| Modify | `PROGRESS.md` | Tick Phase 0 sub-tasks as completed |

No source code is written yet — this phase only sets the table. Subsequent phases (starting with Phase 1 — Config + SQLite schema) write the actual trading logic.

---

## Task 1: Pin Python version

**Files:**
- Create: `.python-version`

- [ ] **Step 1.1: Create `.python-version`**

Create `D:\Projects\Trading\.python-version` with exactly this content (single line, no trailing characters):

```
3.11
```

- [ ] **Step 1.2: Verify uv recognises the pin**

Run from `D:\Projects\Trading\`:

```powershell
uv python find
```

Expected: prints a path that ends in `python3.11.exe` or `python.exe` from a 3.11.x install. If uv reports "No interpreter found for Python 3.11", run `uv python install 3.11` and re-check.

---

## Task 2: Create `pyproject.toml`

**Files:**
- Create: `pyproject.toml`

- [ ] **Step 2.1: Write `pyproject.toml`**

Create `D:\Projects\Trading\pyproject.toml` with this exact content:

```toml
[project]
name = "trading"
version = "0.1.0"
description = "AI-assisted trading & portfolio intelligence for the Indian market"
readme = "README.md"
requires-python = ">=3.11,<3.13"
license = { text = "Proprietary" }
authors = [{ name = "Sandeep Kumar" }]

dependencies = [
    # --- Data & numerics ---
    "pandas>=2.2",
    "polars>=0.20",
    "pyarrow>=15",
    "numpy>=1.26,<2.0",          # pandas-ta is incompatible with numpy 2.x
    "pandas-ta>=0.3.14b0",

    # --- Sources ---
    "kiteconnect>=5.0",
    "yfinance>=0.2.40",
    "nsepython>=2.95",
    "feedparser>=6.0",
    "requests>=2.31",
    "requests-cache>=1.2",

    # --- ML / sentiment ---
    "lightgbm>=4.3",
    "scikit-learn>=1.4",
    "joblib>=1.4",
    "transformers>=4.40",
    "torch>=2.2",

    # --- Backtest ---
    "vectorbt>=0.26",

    # --- LLM ---
    "anthropic>=0.40",

    # --- UI ---
    "streamlit>=1.35",
    "plotly>=5.20",

    # --- Utilities ---
    "python-dotenv>=1.0",
    "typer>=0.12",
    "rich>=13.7",
    "loguru>=0.7",
    "pydantic>=2.7",
]

[project.scripts]
trading = "trading.cli:app"

[dependency-groups]
dev = [
    "ruff>=0.4",
    "mypy>=1.10",
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "pytest-mock>=3.14",
    "syrupy>=4.6",
    "freezegun>=1.5",
    "pandas-stubs",
    "types-requests",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/trading"]

# ---------------------------------------------------------------------------
# Ruff — linting + formatting
# ---------------------------------------------------------------------------
[tool.ruff]
line-length = 100
target-version = "py311"
src = ["src", "tests"]
extend-exclude = ["data", "Research"]

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort
    "B",    # flake8-bugbear
    "C4",   # flake8-comprehensions
    "UP",   # pyupgrade
    "N",    # pep8-naming
    "SIM",  # flake8-simplify
    "RUF",  # ruff-specific
]
ignore = [
    "E501",   # line too long (formatter handles it)
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]   # asserts are fine in tests

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

# ---------------------------------------------------------------------------
# mypy — strict on src/trading, lax on tests
# ---------------------------------------------------------------------------
[tool.mypy]
python_version = "3.11"
strict = true
ignore_missing_imports = true
warn_unused_ignores = true
files = ["src/trading"]

[[tool.mypy.overrides]]
module = "tests.*"
ignore_errors = true

# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
addopts = "-q --strict-markers"
markers = [
    "live: tests that hit live external APIs (Kite, news) — excluded from CI",
    "integration: end-to-end tests using cached fixtures",
    "slow: tests >5s — excluded from default run",
]
```

The `<2.0` ceiling on numpy is **load-bearing** — pandas-ta is incompatible with numpy 2.x as of writing.

- [ ] **Step 2.2: No verification yet**

We verify after `uv sync` in Task 3 — pyproject.toml is just a manifest at this stage.

---

## Task 3: Install dependencies with `uv sync`

**Files:** (no edits — `uv` writes `uv.lock` and `.venv/`)

- [ ] **Step 3.1: Run `uv sync`**

From `D:\Projects\Trading\`:

```powershell
uv sync
```

Expected output (abridged):
```
Resolved N packages in ...
Built N source distributions ...
Installed N packages in ...
```

This creates `.venv/` and writes `uv.lock`. Expect 3-10 minutes on first run because `torch` is ~2GB.

If `uv` complains it cannot find Python 3.11, run `uv python install 3.11` first then retry.

- [ ] **Step 3.2: Confirm `uv.lock` exists**

```powershell
Test-Path uv.lock
```

Expected: `True`.

- [ ] **Step 3.3: Confirm we can run Python from the venv**

```powershell
uv run python --version
```

Expected: `Python 3.11.x` (any patch level).

---

## Task 4: Create the `src/trading/` module tree

**Files:**
- Create: `src/trading/__init__.py`
- Create: `src/trading/data/__init__.py`
- Create: `src/trading/store/__init__.py`
- Create: `src/trading/features/__init__.py`
- Create: `src/trading/strategy/__init__.py`
- Create: `src/trading/backtest/__init__.py`
- Create: `src/trading/paper/__init__.py`
- Create: `src/trading/portfolio/__init__.py`
- Create: `src/trading/llm/__init__.py`
- Create: `src/trading/jobs/__init__.py`
- Create: `src/trading/ui/__init__.py`
- Create: `src/trading/ui/pages/__init__.py`

- [ ] **Step 4.1: Create the package root `__init__.py`**

Create `D:\Projects\Trading\src\trading\__init__.py` with:

```python
"""trading — AI-assisted trading & portfolio intelligence system."""

__version__ = "0.1.0"
```

- [ ] **Step 4.2: Create each sub-package `__init__.py` as an empty file**

For each of the following paths, create an empty file (zero bytes):

```
src/trading/data/__init__.py
src/trading/store/__init__.py
src/trading/features/__init__.py
src/trading/strategy/__init__.py
src/trading/backtest/__init__.py
src/trading/paper/__init__.py
src/trading/portfolio/__init__.py
src/trading/llm/__init__.py
src/trading/jobs/__init__.py
src/trading/ui/__init__.py
src/trading/ui/pages/__init__.py
```

PowerShell one-liner that creates all of them at once:

```powershell
$paths = @(
  "src/trading/data/__init__.py",
  "src/trading/store/__init__.py",
  "src/trading/features/__init__.py",
  "src/trading/strategy/__init__.py",
  "src/trading/backtest/__init__.py",
  "src/trading/paper/__init__.py",
  "src/trading/portfolio/__init__.py",
  "src/trading/llm/__init__.py",
  "src/trading/jobs/__init__.py",
  "src/trading/ui/__init__.py",
  "src/trading/ui/pages/__init__.py"
)
foreach ($p in $paths) {
  New-Item -ItemType File -Path $p -Force | Out-Null
}
```

- [ ] **Step 4.3: Verify the import path works**

```powershell
uv run python -c "import trading; print(trading.__version__)"
```

Expected: `0.1.0`.

---

## Task 5: Create the `tests/` structure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/fixtures/.gitkeep`
- Create: `tests/test_smoke.py`

- [ ] **Step 5.1: Create `tests/__init__.py` (empty file)**

Create `D:\Projects\Trading\tests\__init__.py` as an empty file.

- [ ] **Step 5.2: Create `tests/conftest.py`**

Create `D:\Projects\Trading\tests\conftest.py` with:

```python
"""Shared pytest fixtures for the trading project."""

from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the tests/fixtures/ directory."""
    return Path(__file__).parent / "fixtures"
```

- [ ] **Step 5.3: Create `tests/fixtures/.gitkeep`**

Create `D:\Projects\Trading\tests\fixtures\.gitkeep` as an empty file (so the directory is tracked even though it's empty).

- [ ] **Step 5.4: Create `tests/test_smoke.py`**

Create `D:\Projects\Trading\tests\test_smoke.py` with:

```python
"""Smoke test — confirms the package imports cleanly and tooling is wired up."""

from pathlib import Path

import trading


def test_package_version() -> None:
    assert trading.__version__ == "0.1.0"


def test_fixtures_dir_resolves(fixtures_dir: Path) -> None:
    assert fixtures_dir.name == "fixtures"
    assert fixtures_dir.is_dir()
```

- [ ] **Step 5.5: Run the smoke test**

```powershell
uv run pytest tests/test_smoke.py -v
```

Expected output ends with:

```
tests/test_smoke.py::test_package_version PASSED
tests/test_smoke.py::test_fixtures_dir_resolves PASSED
2 passed in ...s
```

If a test fails, the most likely cause is a typo in `__init__.py` — re-check Task 4.

---

## Task 6: Create `.env.example`

**Files:**
- Create: `.env.example`

- [ ] **Step 6.1: Write `.env.example`**

Create `D:\Projects\Trading\.env.example` with:

```
# ============================================================
# .env.example — copy to .env and fill in your real values.
# .env is gitignored. Do NOT commit secrets.
# ============================================================

# --- Anthropic (Claude API) ---
# Get from https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-your-key-here

# --- Zerodha Kite Connect ---
# Get API key + secret from https://kite.trade/connect/
KITE_API_KEY=your-kite-api-key
KITE_API_SECRET=your-kite-api-secret
# Access token rotates daily — leave blank, populated after browser login
KITE_ACCESS_TOKEN=

# --- Runtime ---
LOG_LEVEL=INFO

# --- Optional: news scraper user agent ---
NEWS_USER_AGENT=trading-bot/0.1
```

---

## Task 7: Extend `.gitignore`

**Files:**
- Modify: `.gitignore`

- [ ] **Step 7.1: Append project-specific section to `.gitignore`**

The existing `.gitignore` is the generic Python template. Append the following block at the end of `D:\Projects\Trading\.gitignore`:

```
# ============================================================
# Project-specific
# ============================================================

# Local databases & caches (regeneratable)
data/app.db
data/app.db-journal
data/parquet/
data/cache/
data/logs/

# ML model artifacts (large, regeneratable)
models/*.pkl
models/*.joblib
models/*.bin

# Hugging Face cache (FinBERT lives here once downloaded)
.cache/huggingface/

# Streamlit
.streamlit/secrets.toml

# Editor / IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db
```

Note: do NOT ignore `uv.lock` (we commit it for reproducibility) and do NOT ignore `.python-version` (we commit it to pin the interpreter).

- [ ] **Step 7.2: Verify ignores apply**

```powershell
git status --ignored
```

Expected: under "Ignored files" you should see `.env` (when present), `.venv/`, and once Phase 1 lands, `data/app.db`. For now, mainly `.venv/` and any pyc caches.

---

## Task 8: Verify the toolchain runs clean

- [ ] **Step 8.1: Run ruff lint**

```powershell
uv run ruff check .
```

Expected: `All checks passed!` (or no output, depending on ruff version).

If failures appear, fix and re-run. Common issues at this stage:
- Unused imports in `__init__.py` files (shouldn't happen — they're empty)
- Format issues in `conftest.py` — run `uv run ruff format .` to auto-fix

- [ ] **Step 8.2: Run ruff format check**

```powershell
uv run ruff format --check .
```

Expected: `N files already formatted` with no diffs proposed. If diffs are proposed, run `uv run ruff format .` to apply them, then re-run with `--check`.

- [ ] **Step 8.3: Run mypy**

```powershell
uv run mypy src/
```

Expected: `Success: no issues found in N source files`.

If mypy complains about missing imports for third-party libs without stubs, that's already handled via `ignore_missing_imports = true` in pyproject. If it surfaces, double-check the `[tool.mypy]` block.

- [ ] **Step 8.4: Run the full pytest suite**

```powershell
uv run pytest -q
```

Expected:

```
..                                                                       [100%]
2 passed in ...s
```

- [ ] **Step 8.5: Verify the `trading` CLI script entry point resolves**

The `pyproject.toml` declares a `trading` entry point that targets `trading.cli:app`. That module doesn't exist yet — verify the entry is registered but expect an import error if invoked:

```powershell
uv run trading --help
```

Expected: an `ModuleNotFoundError: No module named 'trading.cli'` (or similar). **This is intentional** — `cli.py` lands in Phase 5. We just need to confirm `uv` knows about the entry point.

---

## Task 9: Update `PROGRESS.md`

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 9.1: Mark all Phase 0 sub-tasks as complete**

In `D:\Projects\Trading\PROGRESS.md`:

1. Change the Phase 0 status snapshot row from `[ ]` to `[x]`
2. Tick every sub-task `0.1` through `0.11` from `[ ]` to `[x]`
3. Update the "Currently working on" line from "Phase 0 — Project Setup" to "Phase 1 — Config + SQLite schema"
4. Update "Next up" from "Phase 1 — Config + SQLite schema" to "Phase 2 — Historical OHLCV (yfinance)"

For example, the status snapshot's Phase 0 row becomes:

```markdown
| 0 | Project setup | `[x]` |
```

And the Phase 0 task list becomes:

```markdown
- [x] 0.1 `uv init` in repo root; pin Python 3.11 via `.python-version`
- [x] 0.2 Author `pyproject.toml` with prod + dev dependency groups (see spec Section 12)
- [x] 0.3 Configure `ruff` rules in `pyproject.toml`
- [x] 0.4 Configure `mypy` (strict on `src/trading/`)
- [x] 0.5 Configure `pytest` (markers: `live`, `integration`, `slow`)
- [x] 0.6 Create `src/trading/` module tree per spec Section 11 (empty `__init__.py` files)
- [x] 0.7 Create `tests/` with `conftest.py` and `fixtures/` placeholder
- [x] 0.8 Author `.env.example` with all keys from spec Section 12
- [x] 0.9 Extend `.gitignore` for `data/app.db`, `data/parquet/`, `data/cache/`, `.env`, `models/*.pkl`
- [x] 0.10 Verify clean: `pytest -q` · `ruff check .` · `mypy src/`
- [x] 0.11 Update PROGRESS.md → commit `chore: scaffold project`
```

---

## Task 10: Commit

**Files:** (no edits — git operation only)

- [ ] **Step 10.1: Stage the new files**

From `D:\Projects\Trading\`:

```powershell
git add .python-version pyproject.toml uv.lock .env.example .gitignore src/ tests/ PROGRESS.md
```

- [ ] **Step 10.2: Verify staged contents**

```powershell
git status
```

Expected: changes to `.gitignore` and `PROGRESS.md`; new files for `.python-version`, `pyproject.toml`, `uv.lock`, `.env.example`, the `src/trading/` tree, and the `tests/` tree.

**Do NOT stage** `.env`, `.venv/`, or any file matching gitignore patterns.

- [ ] **Step 10.3: Commit**

```powershell
git commit -m "chore: scaffold project (Phase 0)

- Pin Python 3.11 (.python-version)
- Author pyproject.toml with all spec Section 12 deps
- Configure ruff, mypy strict, pytest with markers
- Create src/trading/ module tree
- Author .env.example
- Extend .gitignore for data/, models/, HF cache
- Smoke test confirms package imports

Closes: Phase 0 of docs/superpowers/specs/2026-05-11-trading-system-design.md"
```

**Wait for the user's explicit go-ahead before running this commit** — per project policy (CLAUDE.md / repo convention), commits are only created when the user asks. If the user has not explicitly approved committing, leave the work staged and surface the diff for review instead.

- [ ] **Step 10.4: Confirm the working tree is clean**

```powershell
git status
```

Expected: `nothing to commit, working tree clean`.

---

## Done — Phase 0 acceptance criteria

A reviewer should be able to confirm Phase 0 is complete by running these four commands from a fresh clone and seeing no failures:

```powershell
uv sync
uv run ruff check .
uv run mypy src/
uv run pytest -q
```

If all four pass clean, Phase 0 is done. Proceed to **Phase 1 — Config + SQLite schema** (separate plan to be written).
