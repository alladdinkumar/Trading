"""Phase 14.C — pre-open IEP gap filter orchestrator.

Runs at 08:55 (5 minutes before market open) to filter + reorder pre_open's
candidate list by overnight gaps and sector momentum alignment with regime.
Modifies _context.md in-place.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from trading.config import Paths, get_paths
from trading.data.kite import Quote
from trading.data.quotes_snapshot import (
    QuoteSnapshotMissingError,
    QuoteSnapshotStaleError,
    read_latest_quotes,
)
from trading.features.regime import Regime
from trading.ops.logging_setup import configure_logging
from trading.store.db import get_conn
from trading.store.macro_store import get_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.ohlcv import read_ohlcv


class PreOpenIepAborted(RuntimeError):  # noqa: N818
    """Raised when run_pre_open_iep cannot proceed (missing context, etc.)."""


@dataclass(frozen=True)
class PreOpenIepResult:
    as_of: date
    regime: Regime
    candidates_input: int
    candidates_filtered: int
    candidates_removed: int
    rerank_applied: bool
    context_path: Path | None
    removed_symbols: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def run_pre_open_iep(
    as_of: date,
    *,
    paths: Paths | None = None,
    sector_map: dict[str, str] | None = None,
    sector_momentum: dict[str, float] | None = None,
) -> PreOpenIepResult:
    """Filter + reorder pre_open's candidate list by gap + sector momentum.

    Reads:
      - data/research/<as_of>/_context.md (must exist; raises PreOpenIepAborted)
      - data/raw/<as_of>/quotes_HHMM.json (optional; warns if missing)
      - parquet OHLCV (D-1 closes per candidate; warns if missing)
      - regime from macro_snapshot (defaults to NEUTRAL if absent)

    sector_map / sector_momentum are optional injections (MVP has no
    runtime source; tests + future Phase 12.6 will supply them).

    Writes data/research/<as_of>/_context.md (in-place, reordered).
    """
    p = paths if paths is not None else get_paths()
    warnings: list[str] = []

    context_path = p.research_dir / as_of.isoformat() / "_context.md"
    if not context_path.is_file():
        raise PreOpenIepAborted(
            f"Context bundle missing at {context_path}. Run `trading pre-open` first."
        )
    context_md = context_path.read_text(encoding="utf-8")
    candidates = _parse_candidates_from_context(context_md)
    if not candidates:
        return PreOpenIepResult(
            as_of=as_of,
            regime="NEUTRAL",
            candidates_input=0,
            candidates_filtered=0,
            candidates_removed=0,
            rerank_applied=False,
            context_path=context_path,
            removed_symbols=[],
            warnings=["No candidates in _context.md — nothing to filter."],
        )

    with get_conn(p.db_path) as conn:
        run_migrations(conn)
        regime = _load_regime(conn, as_of, warnings)

    try:
        quotes, _capture_ts = read_latest_quotes(p, as_of)
    except (QuoteSnapshotMissingError, QuoteSnapshotStaleError) as e:
        warnings.append(f"Overnight quotes unavailable: {e!s}")
        quotes = {}

    yesterday_closes = _load_yesterday_closes(p, as_of, candidates, warnings)
    gaps = _compute_gaps(set(candidates), quotes, yesterday_closes)

    kept, removed = _filter_by_regime(candidates, gaps, regime)
    if sector_map and sector_momentum:
        kept, sector_removed = _filter_by_sector(kept, sector_map, sector_momentum, regime)
        removed = [*removed, *sector_removed]
        sector_percentiles = _sector_percentile(sector_momentum)
        candidate_sector_pcts = {
            sym: sector_percentiles.get(sector_map.get(sym, ""), 50.0) for sym in kept
        }
    else:
        candidate_sector_pcts = {}
        if sector_map is None and sector_momentum is None:
            warnings.append("Sector data unavailable; filtering by gap + regime only.")

    rerank_applied = bool(gaps)
    new_order = _rerank(kept, gaps, candidate_sector_pcts) if rerank_applied else kept

    updated_md = _update_context_markdown(context_md, new_order, removed)
    context_path.write_text(updated_md, encoding="utf-8")

    return PreOpenIepResult(
        as_of=as_of,
        regime=regime,
        candidates_input=len(candidates),
        candidates_filtered=len(new_order),
        candidates_removed=len(removed),
        rerank_applied=rerank_applied,
        context_path=context_path,
        removed_symbols=removed,
        warnings=warnings,
    )


def _load_regime(conn: sqlite3.Connection, as_of: date, warnings: list[str]) -> Regime:
    """Look up today's regime from macro_snapshot, default NEUTRAL if absent."""
    snap = get_macro_snapshot(conn, as_of.isoformat())
    if snap is None or snap.regime is None:
        warnings.append("Regime not yet classified; using NEUTRAL (no directional filter).")
        return "NEUTRAL"
    regime_str = snap.regime
    if regime_str not in ("RISK_ON", "RISK_OFF", "NEUTRAL"):
        warnings.append(f"Unknown regime '{regime_str}'; using NEUTRAL.")
        return "NEUTRAL"
    return regime_str  # type: ignore[return-value]


