"""Phase 14.A — readers for intraday quote snapshots written by /kite-quotes-snapshot.

Production code reads quotes through this module, never the SDK. The
filename's HHMM component is the single source of truth for capture
time (`_meta.quotes_at` is informational only). Staleness is measured
against real-time `datetime.now()` — a snapshot is "stale" if more
than `max_age_minutes` of wall-clock time has passed since capture.
Each row's `tradingsymbol` field is popped before splatting into
`Quote(**row)` since the dataclass uses `instrument_token` and we
key the dict by symbol externally.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from trading.config import Paths
from trading.data.kite import Quote

_FILENAME_RE = re.compile(r"^quotes_(\d{4})\.json$")


class QuoteSnapshotMissingError(RuntimeError):
    """Raised when no quotes_*.json file exists for the requested date."""


class QuoteSnapshotStaleError(RuntimeError):
    """Raised when the newest quotes_*.json is older than max_age_minutes."""


def read_latest_quotes(
    paths: Paths,
    as_of: date,
    *,
    max_age_minutes: int = 30,
) -> tuple[dict[str, Quote], datetime]:
    """Find the most recent quotes_HHMM.json for `as_of`, parse → dict[symbol, Quote].

    Returns (quotes_by_symbol, capture_ts). Raises:
      - QuoteSnapshotMissingError: no quotes_*.json present for the date
      - QuoteSnapshotStaleError: newest exists but capture > max_age_minutes ago
    """
    date_dir = paths.raw_dir / as_of.isoformat()
    if not date_dir.is_dir():
        raise QuoteSnapshotMissingError(
            f"No quotes for {as_of.isoformat()} (directory absent: {date_dir}). "
            "Run /kite-quotes-snapshot skill in Claude Code first."
        )
    candidates: list[tuple[str, Path]] = []
    for f in date_dir.iterdir():
        m = _FILENAME_RE.match(f.name)
        if m:
            candidates.append((m.group(1), f))
    if not candidates:
        raise QuoteSnapshotMissingError(
            f"No quotes_HHMM.json files in {date_dir}. "
            "Run /kite-quotes-snapshot skill in Claude Code first."
        )
    candidates.sort(key=lambda x: x[0])  # ascending HHMM
    hhmm, target = candidates[-1]
    capture_ts = datetime(
        as_of.year, as_of.month, as_of.day,
        int(hhmm[:2]), int(hhmm[2:]),
    )
    age = datetime.now() - capture_ts
    if age > timedelta(minutes=max_age_minutes):
        raise QuoteSnapshotStaleError(
            f"Newest quotes snapshot is stale: captured at {hhmm[:2]}:{hhmm[2:]} "
            f"({int(age.total_seconds() // 60)} min ago, max {max_age_minutes}). "
            "Re-run /kite-quotes-snapshot skill."
        )
    rows: list[dict[str, Any]] = json.loads(target.read_text(encoding="utf-8"))
    quotes: dict[str, Quote] = {}
    for row in rows:
        sym = row.pop("tradingsymbol")
        quotes[sym] = Quote(**row)
    return quotes, capture_ts
