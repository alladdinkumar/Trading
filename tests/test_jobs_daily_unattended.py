"""Tests for trading.jobs.daily_unattended — the F-032 broker-free gap-filler.

The unattended daily job keeps the macro/scan/auto-open/track-record spine
continuous on operator-absent days. It runs the broker-free subset of pre_open
(`require_snapshot=False`), but only when the operator hasn't already produced
the day's bundle (a `trading status`-style artifact check) and only on trading
days.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.jobs import daily_unattended as du
from trading.jobs.daily_unattended import run_daily_unattended
from trading.jobs.pre_open import PreOpenResult

AS_OF = date(2026, 6, 18)  # Thursday


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


@pytest.fixture(autouse=True)
def _trading_day(monkeypatch):
    monkeypatch.setattr(du, "is_trading_day", lambda d: True)


def _fake_pre_open(paths) -> PreOpenResult:
    return PreOpenResult(
        as_of=AS_OF,
        bundle_path=paths.research_dir / AS_OF.isoformat() / "_context.md",
        macro_written=True,
        sector_written=True,
        news_inserted=0,
        sentiment_rows=0,
        candidates_total=3,
        candidates_passing=2,
        candidates_selected=1,
        paper_trades_opened=0,
        pending_entries=1,
        holdings_scored=0,
        warnings=["no Kite snapshot — holdings health skipped"],
    )


def _spy_run_pre_open(monkeypatch, paths):
    """Replace run_pre_open with a spy that records its kwargs and returns a fake."""
    calls: list[dict] = []

    def spy(as_of, **kwargs):
        calls.append({"as_of": as_of, **kwargs})
        return _fake_pre_open(paths)

    monkeypatch.setattr(du, "run_pre_open", spy)
    return calls


def _write_context(paths, d: date) -> None:
    out = paths.research_dir / d.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "_context.md").write_text("ctx", encoding="utf-8")


def test_skips_on_non_trading_day(paths, monkeypatch):
    monkeypatch.setattr(du, "is_trading_day", lambda d: False)
    calls = _spy_run_pre_open(monkeypatch, paths)
    result = run_daily_unattended(AS_OF, paths=paths)
    assert result.ran is False
    assert result.pre_open is None
    assert "trading" in (result.skipped_reason or "").lower()
    assert calls == []  # run_pre_open never invoked


def test_skips_when_spine_already_ran(paths, monkeypatch):
    _write_context(paths, AS_OF)  # operator already produced the bundle
    calls = _spy_run_pre_open(monkeypatch, paths)
    result = run_daily_unattended(AS_OF, paths=paths)
    assert result.ran is False
    assert "already" in (result.skipped_reason or "").lower()
    assert calls == []


def test_runs_degraded_when_spine_absent(paths, monkeypatch):
    calls = _spy_run_pre_open(monkeypatch, paths)
    result = run_daily_unattended(AS_OF, paths=paths)
    assert result.ran is True
    assert result.skipped_reason is None
    assert result.pre_open is not None
    assert len(calls) == 1
    # The broker-free contract: it must call pre_open with require_snapshot=False.
    assert calls[0]["require_snapshot"] is False


def test_force_reruns_even_when_spine_present(paths, monkeypatch):
    _write_context(paths, AS_OF)
    calls = _spy_run_pre_open(monkeypatch, paths)
    result = run_daily_unattended(AS_OF, paths=paths, force=True)
    assert result.ran is True
    assert len(calls) == 1
