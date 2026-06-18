"""Read-boundary for the Kite MCP cross-source macro file (F-035).

The `/macro-doctor` skill pulls a structured second source (India VIX, a USDINR
futures proxy) from the read-only Kite MCP session and writes it to
`data/raw/<date>/macro_cross_HHMM.json`:

    {"source": "kite_mcp", "captured_at": "<iso>", "vix": 19.55, "usdinr": 83.20}

`trading macro refresh`/`verify` consume it through `read_macro_cross`, which
validates the payload against the `MacroCrossSource` dataclass using the same
F-002 boundary as the broker/quote snapshots. A malformed write raises
`SnapshotSchemaError` with the offending file and field rather than silently
feeding a bad figure into the snapshot. `vix`/`usdinr` are optional — the skill
writes only the fields Kite returned, and an absent figure becomes `None` (left
for the reconciler to flag `missing_secondary`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from trading.data.snapshot_schema import validate_row


@dataclass(frozen=True)
class MacroCrossSource:
    """One Kite MCP cross-source macro capture. `vix`/`usdinr` default to None
    so a file carrying only the fields Kite returned still validates."""

    source: str
    captured_at: str  # ISO timestamp the skill captured the quotes
    vix: float | None = None
    usdinr: float | None = None


def read_macro_cross(path: Path) -> MacroCrossSource:
    """Parse + validate a `macro_cross_HHMM.json` file → `MacroCrossSource`.

    Raises `SnapshotSchemaError` (with the path and offending field) on malformed
    JSON, a non-object payload, a missing required field, or a wrong-typed figure.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    cross: MacroCrossSource = validate_row(MacroCrossSource, raw, source=str(path))
    return cross
