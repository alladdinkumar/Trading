"""Shared pytest fixtures for the trading project."""

import json as _kite_json
from datetime import datetime as _kite_dt
from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the tests/fixtures/ directory."""
    return Path(__file__).parent / "fixtures"


def seed_kite_snapshot(
    paths,
    as_of,
    *,
    holdings=None,
    gtts=None,
    positions=None,
    snapshot_at=None,
    source="mcp",
):
    """Test helper: write data/raw/<as_of>/{resource}.json + _meta.json.

    Each `*_lists` parameter is a list of dicts (or None to skip the
    file). Used by kite_snapshot tests + pre_open / portfolio CLI tests.
    """
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    if holdings is not None:
        (base / "holdings.json").write_text(
            _kite_json.dumps(holdings), encoding="utf-8"
        )
    if gtts is not None:
        (base / "gtts.json").write_text(
            _kite_json.dumps(gtts), encoding="utf-8"
        )
    if positions is not None:
        (base / "positions.json").write_text(
            _kite_json.dumps(positions), encoding="utf-8"
        )
    meta_ts = (
        snapshot_at.isoformat()
        if snapshot_at is not None
        else _kite_dt.combine(as_of, _kite_dt.min.time()).isoformat()
    )
    (base / "_meta.json").write_text(
        _kite_json.dumps({
            "snapshot_at": meta_ts, "source": source, "skill_version": "1",
        }),
        encoding="utf-8",
    )
    return base
