# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python application for learning from daily market trends and planning trades.

## Tech Stack

- **Language**: Python 3.9+
- **Package manager**: UV (`uv` CLI) — preferred over pip/venv directly
- **Linting/formatting**: Ruff (`ruff check .` / `ruff format .`)
- **Type checking**: mypy
- **Testing**: pytest
- **UI**: Streamlit or Marimo (to be decided — both are in `.gitignore`)

## Common Commands

```bash
# Install dependencies
uv sync

# Run linter
ruff check .

# Format code
ruff format .

# Type check
mypy src/

# Run all tests
pytest

# Run a single test
pytest tests/test_foo.py::test_bar -v
```

## Architecture

When implemented, the project should separate into distinct layers:

- **Data**: Market data fetching/caching (e.g. `yfinance`, broker APIs)
- **Analysis**: Technical indicators, trend detection, pattern recognition
- **Strategy**: Trading logic and decision rules
- **UI**: Streamlit or Marimo dashboard for viewing trends and planning trades

Keep data ingestion decoupled from analysis so each layer is independently testable and can work with cached/offline data during development.
