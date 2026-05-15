"""Phase 12 narrative-part assembler.

Reads analyst-produced narrative files from `data/research/YYYY-MM-DD/`
and concatenates them into a single `brief.md`. Symbol list is parsed from
the bundle's "## Today's candidates" section so orphan candidate files
(symbols not in the bundle) are skipped with a warning.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
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

    out_path = date_dir / "brief.md"
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