def _load_yesterday_closes(
    paths: Paths,
    as_of: date,
    symbols: list[str],
    warnings: list[str],
) -> dict[str, float]:
    """For each symbol, read the most recent parquet bar's close (D-1)."""
    closes: dict[str, float] = {}
    for sym in symbols:
        try:
            df = read_ohlcv(sym, paths, end=as_of)
        except FileNotFoundError:
            warnings.append(f"no parquet for {sym} — gap not computed")
            continue
        if df.empty:
            warnings.append(f"empty parquet for {sym} — gap not computed")
            continue
        closes[sym] = float(df["close"].iloc[-1])
    return closes


def _main(
    date_str: str,
    dry_run: bool = False,
) -> None:
    """`python -m trading.jobs.pre_open_iep <YYYY-MM-DD> [--dry-run]` entry."""
    configure_logging("pre_open_iep")
    from loguru import logger

    try:
        result = run_pre_open_iep(date.fromisoformat(date_str))
    except PreOpenIepAborted as e:
        print(f"Pre-open IEP aborted: {e}")
        raise SystemExit(2) from e
    except Exception:
        logger.exception("pre_open_iep failed")
        raise
    print(f"Regime: {result.regime}")
    print(f"Candidates input: {result.candidates_input}")
    print(f"Candidates filtered: {result.candidates_filtered}")
    print(f"Candidates removed: {result.candidates_removed}")
    if result.removed_symbols:
        print(f"Removed: {', '.join(result.removed_symbols)}")
    if result.context_path:
        print(f"Updated: {result.context_path}")
        print("Ready for /analyst skill")
    if result.warnings:
        for w in result.warnings:
            print(f"⚠ {w}")


def _compute_gaps(
    symbols: set[str],
    quotes: dict[str, Quote],
    yesterday_closes: dict[str, float],
) -> dict[str, float]:
    """Compute gap_pct for each symbol.

    gap_pct = ((ltp - yesterday_close) / yesterday_close) * 100

    Returns dict with entry for each symbol in quotes. Symbols not in
    quotes are silently skipped.
    """
    gaps: dict[str, float] = {}
    for sym in symbols:
        if sym not in quotes:
            continue
        if sym not in yesterday_closes:
            continue
        q = quotes[sym]
        yesterday = yesterday_closes[sym]
        if yesterday == 0:
            continue  # divide-by-zero guard
        gap = ((q.last_price - yesterday) / yesterday) * 100
        gaps[sym] = gap
    return gaps


def _filter_by_regime(
    candidates: list[str],
    gaps: dict[str, float],
    regime: Regime,
) -> tuple[list[str], list[str]]:
    """Filter candidates by regime-aligned gap direction.

    RISK_ON: keep gap >= 0%, remove gap < 0%
    RISK_OFF: keep gap <= 0%, remove gap > 0%
    NEUTRAL: keep all

    Returns (kept_symbols, removed_symbols).
    """
    kept: list[str] = []
    removed: list[str] = []

    for sym in candidates:
        if sym not in gaps:
            # No gap data available, skip to next filter
            kept.append(sym)
            continue

        gap = gaps[sym]

        if regime == "RISK_ON":
            if gap >= 0:
                kept.append(sym)
            else:
                removed.append(sym)
        elif regime == "RISK_OFF":
            if gap <= 0:
                kept.append(sym)
            else:
                removed.append(sym)
        elif regime == "NEUTRAL":
            kept.append(sym)
        else:
            kept.append(sym)  # unknown regime, keep candidate

    return kept, removed


def _filter_by_sector(
    candidates: list[str],
    sector_map: dict[str, str],
    sector_momentum: dict[str, float],
    regime: Regime,
) -> tuple[list[str], list[str]]:
    """Filter candidates by sector momentum alignment with regime.

    RISK_ON: keep if sector_momentum >= 0 (not lagging)
    RISK_OFF: keep if sector_momentum <= 0 (not rallying)
    NEUTRAL: keep all

    Returns (kept_symbols, removed_symbols).
    """
    kept: list[str] = []
    removed: list[str] = []

    for sym in candidates:
        if sym not in sector_map:
            # No sector mapping, default to keep
            kept.append(sym)
            continue

        sector = sector_map[sym]
        if sector not in sector_momentum:
            # Sector data missing, default to keep
            kept.append(sym)
            continue

        sector_mom = sector_momentum[sector]

        if regime == "RISK_ON":
            if sector_mom >= 0:
                kept.append(sym)
            else:
                removed.append(sym)
        elif regime == "RISK_OFF":
            if sector_mom <= 0:
                kept.append(sym)
            else:
                removed.append(sym)
        elif regime == "NEUTRAL":
            kept.append(sym)
        else:
            kept.append(sym)

    return kept, removed


