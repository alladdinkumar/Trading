"""Tests for trading.strategy.ranker_io — shared training-input loader."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.store.migrations import run_migrations
from trading.store.ohlcv import write_ohlcv


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


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


def test_load_training_inputs_filters_short_history(paths, conn) -> None:
    from trading.strategy.ranker_io import load_training_inputs

    write_ohlcv(_frame(250), "LONGSYM", paths)
    write_ohlcv(_frame(50), "SHORTSYM", paths)

    inputs = load_training_inputs(paths, conn)
    assert set(inputs.enriched) == {"LONGSYM"}
    # enrichment added indicator columns beyond the raw five
    assert inputs.enriched["LONGSYM"].shape[1] > 5


def test_load_training_inputs_builds_macro_and_sentiment(paths, conn) -> None:
    from trading.data.macro import MacroSnapshot
    from trading.store.macro_store import upsert_macro_snapshot
    from trading.strategy.ranker_io import load_training_inputs

    write_ohlcv(_frame(250), "LONGSYM", paths)
    upsert_macro_snapshot(
        conn,
        MacroSnapshot(
            date=date(2026, 6, 1),
            sgx_nifty=None,
            dow_fut=None,
            nasdaq_fut=None,
            sp500=None,
            usdinr=95.0,
            crude=None,
            vix=15.0,
            us_10y=None,
            fii_flow_cr=100.0,
            dii_flow_cr=200.0,
            regime="NEUTRAL",
        ),
    )
    conn.execute(
        "INSERT INTO sentiment_daily "
        "(date, symbol, score_7d, score_30d, news_count, negative_news_count, has_critical) "
        "VALUES ('2026-06-01', 'LONGSYM', 0.2, 0.1, 3, 1, 0)"
    )

    inputs = load_training_inputs(paths, conn)
    assert list(inputs.macro_history.index) == ["2026-06-01"]
    assert list(inputs.macro_history.columns) == ["vix", "usdinr", "fii_flow_cr"]
    assert ("2026-06-01", "LONGSYM") in inputs.sentiment_lookup
    assert inputs.sentiment_lookup[("2026-06-01", "LONGSYM")].score_7d == 0.2


def test_load_training_inputs_empty_universe(paths, conn) -> None:
    from trading.strategy.ranker_io import load_training_inputs

    inputs = load_training_inputs(paths, conn)
    assert inputs.enriched == {}
    assert inputs.macro_history.empty
    assert inputs.sentiment_lookup == {}
