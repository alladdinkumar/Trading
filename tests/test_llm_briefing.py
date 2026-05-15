"""Tests for trading.llm.briefing — narrative-part assembly into brief.md."""

from __future__ import annotations

from pathlib import Path

import pytest
from freezegun import freeze_time

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
    _write_part(date_dir, "macro_brief.md", "Regime is NEUTRAL. VIX 19.4.\n")
    _write_part(date_dir, "sector_commentary.md", "PSU/Infra LEADING.\n")
    _write_part(date_dir, "candidates/RVNL.md",
        "# RVNL — Conviction: HIGH\n\n## Bullish case\nx\n\n"
        "## Bearish case / risks\nx\n\n"
        "## Event risks in 25-day horizon\n- 2026-05-22: results — x\n")
    _write_part(date_dir, "candidates/NTPC.md",
        "# NTPC — Conviction: MEDIUM\n\n## Bullish case\ny\n\n"
        "## Bearish case / risks\ny\n\n"
        "## Event risks in 25-day horizon\n- (none)\n")

    out = compile_brief(date_dir, mode="pre_open")
    assert out == date_dir / "brief.md"
    assert out.read_text(encoding="utf-8") == snapshot


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
