"""Tests for trading.config — Paths and Settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading.config import TIMEZONE, Paths, Settings, get_paths, get_settings


def test_timezone_is_kolkata() -> None:
    assert TIMEZONE == "Asia/Kolkata"


def test_paths_default_root_is_repo_root() -> None:
    paths = get_paths()
    # The repo root is the parent of `src/`, which contains pyproject.toml
    assert (paths.project_root / "pyproject.toml").is_file()
    assert paths.project_root.name == "Trading"


def test_paths_db_default_location() -> None:
    paths = get_paths()
    assert paths.db_path == paths.data_dir / "app.db"


def test_paths_explicit_root(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    assert paths.project_root == tmp_path
    assert paths.data_dir == tmp_path / "data"
    assert paths.parquet_dir == tmp_path / "data" / "parquet"
    assert paths.cache_dir == tmp_path / "data" / "cache"
    assert paths.logs_dir == tmp_path / "data" / "logs"
    assert paths.research_dir == tmp_path / "data" / "research"
    assert paths.raw_dir == tmp_path / "data" / "raw"
    assert paths.models_dir == tmp_path / "models"
    assert paths.db_path == tmp_path / "data" / "app.db"


def test_paths_is_frozen() -> None:
    paths = get_paths()
    with pytest.raises((AttributeError, Exception)):
        paths.project_root = Path("/somewhere/else")  # type: ignore[misc]


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("KITE_API_KEY", "kk-test")
    monkeypatch.setenv("KITE_API_SECRET", "ks-test")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "kat-test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("NEWS_USER_AGENT", "test-agent/1.0")
    s = get_settings(load_dotenv=False)
    assert s.anthropic_api_key == "sk-ant-test"
    assert s.kite_api_key == "kk-test"
    assert s.kite_api_secret == "ks-test"
    assert s.kite_access_token == "kat-test"
    assert s.log_level == "DEBUG"
    assert s.news_user_agent == "test-agent/1.0"


def test_settings_optional_keys_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "ANTHROPIC_API_KEY",
        "KITE_API_KEY",
        "KITE_API_SECRET",
        "KITE_ACCESS_TOKEN",
        "LOG_LEVEL",
        "NEWS_USER_AGENT",
    ):
        monkeypatch.delenv(k, raising=False)
    s = get_settings(load_dotenv=False)
    assert s.anthropic_api_key is None
    assert s.kite_api_key is None
    assert s.kite_api_secret is None
    assert s.kite_access_token is None


def test_settings_log_level_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    s = get_settings(load_dotenv=False)
    assert s.log_level == "INFO"


def test_settings_news_user_agent_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWS_USER_AGENT", raising=False)
    s = get_settings(load_dotenv=False)
    assert s.news_user_agent.startswith("trading-bot/")


def test_settings_is_frozen() -> None:
    s = Settings(
        anthropic_api_key=None,
        kite_api_key=None,
        kite_api_secret=None,
        kite_access_token=None,
        slack_webhook_url=None,
        log_level="INFO",
        news_user_agent="x/1",
    )
    with pytest.raises((AttributeError, Exception)):
        s.log_level = "DEBUG"  # type: ignore[misc]


def test_paths_construct_directly(tmp_path: Path) -> None:
    """Paths can be constructed by hand if a caller wants full control."""
    p = Paths(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        parquet_dir=tmp_path / "data" / "parquet",
        cache_dir=tmp_path / "data" / "cache",
        logs_dir=tmp_path / "data" / "logs",
        research_dir=tmp_path / "data" / "research",
        raw_dir=tmp_path / "data" / "raw",
        models_dir=tmp_path / "models",
        db_path=tmp_path / "data" / "app.db",
    )
    assert p.project_root == tmp_path


def test_settings_reads_slack_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    s = get_settings(load_dotenv=False)
    assert s.slack_webhook_url == "https://hooks.slack.com/services/T/B/X"


def test_settings_slack_webhook_url_missing_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    s = get_settings(load_dotenv=False)
    assert s.slack_webhook_url is None
