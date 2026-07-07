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
from datetime import date
from pathlib import Path

from trading.data.snapshot_schema import validate_row


class MacroCrossStaleError(RuntimeError):
    """Raised when a cross-file's `captured_at` date ≠ the refresh date (F-063).

    Mirrors `KiteSnapshotStaleError`: a stale/off-date cross-file must never
    gap-fill or verify against today's snapshot, or a days-old VIX/USDINR could
    silently drive today's regime multiplier.
    """


@dataclass(frozen=True)
class MacroCrossSource:
    """One Kite MCP cross-source macro capture. `vix`/`usdinr` default to None
    so a file carrying only the fields Kite returned still validates."""

    source: str
    captured_at: str  # ISO timestamp the skill captured the quotes
    vix: float | None = None
    usdinr: float | None = None


def read_macro_cross(path: Path, *, as_of: date | None = None) -> MacroCrossSource:
    """Parse + validate a `macro_cross_HHMM.json` file → `MacroCrossSource`.

    Raises `SnapshotSchemaError` (with the path and offending field) on malformed
    JSON, a non-object payload, a missing required field, or a wrong-typed figure.

    When `as_of` is given, also raises `MacroCrossStaleError` if the file's
    `captured_at` date doesn't match it — the freshness half of the F-002
    boundary that the broker/quote snapshots already enforce (F-063).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    cross: MacroCrossSource = validate_row(MacroCrossSource, raw, source=str(path))
    if as_of is not None:
        captured_date = cross.captured_at[:10]
        if captured_date != as_of.isoformat():
            raise MacroCrossStaleError(
                f"Cross-source file at {path} was captured {captured_date}, "
                f"requested {as_of.isoformat()}. Re-run /macro-doctor to refresh "
                "it before gap-filling or verifying today's snapshot."
            )
    return cross