def _sector_percentile(sector_momentum: dict[str, float]) -> dict[str, float]:
    """Compute percentile rank [0-100] for each sector by momentum.

    Highest momentum = 100th percentile.
    Lowest momentum = 0th percentile.
    Uses formula: (n - i - 1) / (n - 1) * 100 for n > 1.
    """
    if not sector_momentum:
        return {}

    sorted_sectors = sorted(sector_momentum.items(), key=lambda x: x[1], reverse=True)
    n = len(sorted_sectors)

    percentiles: dict[str, float] = {}
    for i, (sector, _) in enumerate(sorted_sectors):
        percentile = 100.0 if n == 1 else ((n - i - 1) / (n - 1)) * 100
        percentiles[sector] = percentile

    return percentiles


def _compute_scores(
    gaps: dict[str, float],
    sector_percentiles: dict[str, float],
) -> dict[str, float]:
    """Compute composite rerank score for each candidate.

    score = (gap_pct_normalized × 0.6) + (sector_pct × 0.4)

    where gap_pct_normalized = gap_pct / max(|gap_pcts|)
    """
    if not gaps:
        return {}

    gap_values = list(gaps.values())
    gap_max = max(abs(g) for g in gap_values) if gap_values else 1.0
    if gap_max == 0:
        gap_max = 1.0

    scores: dict[str, float] = {}
    for sym, gap in gaps.items():
        gap_norm = gap / gap_max
        sector_pct = sector_percentiles.get(sym, 50) / 100.0
        score = (gap_norm * 0.6) + (sector_pct * 0.4)
        scores[sym] = score

    return scores


def _rerank(
    candidates: list[str],
    gaps: dict[str, float],
    sector_percentiles: dict[str, float],
) -> list[str]:
    """Rerank candidates by score, descending order.

    Candidates not in gaps or sector_percentiles get score 0.
    Stable sort preserves original order for ties.
    """
    scores = _compute_scores(gaps, sector_percentiles)

    def get_score(sym: str) -> float:
        return scores.get(sym, 0.0)

    # Stable sort by score, descending
    sorted_candidates = sorted(candidates, key=get_score, reverse=True)
    return sorted_candidates


def _parse_candidates_from_context(context_md: str) -> list[str]:
    """Extract candidate symbols from _context.md.

    Looks for lines matching: '### SYMBOL — passes X/Y rules'
    Returns list of symbols in order of appearance.
    """
    pattern = r"^### ([A-Z0-9_]+) — passes \d+/\d+ rules"
    matches = re.findall(pattern, context_md, re.MULTILINE)
    return matches


def _update_context_markdown(
    context_md: str,
    new_order: list[str],
    removed_symbols: list[str],
) -> str:
    """Update context markdown with reordered candidates.

    Reorders the '### SYMBOL' lines in the candidates section.
    Appends list of removed candidates with explanations at the end.

    Returns updated markdown string.
    """
    # Parse context into sections
    lines = context_md.split("\n")

    # Find the "## Today's candidates" section
    candidates_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## Today's candidates":
            candidates_idx = i
            break

    if candidates_idx is None:
        # No candidates section, return unchanged
        return context_md

    # Extract candidate blocks (each starts with "###")
    candidate_blocks: dict[str, list[str]] = {}
    current_block: list[str] = []
    current_symbol: str | None = None

    for i in range(candidates_idx + 1, len(lines)):
        line = lines[i]

        # Stop at next section (starts with "##")
        if line.startswith("##") and not line.startswith("###"):
            break

        # New candidate block
        if line.startswith("###"):
            if current_symbol and current_block:
                candidate_blocks[current_symbol] = current_block
            match = re.match(r"^### ([A-Z0-9_]+) —", line)
            if match:
                current_symbol = match.group(1)
                current_block = [line]
            else:
                current_block = [line]
        else:
            if current_block is not None:
                current_block.append(line)

    # Save last block
    if current_symbol and current_block:
        candidate_blocks[current_symbol] = current_block

    # Rebuild candidates section in new order
    new_lines = lines[: candidates_idx + 1]

    for sym in new_order:
        if sym in candidate_blocks:
            new_lines.extend(candidate_blocks[sym])

    # Append removed candidates list
    if removed_symbols:
        new_lines.append("")
        for sym in removed_symbols:
            new_lines.append(f"_(REMOVED: {sym})_")

    # Append rest of context after candidates section
    for i in range(candidates_idx + 1, len(lines)):
        if lines[i].startswith("##") and not lines[i].startswith("###"):
            new_lines.extend(lines[i:])
            break

    return "\n".join(new_lines)


if __name__ == "__main__":  # pragma: no cover
    import typer

    typer.run(_main)
