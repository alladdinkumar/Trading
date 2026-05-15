"""Tests for trading.llm.context — input bundle assembly."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.llm.context import ContextInputs, assemble_context
from trading.portfolio.health import HealthScore
from trading.store.migrations import run_migrations
from trading.strategy.rules import Candidate, RuleResult


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def test_assemble_context_writes_file_with_header(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn,
        paths=paths,
        as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    assert "# Trading context bundle — 2026-05-15" in body
    assert "(mode: pre_open)" in body
    assert out == paths.research_dir / "2026-05-15" / "_context.md"


def _seed_macro(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO macro_snapshot
          (date, sgx_nifty, dow_fut, nasdaq_fut, sp500, usdinr, crude,
           vix, us_10y, fii_flow_cr, dii_flow_cr, regime)
        VALUES (?, NULL, NULL, NULL, NULL, ?, NULL, ?, NULL, ?, NULL, ?)
        """,
        ("2026-05-15", 95.76, 19.4, 187.0, "NEUTRAL"),
    )
    conn.commit()


def test_assemble_context_includes_macro_section(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_macro(conn)
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "VIX" in body and "19.4" in body
    assert "USDINR" in body and "95.76" in body
    assert "FII flow" in body and "187" in body
    assert "NEUTRAL" in body


def test_assemble_context_macro_no_data_when_missing(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Macro snapshot" in body
    assert "_(no data)_" in body


def _candidate(symbol: str = "RVNL", n_passed: int = 9) -> Candidate:
    rules = tuple(
        RuleResult(name=f"r{i}", passed=(i < n_passed), reason="")
        for i in range(10)
    )
    return Candidate(
        symbol=symbol,
        scan_date=date(2026, 5, 15),
        close=312.5,
        rsi_14=58.0,
        sma_20=305.0,
        sma_50=300.0,
        sma_200=275.0,
        atr_14=8.4,
        rules=rules,
    )


def _seed_news(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute(
        "INSERT INTO news_items (ts, symbol, source, headline, url, "
        "sentiment, category, is_critical) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-05-13T10:00:00",
            symbol,
            "moneycontrol",
            "RVNL bags rs 500cr order from Indian Railways",
            "https://example.com/rvnl",
            0.55,
            "results",
            0,
        ),
    )
    conn.execute(
        "INSERT INTO sentiment_daily (date, symbol, score_7d, score_30d, "
        "news_count, negative_news_count, has_critical) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-15", symbol, 0.32, 0.18, 4, 0, 0),
    )
    conn.commit()


def test_assemble_context_includes_candidates_section(
    conn: sqlite3.Connection, paths
) -> None:
    _seed_news(conn, "RVNL")
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[_candidate("RVNL", n_passed=9)],
                             holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Today's candidates" in body
    assert "### RVNL" in body
    assert "9/10" in body
    assert "RSI" in body and "58" in body
    assert "ATR" in body and "8.4" in body
    assert "RVNL bags" in body
    assert "Critical news flag: NO" in body


def test_assemble_context_candidates_no_data_when_empty(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Today's candidates" in body
    assert body.count("_(no data)_") >= 1  # at least the candidates section


def test_assemble_context_includes_holdings_health(
    conn: sqlite3.Connection, paths
) -> None:
    health = HealthScore(
        symbol="TATAPOWER",
        verdict="TRIM",
        score=22,
        net_votes=-2,
        votes_cast=8,
        reasons=["below 200-DMA", "RSI 38", "dist to 52w high 28%"],
        pnl_pct=-3.2,
    )
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[health]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Holdings health" in body
    assert "### TATAPOWER" in body
    assert "TRIM" in body
    assert "22/100" in body
    assert "below 200-DMA" in body


def test_assemble_context_holdings_health_no_data_when_empty(
    conn: sqlite3.Connection, paths
) -> None:
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[], holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "## Holdings health" in body
    assert "_(no data)_" in body
