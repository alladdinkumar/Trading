"""Tests for trading.paper.pending — the pre-open → open-fills handoff file."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.paper.pending import (
    PendingEntriesMissingError,
    PendingEntry,
    RiskParams,
    read_pending_entries,
    write_pending_entries,
)
from trading.strategy.daily_budget import (
    DEFAULT_DAILY_DEPLOY_CAP,
    DEFAULT_POOL_CAPITAL,
    DEFAULT_RISK_PCT,
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def test_write_then_read_roundtrips(paths) -> None:
    entries = [
        PendingEntry(symbol="TATASTEEL", atr_14=4.30, ml_score=0.61, ref_close=198.97),
        PendingEntry(symbol="COALINDIA", atr_14=11.2, ml_score=None, ref_close=449.0),
    ]
    out = write_pending_entries(paths, date(2026, 6, 23), regime="NEUTRAL", entries=entries)
    assert out.name == "_pending_entries.json"

    regime, got, risk_params = read_pending_entries(paths, date(2026, 6, 23))
    assert regime == "NEUTRAL"
    assert got == entries
    # No risk kwargs were passed to write_pending_entries → the daily_budget defaults.
    assert risk_params == RiskParams(
        pool_capital=DEFAULT_POOL_CAPITAL,
        daily_deploy_cap=DEFAULT_DAILY_DEPLOY_CAP,
        risk_pct=DEFAULT_RISK_PCT,
    )


def test_write_empty_entries_roundtrips(paths) -> None:
    write_pending_entries(paths, date(2026, 6, 23), regime="BULL", entries=[])
    regime, got, _risk_params = read_pending_entries(paths, date(2026, 6, 23))
    assert regime == "BULL"
    assert got == []


def test_read_missing_raises(paths) -> None:
    with pytest.raises(PendingEntriesMissingError):
        read_pending_entries(paths, date(2026, 6, 23))


def test_write_then_read_roundtrips_operator_risk_params(paths) -> None:
    """F-056: operator-supplied capital/cap/risk survive the handoff file
    round trip — not the daily_budget defaults."""
    entries = [PendingEntry(symbol="TATASTEEL", atr_14=4.30, ml_score=0.61, ref_close=198.97)]
    write_pending_entries(
        paths,
        date(2026, 6, 23),
        regime="NEUTRAL",
        entries=entries,
        pool_capital=20_000.0,
        daily_deploy_cap=2_000.0,
        risk_pct=0.01,
    )

    _, _, risk_params = read_pending_entries(paths, date(2026, 6, 23))
    assert risk_params == RiskParams(pool_capital=20_000.0, daily_deploy_cap=2_000.0, risk_pct=0.01)


def test_read_falls_back_to_defaults_when_risk_params_block_absent(paths) -> None:
    """F-056 fallback: a pending file written before this change has no
    `risk_params` key — reading it must not raise, and must yield the
    daily_budget defaults so an in-flight day isn't broken by the upgrade."""
    out = write_pending_entries(
        paths,
        date(2026, 6, 23),
        regime="NEUTRAL",
        entries=[PendingEntry(symbol="TATASTEEL", atr_14=4.30, ml_score=0.61, ref_close=198.97)],
    )
    # Simulate an older pending file by stripping the risk_params block.
    payload = json.loads(out.read_text(encoding="utf-8"))
    del payload["risk_params"]
    out.write_text(json.dumps(payload), encoding="utf-8")

    _, _, risk_params = read_pending_entries(paths, date(2026, 6, 23))
    assert risk_params == RiskParams(
        pool_capital=DEFAULT_POOL_CAPITAL,
        daily_deploy_cap=DEFAULT_DAILY_DEPLOY_CAP,
        risk_pct=DEFAULT_RISK_PCT,
    )
