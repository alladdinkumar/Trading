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
    """Raised by `compile_brief` when one or more required parts are absent."""


SECTOR_COMMENTARY_PLACEHOLDER = (
    "_(analyst did not write a sector commentary for this run)_"
)


def required_parts(mode: Mode, candidate_symbols: list[str]) -> list[str]:
    """Parts that MUST exist or `compile_brief` raises `MissingNarrativeError`."""
    parts = ["macro_brief.md"]
    parts.extend(f"candidates/{sym}.md" for sym in candidate_symbols)
    if mode == "post_close":
        parts.append("post_close_recap.md")
    return parts


def optional_parts(mode: Mode) -> list[str]:
    """Parts `compile_brief` tolerates being absent (substitutes a placeholder)."""
    return ["sector_commentary.md"]


_CANDIDATE_HEADING = re.compile(r"^### ([A-Z0-9_&-]+) — passes \d+/\d+ rules", re.MULTILINE)


def _parse_candidate_symbols(context_md: str) -> list[str]:
    return _CANDIDATE_HEADING.findall(context_md)


def _infer_mode(context_md: str) -> Mode:
    if "(mode: post_close)" in context_md:
        return "post_close"
    return "pre_open"


def compile_brief(date_dir: Path, *, mode: Mode | None = None) -> Path:
    """Read narrative parts in `date_dir`, write `brief.md`, return its path.

    Raises `MissingNarrativeError` listing any required parts that are
    absent. Optional parts (`sector_commentary.md`) get a hardcoded
    placeholder body when missing — the section header stays. Orphan
    candidate files (symbols not in the bundle) are skipped with a
    stderr warning. If `mode` is None, it is inferred from the bundle
    header.
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
    required = required_parts(mode, symbols)
    missing = [p for p in required if not (date_dir / p).is_file()]
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

    sector_path = date_dir / "sector_commentary.md"
    sector_body = (
        sector_path.read_text(encoding="utf-8").strip()
        if sector_path.is_file()
        else SECTOR_COMMENTARY_PLACEHOLDER
    )

    out_path = date_dir / "brief.md"
    parts_count = len(required) + (1 if sector_path.is_file() else 0)
    sections: list[str] = [
        f"# Daily brief — {date_dir.name}",
        f"_Compiled at {datetime.now().isoformat(timespec='seconds')} from "
        f"{parts_count} narrative parts._",
        "",
        "## Macro",
        (date_dir / "macro_brief.md").read_text(encoding="utf-8").strip(),
        "",
        "## Sector commentary",
        sector_body,
        "",
        "## Candidates",
    ]
    for sym in symbols:
        body = (date_dir / "candidates" / f"{sym}.md").read_text(encoding="utf-8")
        sections.append("")
        sections.append(body.strip())

    # Optional mid-day update (Phase 14.A): included when present, regardless of mode.
    mid_day_path = date_dir / "mid_day_update.md"
    if mid_day_path.is_file():
        sections.append("")
        sections.append(mid_day_path.read_text(encoding="utf-8").strip())

    # Optional post-close summary (Phase 14.B): same pattern, regardless of mode.
    post_close_summary_path = date_dir / "post_close_summary.md"
    if post_close_summary_path.is_file():
        sections.append("")
        sections.append(
            post_close_summary_path.read_text(encoding="utf-8").strip()
        )

    if mode == "post_close":
        sections.append("")
        sections.append("## Post-close recap")
        sections.append(
            (date_dir / "post_close_recap.md").read_text(encoding="utf-8").strip()
        )
    out_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return out_path
