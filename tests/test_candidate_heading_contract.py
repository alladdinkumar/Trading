"""F-027 spanning contract test for the `### SYM — passes N/M rules` heading.

The heading line is touched by THREE modules that must agree on its exact
format:

  1. ``trading.llm.context.render_candidate_heading`` — WRITES it,
  2. ``trading.jobs.pre_open_iep`` — REWRITES / reorders around it,
  3. ``trading.llm.briefing`` — PARSES symbols back out.

Before F-027 each duplicated the literal / regex, so a format drift in one
silently broke the others with no test spanning all three. This test renders
via the shared helper and asserts both downstream parsers recover the symbol —
so a drift between the renderer format and either parser fails here.
"""

from __future__ import annotations

from trading.jobs.pre_open_iep import (
    _parse_candidates_from_context,
    _update_context_markdown,
)
from trading.llm.briefing import _parse_candidate_symbols
from trading.llm.context import (
    CANDIDATE_HEADING_RE,
    render_candidate_heading,
)

# Tricky symbols that exercise the F-033 hyphen/ampersand symbol class, plus a
# plain one as a control.
_SYMBOLS = [("M&M", 9, 10), ("BAJAJ-AUTO", 7, 10), ("RVNL", 10, 10)]


def _build_context(symbols: list[tuple[str, int, int]]) -> str:
    lines = ["# Trading context bundle (mode: pre_open)", "", "## Today's candidates"]
    for sym, n, m in symbols:
        lines.append("")
        lines.append(render_candidate_heading(sym, n, m))
        lines.append(f"- close 100.00 for {sym}")
    lines.append("")
    lines.append("## Macro")
    lines.append("- vix 12")
    return "\n".join(lines)


def test_rendered_heading_is_byte_identical() -> None:
    """Guard the exact literal so a silent FMT change is caught even if a
    parser is "fixed" alongside it."""
    assert render_candidate_heading("M&M", 9, 10) == "### M&M — passes 9/10 rules"


def test_all_three_call_sites_share_one_regex() -> None:
    """briefing + pre_open_iep must consume the *same* compiled regex object,
    so they cannot drift independently."""
    from trading.jobs import pre_open_iep
    from trading.llm import briefing

    assert briefing.CANDIDATE_HEADING_RE is CANDIDATE_HEADING_RE
    assert pre_open_iep.CANDIDATE_HEADING_RE is CANDIDATE_HEADING_RE


def test_briefing_parses_what_renderer_writes() -> None:
    """Renderer -> briefing parser round trip for every (incl. tricky) symbol."""
    context_md = _build_context(_SYMBOLS)
    parsed = _parse_candidate_symbols(context_md)
    assert parsed == ["M&M", "BAJAJ-AUTO", "RVNL"]


def test_pre_open_iep_parses_what_renderer_writes() -> None:
    """Renderer -> pre_open_iep parser round trip for every symbol."""
    context_md = _build_context(_SYMBOLS)
    parsed = _parse_candidates_from_context(context_md)
    assert parsed == ["M&M", "BAJAJ-AUTO", "RVNL"]


def test_pre_open_iep_rewrite_preserves_rendered_headings() -> None:
    """The rewrite path must recognise rendered headings, reorder by symbol,
    and keep each heading line byte-identical."""
    context_md = _build_context(_SYMBOLS)
    new_order = ["RVNL", "M&M", "BAJAJ-AUTO"]

    rewritten = _update_context_markdown(context_md, new_order, removed_symbols=[])

    # Reorder applied and recoverable by the parser.
    assert _parse_candidates_from_context(rewritten) == new_order
    # Each heading line survives unchanged.
    for sym, n, m in _SYMBOLS:
        assert render_candidate_heading(sym, n, m) in rewritten


def test_regex_captures_symbol_and_counts() -> None:
    """The shared regex extracts the symbol, and the heading carries N/M."""
    line = render_candidate_heading("BAJAJ-AUTO", 7, 10)
    m = CANDIDATE_HEADING_RE.match(line)
    assert m is not None
    assert m.group(1) == "BAJAJ-AUTO"
    assert "7/10" in line
