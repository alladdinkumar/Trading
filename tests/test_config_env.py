"""Tests for trading.config.update_env_var — idempotent .env line writer."""

from __future__ import annotations

from pathlib import Path

from trading.config import update_env_var


def test_creates_file_if_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert not env.exists()
    update_env_var(env, "FOO", "bar")
    assert env.read_text(encoding="utf-8").strip() == "FOO=bar"


def test_appends_when_key_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n", encoding="utf-8")
    update_env_var(env, "FOO", "bar")
    body = env.read_text(encoding="utf-8")
    assert "EXISTING=1" in body
    assert "FOO=bar" in body


def test_updates_existing_key_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=old\nBAR=keep\n", encoding="utf-8")
    update_env_var(env, "FOO", "new")
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "FOO=new" in lines
    assert "FOO=old" not in lines
    assert "BAR=keep" in lines  # untouched


def test_preserves_comments_and_blank_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# header\n\nFOO=old\n# trailing comment\n", encoding="utf-8")
    update_env_var(env, "FOO", "new")
    body = env.read_text(encoding="utf-8")
    assert "# header" in body
    assert "# trailing comment" in body
    assert "FOO=new" in body


def test_handles_no_trailing_newline(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=old", encoding="utf-8")  # no \n
    update_env_var(env, "FOO", "new")
    assert "FOO=new" in env.read_text(encoding="utf-8")


def test_handles_values_with_special_chars(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    update_env_var(env, "TOKEN", "abc=xyz/123+/")
    assert env.read_text(encoding="utf-8").strip() == "TOKEN=abc=xyz/123+/"
