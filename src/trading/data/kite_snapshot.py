"""Phase 13.5 — readers for Kite snapshots written by /kite-snapshot skill.

Production code reads Kite data through these readers, never the SDK.
Each reader opens `data/raw/<as_of>/<resource>.json` and parses rows
into the existing dataclasses from `trading.data.kite`. Missing files
raise `KiteSnapshotMissingError`; out-of-date snapshots raise
`KiteSnapshotStaleError`. The skill (or `kite-emergency-snapshot` CLI
fallback) writes both the resource JSONs and `_meta.json`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from trading.config import Paths
from trading.data.kite import GttOrder, Holding, Position


class KiteSnapshotMissingError(RuntimeError):
    """Raised when an expected snapshot JSON is absent for `as_of`."""


class KiteSnapshotStaleError(RuntimeError):
    """Raised when _meta.snapshot_at's date doesn't match `as_of`."""


def _validate_meta(date_dir: Path, as_of: date) -> None:
    meta_path = date_dir / "_meta.json"
    if not meta_path.is_file():
        raise KiteSnapshotMissingError(
            f"No Kite snapshot _meta.json at {meta_path}. "
            "Run /kite-snapshot skill in Claude Code first."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    snapshot_at = meta.get("snapshot_at", "")
    snap_date = snapshot_at[:10]
    if snap_date != as_of.isoformat():
        raise KiteSnapshotStaleError(
            f"Snapshot at {date_dir} is for {snap_date}, "
            f"requested {as_of.isoformat()}. "
            "Re-run /kite-snapshot skill to refresh."
        )


def _read_resource(
    paths: Paths,
    as_of: date,
    filename: str,
) -> list[dict]:
    date_dir = paths.raw_dir / as_of.isoformat()
    target = date_dir / filename
    if not target.is_file():
        raise KiteSnapshotMissingError(
            f"No Kite {filename} at {target}. "
            "Run /kite-snapshot skill in Claude Code first."
        )
    _validate_meta(date_dir, as_of)
    return json.loads(target.read_text(encoding="utf-8"))


def read_holdings(paths: Paths, as_of: date) -> list[Holding]:
    """Read `holdings.json` for `as_of` → list of Holding dataclasses."""
    rows = _read_resource(paths, as_of, "holdings.json")
    return [Holding(**row) for row in rows]


def read_gtts(paths: Paths, as_of: date) -> list[GttOrder]:
    """Read `gtts.json` for `as_of` → list of GttOrder dataclasses."""
    rows = _read_resource(paths, as_of, "gtts.json")
    return [GttOrder(**row) for row in rows]


def read_positions(paths: Paths, as_of: date) -> list[Position]:
    """Read `positions.json` for `as_of` → list of Position dataclasses."""
    rows = _read_resource(paths, as_of, "positions.json")
    return [Position(**row) for row in rows]
