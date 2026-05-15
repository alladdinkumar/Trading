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
