"""Phase 12 narrative-part assembler.

Reads analyst-produced narrative files from `data/research/YYYY-MM-DD/`
and concatenates them into a single `brief.md`. Symbol list is parsed from
the bundle's "## Today's candidates" section so orphan candidate files
(symbols not in the bundle) are skipped with a warning.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from trading.llm.context import CANDIDATE_HEADING_RE, Mode


class MissingNarrativeError(RuntimeError):
    """Raised by `compile_brief` when one or more required parts are absent."""


class StaleBundleError(RuntimeError):
    """Raised by `compile_brief` when the bundle is older than `max_age`.

    Moves the analyst skill's advisory ">12h-old bundle" rule into code so a
    narrative is never compiled off a stale snapshot by accident (F-026).
    """


# F-026: the analyst skill's advisory refuse-stale threshold, now enforced.
DEFAULT_MAX_BUNDLE_AGE = timedelta(hours=12)

_ASSEMBLED_AT = re.compile(r"_Assembled at (\S+?)\._")


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


def _parse_candidate_symbols(context_md: str) -> list[str]:
    # F-027: shared heading regex lives in trading.llm.context (single source).
    return CANDIDATE_HEADING_RE.findall(context_md)


def _infer_mode(context_md: str) -> Mode:
    if "(mode: post_close)" in context_md:
        return "post_close"
    return "pre_open"


def _parse_assembled_at(context_md: str) -> datetime | None:
    """Parse the bundle's `_Assembled at <iso>._` stamp; None if absent/bad."""
    m = _ASSEMBLED_AT.search(context_md)
    if m is None:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def _parse_macro_table(context_md: str) -> dict[str, str]:
    """Extract the `## Macro snapshot` field→value rows from the bundle.

    The bundle (not the DB) is the source of truth at compile time, so this
    keeps `compile_brief` a pure file operation.
    """
    out: dict[str, str] = {}
    in_section = False
    for line in context_md.splitlines():
        if line.strip() == "## Macro snapshot":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if (
                    len(cells) == 2
                    and cells[0] not in ("field", "---")
                    and cells[1] not in ("value", "---")
                ):
                    out[cells[0]] = cells[1]
    return out


# Field → keywords that, if present in macro_brief.md, mean the analyst cited
# that figure and we should cross-check its value. Limited to the unambiguous,
# sign-stable numeric fields (VIX, USDINR); flows carry ₹/comma/sign formatting
# that makes numeric matching unreliable, so they are left to the human.
_MACRO_FIGURE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("VIX", ("vix",)),
    ("USDINR", ("usdinr", "usd/inr", "usd-inr")),
]


def _macro_figure_warnings(
    macro_table: dict[str, str], brief_text: str
) -> list[str]:
    """Warn when macro_brief cites a field but its bundle value is absent.

    Rounding-tolerant (matches to 1 decimal or nearest integer) so a correctly
    rounded figure never warns; a clearly different number does. Warn-only —
    never blocks the compile (F-026)."""
    brief_lower = brief_text.lower()
    nums = [
        float(t)
        for t in re.findall(r"-?\d+(?:\.\d+)?", brief_text.replace(",", ""))
    ]
    warnings: list[str] = []
    for field, keywords in _MACRO_FIGURE_KEYWORDS:
        val_str = macro_table.get(field)
        if val_str is None or not any(k in brief_lower for k in keywords):
            continue
        # A reconciliation-flagged cell is annotated ("19.40 ⚠ kite 22.5" /
        # "83.12 (unreconciled)") — parse only the leading numeric token so the
        # hallucination check still runs on the bare value (F-064). Skipping it
        # here would disable the guard exactly when the figure is already flagged.
        leading = val_str.split()[0] if val_str.split() else ""
        try:
            v = float(leading.replace(",", ""))
        except ValueError:
            continue
        if not any(round(n, 1) == round(v, 1) or round(n) == round(v) for n in nums):
            warnings.append(
                f"warning: macro_brief cites {field} but bundle value {val_str} "
                f"not found — verify (possible hallucinated figure)"
            )
    return warnings


def compile_brief(
    date_dir: Path,
    *,
    mode: Mode | None = None,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_BUNDLE_AGE,
    allow_stale: bool = False,
) -> Path:
    """Read narrative parts in `date_dir`, write `brief.md`, return its path.

    Raises `MissingNarrativeError` listing any required parts that are
    absent. Optional parts (`sector_commentary.md`) get a hardcoded
    placeholder body when missing — the section header stays. Orphan
    candidate files (symbols not in the bundle) are skipped with a
    stderr warning. If `mode` is None, it is inferred from the bundle
    header.

    F-026 guardrails: refuses (`StaleBundleError`) when the bundle's
    `_Assembled at_` stamp is older than `max_age` relative to `now`
    (unless `allow_stale`); and warns to stderr when a macro figure cited
    in `macro_brief.md` disagrees with the bundle's macro snapshot.
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

    if not allow_stale:
        assembled = _parse_assembled_at(context_md)
        if assembled is not None:
            age = (now or datetime.now()) - assembled
            if age > max_age:
                raise StaleBundleError(
                    f"Bundle at {context_path} was assembled "
                    f"{assembled.isoformat()} ({age} ago, limit {max_age}). "
                    f"Re-run `trading brief assemble-context --date "
                    f"{date_dir.name}` to refresh it, or pass allow_stale=True."
                )

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

    macro_brief_text = (date_dir / "macro_brief.md").read_text(encoding="utf-8")
    for w in _macro_figure_warnings(_parse_macro_table(context_md), macro_brief_text):
        print(w, file=sys.stderr)

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
        macro_brief_text.strip(),
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
