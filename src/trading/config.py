"""Configuration: filesystem paths, environment-loaded settings, constants.

Two dataclasses:
- `Paths`  — read-only filesystem layout, derived from the project root.
- `Settings` — read-only secrets/config, loaded from `.env` + environment.

Both are frozen so they can be passed around safely without surprise mutation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TIMEZONE = "Asia/Kolkata"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_NEWS_USER_AGENT = "trading-bot/0.1"


@dataclass(frozen=True)
class Paths:
    """Project directory layout."""

    project_root: Path
    data_dir: Path
    parquet_dir: Path
    cache_dir: Path
    logs_dir: Path
    research_dir: Path
    raw_dir: Path
    models_dir: Path
    db_path: Path


@dataclass(frozen=True)
class Settings:
    """Secrets and runtime config loaded from environment."""

    anthropic_api_key: str | None
    kite_api_key: str | None
    kite_api_secret: str | None
    kite_access_token: str | None
    log_level: str
    news_user_agent: str


def _detect_project_root() -> Path:
    """Walk up from this file until we find pyproject.toml."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    # Fallback: two levels up from this file (src/trading/config.py → repo)
    return here.parents[2]


def get_paths(root: Path | None = None) -> Paths:
    """Return the project's filesystem layout.

    Resolution order: explicit `root` arg → `TRADING_PROJECT_ROOT` env var
    (used by tests) → auto-detect by walking up to `pyproject.toml`.
    """
    if root is not None:
        project_root = root
    elif env_root := os.environ.get("TRADING_PROJECT_ROOT"):
        project_root = Path(env_root)
    else:
        project_root = _detect_project_root()
    data_dir = project_root / "data"
    return Paths(
        project_root=project_root,
        data_dir=data_dir,
        parquet_dir=data_dir / "parquet",
        cache_dir=data_dir / "cache",
        logs_dir=data_dir / "logs",
        research_dir=data_dir / "research",
        raw_dir=data_dir / "raw",
        models_dir=project_root / "models",
        db_path=data_dir / "app.db",
    )


def get_settings(*, load_dotenv: bool = True) -> Settings:
    """Load `.env` (if requested) and return a frozen Settings snapshot.

    Pass `load_dotenv=False` in tests that already populate `os.environ` to avoid
    the on-disk `.env` clobbering monkeypatched values.
    """
    if load_dotenv:
        env_path = get_paths().project_root / ".env"
        if env_path.is_file():
            _load_env_file(env_path)
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        kite_api_key=os.environ.get("KITE_API_KEY") or None,
        kite_api_secret=os.environ.get("KITE_API_SECRET") or None,
        kite_access_token=os.environ.get("KITE_ACCESS_TOKEN") or None,
        log_level=os.environ.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL,
        news_user_agent=os.environ.get("NEWS_USER_AGENT") or DEFAULT_NEWS_USER_AGENT,
    )


def _load_env_file(path: Path) -> None:
    """Indirection so we can stub `python-dotenv` if needed in tests."""
    load_dotenv(dotenv_path=path, override=False)


def update_env_var(path: Path, key: str, value: str) -> None:
    """Idempotently set `KEY=VALUE` in a dotenv file.

    Creates the file if missing. If `KEY=` already exists, replaces that line
    in place. Otherwise appends a new line. Preserves all unrelated lines
    (comments, blanks, other keys) verbatim.
    """
    new_line = f"{key}={value}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_line + "\n", encoding="utf-8")
        return
    body = path.read_text(encoding="utf-8")
    lines = body.splitlines()
    prefix = f"{key}="
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
