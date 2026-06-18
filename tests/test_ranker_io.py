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
    assert inputs.negative_news_lookup == {}


def _news_row(symbol: str, ts: str, sentiment: float):
    from trading.data.news import NewsItem

    return NewsItem(
        ts=ts,
        symbol=symbol,
        source="mc",
        headline=f"{symbol} {ts}",
        url=f"https://x/{symbol}/{ts}",
        sentiment=sentiment,
    )


def test_load_training_inputs_builds_negative_news_lookup(paths, conn) -> None:
    """F-031: the training-input loader populates negative_news_lookup using the
    same 7d query as inference, keyed (date_iso, symbol) over each symbol's
    trading-date index — so weekly_train no longer starves the feature."""
    from trading.store.news_store import insert_news_items, negative_news_count_7d
    from trading.strategy.ranker_io import load_training_inputs

    # OHLCV index spans 2025-01-01 .. (250 business days). Put two negative news
    # items a few days before a known in-range trading date.
    write_ohlcv(_frame(250), "LONGSYM", paths)
    insert_news_items(
        conn,
        [
            _news_row("LONGSYM", "2025-03-03T10:00:00+00:00", -0.5),
            _news_row("LONGSYM", "2025-03-05T10:00:00+00:00", -0.3),
            _news_row("LONGSYM", "2025-03-04T10:00:00+00:00", 0.4),  # positive
        ],
    )

    inputs = load_training_inputs(paths, conn)
    lookup = inputs.negative_news_lookup

    # The lookup must agree with the shared inference function on every key.
    assert lookup, "expected at least one negative-news lookup entry"
    for (date_iso, symbol), count in lookup.items():
        assert count == negative_news_count_7d(conn, symbol, date.fromisoformat(date_iso))
    # On 2025-03-06 (a Thursday, in the index), the trailing 7d holds both negatives.
    assert lookup[("2025-03-06", "LONGSYM")] == 2


def test_load_training_inputs_negative_news_lookup_empty_without_news(paths, conn) -> None:
    from trading.strategy.ranker_io import load_training_inputs

    write_ohlcv(_frame(250), "LONGSYM", paths)
    inputs = load_training_inputs(paths, conn)
    assert inputs.negative_news_lookup == {}
