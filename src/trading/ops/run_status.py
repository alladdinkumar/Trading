"""Half-run / missed-step detection for the daily flow (F-003).

The daily pipeline is ~13 manually-sequenced commands (see
`docs/daily-workflow.md`); nothing reconciled *which* steps actually ran on a
given date, so skipping IEP or running blocks out of order failed silently.

`compute_status` infers, purely from the durable artifacts each step leaves on
disk (or in the DB), which checkpoints have run — almost no job or skill needs
to stamp anything (the one exception: `pre_open_iep` stamps an in-place
"ran" marker in `_context.md`, since nothing writes an IEP-band
`_quote_symbols.txt`/quotes capture for it to be detected from otherwise —
F-062). It tracks **8 checkpoints across the 4 IST blocks** (the
meaningful per-block completion signals; the two prepare steps share one
overwritten `_quote_symbols.txt` and so aren't independently detectable, and a
block's "apply" output is what proves it finished).

It is *time-aware*: a not-yet-run step is `missing` only once its IST slot time
has passed (for today) or for any past date; before its time it is `pending`.
Non-trading days report every step as `n/a`. `has_due_failure` is the gate a
caller (CLI exit code, cron) keys on.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from trading.clock import IST, now_ist
from trading.config import Paths
from trading.ops.calendar import is_trading_day

# Quote snapshots are written as `quotes_HHMM.json`. The IEP block fires before
# market open, so an IEP-band capture is anything stamped before 10:30 IST;
# mid-day and post-close captures fall later in the day.
_IEP_QUOTE_CUTOFF_HHMM = 1030

# --- IEP-ran marker: single source of truth (F-062) ---------------------------
# `trading.jobs.pre_open_iep` rewrites `_context.md` in place and stamps this
# marker on every run — even on the (known, separately-tracked) day nothing
# writes `_quote_symbols.txt` for the IEP block, so no overnight quotes exist
# and the gap filter degrades to a benign no-op. Without it, this module had
# no way to tell "ran benignly with no quotes" from "never ran", and reported
# a false half-run every trading day. Lives here (not `trading.llm.context`,
# where the sibling `CANDIDATE_HEADING_RE` marker lives) because the L0-L6
# layering contract (F-009) forbids `ops` importing `llm`; `jobs` sits above
# both and imports this module for the writer side.
IEP_MARKER_FMT = "<!-- pre-open-iep: ran_at={ran_at} quotes_available={quotes_available} -->"
IEP_MARKER_RE = re.compile(
    r"<!-- pre-open-iep: ran_at=(?P<ran_at>\S+) quotes_available=(?P<quotes_available>true|false) -->"
)


def render_iep_marker(ran_at: datetime, quotes_available: bool) -> str:
    """Render the canonical `pre_open_iep`-ran marker line (F-062 single source)."""
    return IEP_MARKER_FMT.format(
        ran_at=ran_at.isoformat(),
        quotes_available="true" if quotes_available else "false",
    )


@dataclass(frozen=True)
class _Check:
    """A tracked checkpoint: stable key, block, label, and IST slot time."""

    key: str
    block: str
    label: str
    when: time


@dataclass(frozen=True)
class StepStatus:
    """The resolved status of one checkpoint for a given date."""

    key: str
    block: str
    label: str
    when: time
    ran: bool
    detail: str
    state: str  # "done" | "missing" | "pending" | "n/a"


@dataclass(frozen=True)
class _DayContext:
    paths: Paths
    as_of: date
    conn: sqlite3.Connection | None


def _raw_dir(ctx: _DayContext) -> Path:
    return ctx.paths.raw_dir / ctx.as_of.isoformat()


def _research_dir(ctx: _DayContext) -> Path:
    return ctx.paths.research_dir / ctx.as_of.isoformat()


def _iep_quote_file(ctx: _DayContext) -> Path | None:
    """The earliest quotes capture stamped in the IEP time band, if any."""
    raw = _raw_dir(ctx)
    if not raw.is_dir():
        return None
    for f in sorted(raw.glob("quotes_*.json")):
        hhmm = f.stem.split("_", 1)[-1]
        if hhmm.isdigit() and len(hhmm) == 4 and int(hhmm) < _IEP_QUOTE_CUTOFF_HHMM:
            return f
    return None


def _probe_snapshot(ctx: _DayContext) -> tuple[bool, str]:
    if not (_raw_dir(ctx) / "holdings.json").is_file():
        return False, ""
    meta = _raw_dir(ctx) / "_meta.json"
    if meta.is_file():
        try:
            at = json.loads(meta.read_text(encoding="utf-8")).get("snapshot_at", "")
        except (json.JSONDecodeError, OSError):
            at = ""
        return True, str(at)
    return True, ""


def _probe_scan(ctx: _DayContext) -> tuple[bool, str]:
    f = _research_dir(ctx) / "_context.md"
    return (True, f.name) if f.is_file() else (False, "")


def _probe_analyst(ctx: _DayContext) -> tuple[bool, str]:
    f = _research_dir(ctx) / "macro_brief.md"
    return (True, f.name) if f.is_file() else (False, "")


def _probe_compile(ctx: _DayContext) -> tuple[bool, str]:
    f = _research_dir(ctx) / "brief.md"
    return (True, f.name) if f.is_file() else (False, "")


def _iep_marker(ctx: _DayContext) -> tuple[str, bool] | None:
    """Parse `pre_open_iep`'s own "ran" marker out of `_context.md`, if present.

    F-062: nothing writes `_quote_symbols.txt` for the IEP block, so no
    overnight quotes snapshot is ever captured and `_iep_quote_file` returns
    `None` unconditionally — pre_open_iep still runs and degrades gracefully,
    but there was no durable signal distinguishing that from "never ran".
    pre_open_iep now stamps this marker on every run (see
    `trading.jobs.pre_open_iep._write_iep_marker`), so this is the primary
    "did IEP run today" signal; the quotes file above is used when present.
    """
    ctxmd = _research_dir(ctx) / "_context.md"
    if not ctxmd.is_file():
        return None
    m = IEP_MARKER_RE.search(ctxmd.read_text(encoding="utf-8"))
    if not m:
        return None
    return m.group("ran_at"), m.group("quotes_available") == "true"


def _probe_iep_quotes(ctx: _DayContext) -> tuple[bool, str]:
    f = _iep_quote_file(ctx)
    if f is not None:
        return True, f.name
    marker = _iep_marker(ctx)
    if marker is not None and not marker[1]:
        return True, "no overnight quotes captured (pre_open_iep ran without them)"
    return False, ""


def _probe_iep_filter(ctx: _DayContext) -> tuple[bool, str]:
    marker = _iep_marker(ctx)
    if marker is None:
        return False, ""
    _ran_at, quotes_available = marker
    detail = "re-filtered" if quotes_available else "re-filtered (no overnight quotes)"
    return True, detail


def _probe_mid_day(ctx: _DayContext) -> tuple[bool, str]:
    f = _research_dir(ctx) / "mid_day_update.md"
    return (True, f.name) if f.is_file() else (False, "")


def _probe_post_close(ctx: _DayContext) -> tuple[bool, str]:
    f = _research_dir(ctx) / "post_close_summary.md"
    if f.is_file():
        return True, f.name
    if ctx.conn is not None:
        row = ctx.conn.execute(
            "SELECT 1 FROM portfolio_snapshots WHERE date = ? LIMIT 1",
            (ctx.as_of.isoformat(),),
        ).fetchone()
        if row is not None:
            return True, "portfolio_snapshots row"
    return False, ""


# (checkpoint, probe) in chronological order — slot times mirror ops.runner.SCHEDULE.
_CHECKS: list[tuple[_Check, Callable[[_DayContext], tuple[bool, str]]]] = [
    (_Check("pre_open_kite", "pre_open", "/kite-snapshot", time(8, 30)), _probe_snapshot),
    (_Check("pre_open_scan", "pre_open", "trading pre-open", time(8, 35)), _probe_scan),
    (_Check("pre_open_analyst", "pre_open", "/analyst", time(8, 40)), _probe_analyst),
    (_Check("pre_open_compile", "pre_open", "trading brief compile", time(8, 45)), _probe_compile),
    (_Check("iep_quotes", "iep", "/kite-quotes-snapshot (IEP)", time(8, 55)), _probe_iep_quotes),
    (_Check("iep_filter", "iep", "trading pre-open-iep", time(9, 0)), _probe_iep_filter),
    (_Check("mid_day", "mid_day", "trading mid-day --apply", time(12, 35)), _probe_mid_day),
    (_Check("post_close", "post_close", "trading post-close --apply", time(16, 15)), _probe_post_close),
]


def _resolve_state(ran: bool, when: time, as_of: date, now: datetime) -> str:
    if ran:
        return "done"
    today = now.astimezone(IST).date()
    if as_of > today:
        return "pending"  # whole day still in the future
    if as_of < today:
        return "missing"  # day is over; it never ran
    due_at = datetime.combine(as_of, when, tzinfo=IST)
    return "missing" if now.astimezone(IST) >= due_at else "pending"


def compute_status(
    paths: Paths,
    as_of: date,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[StepStatus]:
    """Resolve every tracked checkpoint's status for `as_of`.

    `now` (default: current IST time) drives the due/pending split. `conn`, if
    given, lets the post-close probe fall back to a `portfolio_snapshots` row.
    """
    now = now or now_ist()
    trading = is_trading_day(as_of)
    ctx = _DayContext(paths=paths, as_of=as_of, conn=conn)
    out: list[StepStatus] = []
    for check, probe in _CHECKS:
        if not trading:
            out.append(
                StepStatus(
                    key=check.key,
                    block=check.block,
                    label=check.label,
                    when=check.when,
                    ran=False,
                    detail="non-trading day",
                    state="n/a",
                )
            )
            continue
        ran, detail = probe(ctx)
        out.append(
            StepStatus(
                key=check.key,
                block=check.block,
                label=check.label,
                when=check.when,
                ran=ran,
                detail=detail,
                state=_resolve_state(ran, check.when, as_of, now),
            )
        )
    return out


def has_due_failure(statuses: list[StepStatus]) -> bool:
    """True if any checkpoint is `missing` (run was due but its artifact absent)."""
    return any(s.state == "missing" for s in statuses)
