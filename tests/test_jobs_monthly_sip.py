"""Tests for trading.jobs.monthly_sip."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from tests.conftest import seed_kite_snapshot
from trading.config import get_paths
from trading.store.migrations import run_migrations
from trading.store.ohlcv import write_ohlcv

AS_OF = date(2026, 7, 1)  # a Wednesday


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


@pytest.fixture
def weekday_calendar(monkeypatch: pytest.MonkeyPatch):
    """Deterministic, offline trading-day calendar (no holiday fetch)."""
    monkeypatch.setattr(
        "trading.jobs.monthly_sip.is_trading_day", lambda d: d.weekday() < 5
    )


@pytest.fixture
def notify_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "trading.jobs.monthly_sip.notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )
    return calls


def _frame(n: int) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    idx.name = "date"
    closes = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def _holding_row(
    symbol: str, qty: int = 10, last_price: float = 100.0, avg: float = 90.0
) -> dict:
    return {
        "tradingsymbol": symbol,
        "exchange": "NSE",
        "isin": None,
        "quantity": qty,
        "average_price": avg,
        "last_price": last_price,
        "close_price": last_price,
        "pnl": (last_price - avg) * qty,
        "day_change": 0.0,
        "day_change_percentage": 0.0,
    }


def _memdb() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


def _insert_signal(
    conn: sqlite3.Connection, ts: str, symbol: str, ml_score: float | None
) -> None:
    conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, "
        "ml_score, created_by) VALUES (?, ?, 'LONG', 100.0, 95.0, 120.0, 25, ?, 'test')",
        (ts, symbol, ml_score),
    )


def test_trailing_trading_window(weekday_calendar) -> None:
    from trading.jobs.monthly_sip import trailing_trading_window

    oldest, newest = trailing_trading_window(AS_OF)
    assert newest == AS_OF
    assert oldest == date(2026, 6, 18)  # 10 weekdays back, inclusive of as_of


def test_gather_candidates_window_priority_and_health(paths, weekday_calendar) -> None:
    from trading.jobs.monthly_sip import gather_candidates

    write_ohlcv(_frame(250), "COALINDIA", paths)
    write_ohlcv(_frame(250), "CANDSYM", paths)
    conn = _memdb()
    _insert_signal(conn, "2026-06-25T08:30:00", "COALINDIA", 0.5)
    _insert_signal(conn, "2026-06-25T08:30:00", "CANDSYM", None)  # NULL → priority 0
    _insert_signal(conn, "2026-06-01T08:30:00", "OLDSYM", 0.9)  # outside window
    _insert_signal(conn, "2026-06-26T08:30:00", "NOPARQUET", 0.8)  # no parquet → dropped

    warnings: list[str] = []
    cands = gather_candidates(
        conn,
        paths,
        AS_OF,
        window=(date(2026, 6, 18), AS_OF),
        sector_map={"COALINDIA": "METAL"},
        held={"COALINDIA"},
        verdicts={"COALINDIA": "HOLD"},
        warnings=warnings,
    )
    by_sym = {c.symbol: c for c in cands}
    assert set(by_sym) == {"COALINDIA", "CANDSYM"}
    assert by_sym["COALINDIA"].health == "HOLD"
    assert by_sym["COALINDIA"].sector == "METAL"
    assert by_sym["COALINDIA"].priority == 0.5
    assert by_sym["CANDSYM"].health is None  # not held → NEW bucket
    assert by_sym["CANDSYM"].sector == "UNKNOWN"
    assert by_sym["CANDSYM"].priority == 0.0
    assert any("NOPARQUET" in w for w in warnings)


def test_score_holdings_skips_missing_parquet(paths) -> None:
    from trading.data.kite import Holding
    from trading.jobs.monthly_sip import _score_holdings

    write_ohlcv(_frame(250), "COALINDIA", paths)
    holdings = [
        Holding(**_holding_row("COALINDIA", qty=20, last_price=124.0, avg=100.0)),
        Holding(**_holding_row("GHOST")),
    ]
    warnings: list[str] = []
    verdicts = _score_holdings(paths, holdings, warnings)
    assert set(verdicts) == {"COALINDIA"}
    assert verdicts["COALINDIA"] in ("HOLD", "TRIM", "EXIT")
    assert any("GHOST" in w for w in warnings)


def _write_sector_map(paths, rows: dict[str, str]) -> None:
    p = paths.project_root / "data" / "static"
    p.mkdir(parents=True, exist_ok=True)
    body = "symbol,sector\n" + "\n".join(f"{s},{sec}" for s, sec in rows.items())
    (p / "sector_map.csv").write_text(body + "\n", encoding="utf-8")


def _seed_db(paths) -> None:
    from trading.store.db import get_conn

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        _insert_signal(conn, "2026-06-25T08:30:00", "CANDSYM", 0.7)
        _insert_signal(conn, "2026-06-01T08:30:00", "OLDSYM", 0.9)  # outside window
        conn.commit()


def test_aborts_without_kite_snapshot(paths, weekday_calendar) -> None:
    from trading.jobs.monthly_sip import MonthlySipAborted, run_monthly_sip

    with pytest.raises(MonthlySipAborted):
        run_monthly_sip(AS_OF, paths=paths)


def test_happy_path_writes_plan_and_notifies(
    paths, weekday_calendar, notify_calls, snapshot
) -> None:
    from trading.jobs.monthly_sip import run_monthly_sip

    seed_kite_snapshot(
        paths,
        AS_OF,
        holdings=[_holding_row("COALINDIA", qty=20, last_price=460.0, avg=400.0)],
        gtts=[],
    )
    write_ohlcv(_frame(250), "COALINDIA", paths)
    write_ohlcv(_frame(250), "CANDSYM", paths)
    _write_sector_map(paths, {"COALINDIA": "METAL", "CANDSYM": "AUTO"})
    _seed_db(paths)

    result = run_monthly_sip(AS_OF, paths=paths)
    assert result.holdings_count == 1
    assert result.candidates_considered == 1  # CANDSYM in window; OLDSYM outside
    assert result.deployed > 0
    assert result.plan_path is not None and result.plan_path.is_file()

    text = result.plan_path.read_text(encoding="utf-8")
    assert "# SIP plan — 2026-07-01" in text
    assert "CANDSYM" in text
    assert "## Post-plan sector weights" in text
    assert "⚠️" in text  # METAL holding dominates → >30% flag
    assert text == snapshot

    assert len(notify_calls) == 1
    level, title, body = notify_calls[0]
    assert level == "info"
    assert "2026-07-01" in title
    assert "Deployed" in body


def test_dry_run_writes_nothing(paths, weekday_calendar, notify_calls) -> None:
    from trading.jobs.monthly_sip import run_monthly_sip

    seed_kite_snapshot(paths, AS_OF, holdings=[_holding_row("COALINDIA")], gtts=[])
    write_ohlcv(_frame(250), "COALINDIA", paths)

    result = run_monthly_sip(AS_OF, paths=paths, dry_run=True)
    assert result.plan_path is None
    assert notify_calls == []
    assert not (paths.research_dir / "2026-07-01" / "sip_plan.md").exists()


def test_unmapped_symbols_get_unknown_sector(paths, weekday_calendar, notify_calls) -> None:
    """No sector_map.csv at all → everything lands in UNKNOWN, job still works."""
    from trading.jobs.monthly_sip import run_monthly_sip

    seed_kite_snapshot(paths, AS_OF, holdings=[_holding_row("COALINDIA")], gtts=[])
    write_ohlcv(_frame(250), "COALINDIA", paths)

    result = run_monthly_sip(AS_OF, paths=paths)
    assert result.plan_path is not None
    assert "UNKNOWN" in result.plan_path.read_text(encoding="utf-8")
