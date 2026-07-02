"""Handoff file between pre-open (writes) and open-fills (reads).

Pre-open selects the day's funding-eligible candidates but no longer opens
trades at the previous close; it records them here so the post-open
`open-fills` block can fill them at the live LTP. One JSON file per date:
`data/raw/<date>/_pending_entries.json`.

The file also carries the operator's risk controls (F-056) — pool capital,
daily deploy cap, risk-per-trade — supplied to `run_pre_open` via the CLI's
`--capital`/`--daily-cap`/`--risk-pct` flags, so `open_fills` apply mode can
pass the *same* values to `plan_daily_entries` instead of silently sizing
against the hardcoded `daily_budget` defaults. A `risk_params` block absent
from an older pending file (written before this change) falls back to those
same defaults, so an in-flight day is not broken by upgrading mid-day.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from trading.config import Paths
from trading.strategy.daily_budget import (
    DEFAULT_DAILY_DEPLOY_CAP,
    DEFAULT_POOL_CAPITAL,
    DEFAULT_RISK_PCT,
)

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


@dataclass(frozen=True)
class RiskParams:
    """The operator's sizing controls, threaded from `run_pre_open` to
    `open_fills.plan_daily_entries` (F-056)."""

    pool_capital: float = DEFAULT_POOL_CAPITAL
    daily_deploy_cap: float = DEFAULT_DAILY_DEPLOY_CAP
    risk_pct: float = DEFAULT_RISK_PCT


def _path(paths: Paths, as_of: date) -> Path:
    return paths.raw_dir / as_of.isoformat() / _FILENAME


def write_pending_entries(
    paths: Paths,
    as_of: date,
    *,
    regime: str,
    entries: list[PendingEntry],
    pool_capital: float = DEFAULT_POOL_CAPITAL,
    daily_deploy_cap: float = DEFAULT_DAILY_DEPLOY_CAP,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> Path:
    """Write the pending entries for `as_of`; returns the file path."""
    out = _path(paths, as_of)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": as_of.isoformat(),
        "regime": regime,
        "risk_params": {
            "pool_capital": pool_capital,
            "daily_deploy_cap": daily_deploy_cap,
            "risk_pct": risk_pct,
        },
        "entries": [asdict(e) for e in entries],
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def read_pending_entries(paths: Paths, as_of: date) -> tuple[str, list[PendingEntry], RiskParams]:
    """Read `(regime, entries, risk_params)` for `as_of`.

    `risk_params` falls back to `RiskParams()` defaults when the block is
    absent — the file predates F-056. Raises PendingEntriesMissingError.
    """
    src = _path(paths, as_of)
    if not src.is_file():
        raise PendingEntriesMissingError(
            f"No pending entries for {as_of.isoformat()} ({src}). Run `trading pre-open` first."
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
    rp = payload.get("risk_params")
    risk_params = (
        RiskParams(
            pool_capital=float(rp["pool_capital"]),
            daily_deploy_cap=float(rp["daily_deploy_cap"]),
            risk_pct=float(rp["risk_pct"]),
        )
        if rp is not None
        else RiskParams()
    )
    return str(payload["regime"]), entries, risk_params
