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
    """Return the project's filesystem layout, anchored at `root` or autodetected."""
    project_root = root if root is not None else _detect_project_root()
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
