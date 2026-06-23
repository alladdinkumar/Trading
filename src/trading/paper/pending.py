"""Handoff file between pre-open (writes) and open-fills (reads).

Pre-open selects the day's funding-eligible candidates but no longer opens
trades at the previous close; it records them here so the post-open
`open-fills` block can fill them at the live LTP. One JSON file per date:
`data/raw/<date>/_pending_entries.json`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from trading.config import Paths

_FILENAME = "_pending_entries.json"


class PendingEntriesMissingError(RuntimeError):
    """Raised when no _pending_entries.json exists for the requested date."""


@dataclass(frozen=True)
class PendingEntry:
    """A funding-eligible candidate from pre-open, awaiting a live-LTP fill."""

    symbol: str
    atr_14: float
    ml_score: float | None
    ref_close: float  # D-1 close, kept only for the open_fills drift report


def _path(paths: Paths, as_of: date) -> Path:
    return paths.raw_dir / as_of.isoformat() / _FILENAME


def write_pending_entries(
    paths: Paths,
    as_of: date,
    *,
    regime: str,
    entries: list[PendingEntry],
) -> Path:
    """Write the pending entries for `as_of`; returns the file path."""
    out = _path(paths, as_of)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": as_of.isoformat(),
        "regime": regime,
        "entries": [asdict(e) for e in entries],
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def read_pending_entries(paths: Paths, as_of: date) -> tuple[str, list[PendingEntry]]:
    """Read `(regime, entries)` for `as_of`. Raises PendingEntriesMissingError."""
    src = _path(paths, as_of)
    if not src.is_file():
        raise PendingEntriesMissingError(
            f"No pending entries for {as_of.isoformat()} ({src}). "
            "Run `trading pre-open` first."
        )
    payload = json.loads(src.read_text(encoding="utf-8"))
    entries = [
        PendingEntry(
            symbol=e["symbol"],
            atr_14=float(e["atr_14"]),
            ml_score=(None if e["ml_score"] is None else float(e["ml_score"])),
            ref_close=float(e["ref_close"]),
        )
        for e in payload["entries"]
    ]
    return str(payload["regime"]), entries
