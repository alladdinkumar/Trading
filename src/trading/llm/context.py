"""Phase 12 input-bundle renderer.

Reads DB-resident state (macro_snapshot, sentiment_daily, news_items,
paper_trades, predictions) directly via `conn`. Ephemeral upstream outputs
(scanner candidates, portfolio health) arrive via the typed `ContextInputs`
dataclass — Phase 13's pre_open job is responsible for computing them once
and passing them in. Pure renderer: no scanner / portfolio invocation here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from trading.config import Paths
from trading.portfolio.health import HealthScore
from trading.strategy.rules import Candidate

Mode = Literal["pre_open", "post_close"]


@dataclass(frozen=True)
class ContextInputs:
    """Caller-supplied ephemeral inputs that don't live in any DB table."""

    candidates: list[Candidate] = field(default_factory=list)
    holdings_health: list[HealthScore] = field(default_factory=list)


def assemble_context(
    *,
    conn: sqlite3.Connection,
    paths: Paths,
    as_of: date,
    mode: Mode,
    inputs: ContextInputs,
) -> Path:
    """Render `_context.md` for `as_of` and return the written path.

    Sections rendered (in order): header, macro, candidates, holdings health,
    open paper-trades, (post_close only) matured predictions. Empty sections
    render as `_(no data)_` so the skill can flag the gap explicitly.
    """
    date_dir = paths.research_dir / as_of.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    out_path = date_dir / "_context.md"

    parts: list[str] = []
    parts.append(_render_header(as_of, mode))
    parts.append(_render_macro(conn, as_of))

    out_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return out_path


def _render_header(as_of: date, mode: Mode) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    return (
        f"# Trading context bundle — {as_of.isoformat()}  (mode: {mode})\n"
        f"\n"
        f"_Assembled at {ts}._"
    )


def _render_macro(conn: sqlite3.Connection, as_of: date) -> str:
    row = conn.execute(
        "SELECT vix, usdinr, fii_flow_cr, dii_flow_cr, regime "
        "FROM macro_snapshot WHERE date = ?",
        (as_of.isoformat(),),
    ).fetchone()
    if row is None:
        return "## Macro snapshot\n\n_(no data)_"
    lines = ["## Macro snapshot", "", "| field | value |", "|---|---|"]
    if row["vix"] is not None:
        lines.append(f"| VIX | {row['vix']:.2f} |")
    if row["usdinr"] is not None:
        lines.append(f"| USDINR | {row['usdinr']:.2f} |")
    if row["fii_flow_cr"] is not None:
        lines.append(f"| FII flow (₹ cr) | {row['fii_flow_cr']:+.0f} |")
    if row["dii_flow_cr"] is not None:
        lines.append(f"| DII flow (₹ cr) | {row['dii_flow_cr']:+.0f} |")
    if row["regime"] is not None:
        lines.append(f"| Regime | {row['regime']} |")
    return "\n".join(lines)
