"""Tests for trading.features.sentiment — keyword classifier, aggregator,
plus one @slow real-FinBERT snapshot.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from trading.domain import NewsItem
from trading.features.sentiment import (
    NEGATIVE_THRESHOLD,
    ScoreResult,
    aggregate_daily,
    aggregate_symbol,
    classify_category,
    is_critical_headline,
    score_headline,
    score_news_items,
)
from trading.store.migrations import run_migrations
from trading.store.news_store import (
    get_sentiment_daily,
    insert_news_items,
)

# ---------------------------------------------------------------------------
# Critical-event classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headline",
    [
        "SEBI bars promoter of small-cap auto firm over disclosure lapses",
        "Auditor of XYZ Ltd resigns citing differences with management",
        "SEBI orders probe into accounting irregularities at ABC Ltd",
        "Promoter pledges further 5% of stake in DEF Ltd",
        "Auditor gives qualified opinion on FY26 statements",
        "ED raids GHI offices in money-laundering probe",
        "CBI probe ordered against JKL board member",
        "NCLT admits insolvency petition against MNO Steel",
        "PQR defaults on bond payment due May 14",
        "Investigation finds fraud in subsidiary books",
        "SAT order against SEBI ban set aside",
    ],
)
def test_is_critical_fires(headline: str) -> None:
    assert is_critical_headline(headline) is True


@pytest.mark.parametrize(
    "headline",
    [
        "Reliance Q4 results beat estimates",
        "NTPC commissions 800 MW solar capacity",
        "Nifty closes 1% higher on FII inflows",
        "Tata Power eyes 4 GW battery storage by FY28",
        "",
    ],
)
def test_is_critical_does_not_fire_on_neutral(headline: str) -> None:
    assert is_critical_headline(headline) is False


# ---------------------------------------------------------------------------
# Category classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Reliance Q4 results beat estimates", "results"),
        ("Acme CEO resigns after board fallout", "management"),
        ("SEBI proposes new disclosure norms", "regulatory"),
        ("XYZ announces 5:1 stock split", "dividend"),
        ("ABC announces acquisition of subsidiary", "M&A"),
        ("Broker downgrades stock to underweight", "downgrade"),
        ("Promoter pledged 3% of stake last month", "pledge"),
    ],
)
def test_classify_category(headline: str, expected: str) -> None:
    assert classify_category(headline) == expected


def test_classify_category_returns_none_when_no_keywords() -> None:
    assert classify_category("Market closes flat after volatile session") is None


def test_classify_category_handles_empty() -> None:
    assert classify_category("") is None


# ---------------------------------------------------------------------------
# score_headline with injected stub scorer
# ---------------------------------------------------------------------------


def _stub_scorer(text: str) -> ScoreResult:
    """Deterministic stub: positive for 'beat', negative for 'miss', else 0."""
    if "beat" in text.lower():
        score = 0.8
    elif "miss" in text.lower():
        score = -0.7
    else:
        score = 0.0
    return ScoreResult(
        score=score,
        category=classify_category(text),
        is_critical=is_critical_headline(text),
    )


def test_score_headline_uses_injected_scorer() -> None:
    result = score_headline("Reliance Q4 beat estimates", scorer=_stub_scorer)
    assert result.score == 0.8
    assert result.category == "results"
    assert result.is_critical is False


def test_score_news_items_populates_fields() -> None:
    items = [
        NewsItem(
            ts="2026-05-13T10:00:00+00:00",
            symbol="RELIANCE",
            source="mc",
            headline="Reliance Q4 beat estimates",
            url="https://x/1",
        ),
        NewsItem(
            ts="2026-05-13T11:00:00+00:00",
            symbol=None,
            source="mc",
            headline="SEBI bars promoter of ABC Ltd",
            url="https://x/2",
        ),
    ]
    scored = score_news_items(items, scorer=_stub_scorer)
    assert scored[0].sentiment == 0.8
    assert scored[0].category == "results"
    assert scored[0].is_critical is False
    assert scored[1].is_critical is True
    assert scored[1].category == "regulatory"


# ---------------------------------------------------------------------------
# Daily aggregator — synthetic news_items in an in-memory SQLite
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    run_migrations(c)
    return c


def _make_item(
    *,
    ts: datetime,
    symbol: str | None,
    sentiment: float | None,
    is_critical: bool = False,
    url: str | None = None,
) -> NewsItem:
    return NewsItem(
        ts=ts.isoformat(),
        symbol=symbol,
        source="test",
        headline=f"news for {symbol} @ {ts.isoformat()}",
        url=url or f"https://test/{ts.timestamp()}-{symbol}",
        sentiment=sentiment,
        category=None,
        is_critical=is_critical,
    )


def test_aggregate_symbol_computes_7d_and_30d_means(conn: sqlite3.Connection) -> None:
    as_of = date(2026, 5, 13)
    end_dt = datetime(2026, 5, 14, tzinfo=UTC)
    items = [
        # Inside 7d window
        _make_item(ts=end_dt - timedelta(days=1), symbol="RELIANCE", sentiment=0.5),
        _make_item(ts=end_dt - timedelta(days=3), symbol="RELIANCE", sentiment=-0.1),
        _make_item(ts=end_dt - timedelta(days=6), symbol="RELIANCE", sentiment=0.2),
        # 7d < t ≤ 30d
        _make_item(ts=end_dt - timedelta(days=15), symbol="RELIANCE", sentiment=-0.4),
        _make_item(ts=end_dt - timedelta(days=29), symbol="RELIANCE", sentiment=0.1),
        # Outside 30d window — must be excluded
        _make_item(ts=end_dt - timedelta(days=45), symbol="RELIANCE", sentiment=1.0),
    ]
    insert_news_items(conn, items)

    row = aggregate_symbol(conn, "RELIANCE", as_of)

    # 7d mean over [0.5, -0.1, 0.2] = 0.2
    assert row.score_7d == pytest.approx((0.5 - 0.1 + 0.2) / 3)
    # 30d mean over [0.5, -0.1, 0.2, -0.4, 0.1] = 0.06
    assert row.score_30d == pytest.approx((0.5 - 0.1 + 0.2 - 0.4 + 0.1) / 5)
    assert row.news_count == 5  # 30d window
    assert row.has_critical is False


def test_aggregate_symbol_flags_critical(conn: sqlite3.Connection) -> None:
    as_of = date(2026, 5, 13)
    end_dt = datetime(2026, 5, 14, tzinfo=UTC)
    items = [
        _make_item(
            ts=end_dt - timedelta(days=2),
            symbol="ABC",
            sentiment=-0.5,
            is_critical=True,
        ),
    ]
    insert_news_items(conn, items)
    row = aggregate_symbol(conn, "ABC", as_of)
    assert row.has_critical is True


def test_aggregate_symbol_counts_negative_below_threshold(
    conn: sqlite3.Connection,
) -> None:
    as_of = date(2026, 5, 13)
    end_dt = datetime(2026, 5, 14, tzinfo=UTC)
    # threshold is -0.20: -0.5 and -0.3 count, -0.1 does not
    items = [
        _make_item(ts=end_dt - timedelta(days=2), symbol="X", sentiment=-0.5),
        _make_item(ts=end_dt - timedelta(days=4), symbol="X", sentiment=-0.3),
        _make_item(ts=end_dt - timedelta(days=5), symbol="X", sentiment=-0.1),
        _make_item(ts=end_dt - timedelta(days=6), symbol="X", sentiment=0.4),
    ]
    insert_news_items(conn, items)
    row = aggregate_symbol(conn, "X", as_of)
    assert row.negative_news_count == 2
    assert row.news_count == 4


def test_aggregate_symbol_handles_zero_news(conn: sqlite3.Connection) -> None:
    row = aggregate_symbol(conn, "NOPE", date(2026, 5, 13))
    assert row.score_7d is None
    assert row.score_30d is None
    assert row.news_count == 0


def test_aggregate_daily_persists_and_skips_empties(conn: sqlite3.Connection) -> None:
    as_of = date(2026, 5, 13)
    end_dt = datetime(2026, 5, 14, tzinfo=UTC)
    items = [
        _make_item(ts=end_dt - timedelta(days=2), symbol="RELIANCE", sentiment=0.3),
    ]
    insert_news_items(conn, items)

    written = aggregate_daily(conn, ["RELIANCE", "NOPE"], as_of)
    assert [r.symbol for r in written] == ["RELIANCE"]

    row = get_sentiment_daily(conn, as_of.isoformat(), "RELIANCE")
    assert row is not None
    assert row.score_7d == pytest.approx(0.3)
    assert row.news_count == 1

    # NOPE was skipped because it had zero news
    assert get_sentiment_daily(conn, as_of.isoformat(), "NOPE") is None


def test_aggregate_daily_is_idempotent(conn: sqlite3.Connection) -> None:
    """Running twice on the same day overwrites instead of duplicating."""
    as_of = date(2026, 5, 13)
    end_dt = datetime(2026, 5, 14, tzinfo=UTC)
    items = [_make_item(ts=end_dt - timedelta(days=1), symbol="X", sentiment=0.4)]
    insert_news_items(conn, items)

    aggregate_daily(conn, ["X"], as_of)
    aggregate_daily(conn, ["X"], as_of)
    rows = conn.execute(
        "SELECT * FROM sentiment_daily WHERE date = ? AND symbol = ?",
        (as_of.isoformat(), "X"),
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Negative-threshold constant sanity
# ---------------------------------------------------------------------------


def test_negative_threshold_is_negative() -> None:
    assert NEGATIVE_THRESHOLD < 0


# ---------------------------------------------------------------------------
# Real-FinBERT snapshot — slow, downloads ~440MB on first run
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_finbert_real_model_directional_scores() -> None:
    """Smoke-check that ProsusAI/finbert distinguishes positive/negative
    sentences in the expected direction. Exact scores are not asserted
    (HF model versions drift); only the sign relationship is."""
    from trading.features.sentiment import finbert_score

    positive = finbert_score("Profits surged 50% year-on-year, beating estimates")
    negative = finbert_score("The company defaulted on its bond payment and filed for bankruptcy")
    neutral = finbert_score("The company will hold its AGM next month")

    assert positive > 0.3
    assert negative < -0.3
    assert -0.5 < neutral < 0.5
    assert positive > neutral > negative
