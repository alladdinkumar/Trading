"""Phase 14.A — readers for intraday quote snapshots written by /kite-quotes-snapshot.

Production code reads quotes through this module, never the SDK. The
filename's HHMM component is the single source of truth for capture
time (`_meta.quotes_at` is informational only) and is IST wall-clock,
same as the rest of the system (`trading.clock`). Staleness is measured
against `trading.clock.now_ist()` — a snapshot is "stale" if more
than `max_age_minutes` of IST wall-clock time has passed since capture
(F-058: never the host clock, which may not be IST).
Each row's `tradingsymbol` field is popped before splatting into
`Quote(**row)` since the dataclass uses `instrument_token` and we
key the dict by symbol externally.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from trading import clock
from trading.config import Paths
from trading.data.kite import Quote
from trading.data.snapshot_schema import SnapshotSchemaError, validate_row

_FILENAME_RE = re.compile(r"^quotes_([01]\d|2[0-3])([0-5]\d)\.json$")


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

    Returns (quotes_by_symbol, capture_ts) — `capture_ts` is tz-aware
    (`trading.clock.IST`), not naive (F-058). Raises:
      - QuoteSnapshotMissingError: no quotes_*.json present for the date
      - QuoteSnapshotStaleError: newest exists but capture > max_age_minutes ago

    Note: tradingsymbol is popped from each row before splatting into Quote, so the
    returned Quote objects use instrument_token for identity; symbol is the dict key only.
    """
    date_dir = paths.raw_dir / as_of.isoformat()
    if not date_dir.is_dir():
        raise QuoteSnapshotMissingError(
            f"No quotes for {as_of.isoformat()} (directory absent: {date_dir}). "
            "Run /kite-quotes-snapshot skill in Claude Code first."
        )
    candidates: list[tuple[str, str, Path]] = []
    for f in date_dir.iterdir():
        m = _FILENAME_RE.match(f.name)
        if m:
            candidates.append((m.group(1), m.group(2), f))
    if not candidates:
        raise QuoteSnapshotMissingError(
            f"No quotes_HHMM.json files in {date_dir}. "
            "Run /kite-quotes-snapshot skill in Claude Code first."
        )
    candidates.sort(key=lambda x: (x[0], x[1]))  # ascending HH then MM
    hh, mm, target = candidates[-1]
    capture_ts = datetime(
        as_of.year,
        as_of.month,
        as_of.day,
        int(hh),
        int(mm),
        tzinfo=clock.IST,
    )
    age = clock.now_ist() - capture_ts
    if age > timedelta(minutes=max_age_minutes):
        raise QuoteSnapshotStaleError(
            f"Newest quotes snapshot is stale: captured at {hh}:{mm} "
            f"({int(age.total_seconds() // 60)} min ago, max {max_age_minutes}). "
            "Re-run /kite-quotes-snapshot skill."
        )
    rows = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SnapshotSchemaError(
            f"{target}: expected a JSON array of quote rows, got {type(rows).__name__}. "
            "Re-run /kite-quotes-snapshot skill."
        )
    quotes: dict[str, Quote] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or "tradingsymbol" not in row:
            raise SnapshotSchemaError(
                f"{target}[{i}]: quote row is missing its 'tradingsymbol'. "
                "Re-run /kite-quotes-snapshot skill."
            )
        sym = row.pop("tradingsymbol")
        if not isinstance(sym, str):
            raise SnapshotSchemaError(
                f"{target}[{i}]: 'tradingsymbol' must be a string, got "
                f"{type(sym).__name__}. Re-run /kite-quotes-snapshot skill."
            )
        quotes[sym] = validate_row(Quote, row, source=str(target), index=i)
    return quotes, capture_ts
