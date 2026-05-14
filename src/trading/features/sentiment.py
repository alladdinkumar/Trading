"""Headline sentiment scoring + critical-event flagging + daily aggregation.

Pipeline (one headline at a time):
  text  →  FinBERT (3-class softmax)  →  score ∈ [-1, +1]
        →  category keyword match      →  category tag
        →  critical regex match        →  is_critical flag

`load_finbert` is lazy and singleton — the model is ~440MB so we never
import torch/transformers at module load, and tests inject a stub scorer
instead. One `@pytest.mark.slow` test exercises the real model.

The aggregator reads `news_items` rows for the trailing 30 days and writes
one `sentiment_daily` row per (date, symbol).
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from trading.config import get_paths
from trading.data.news import NewsItem
from trading.store.news_store import (
    SentimentDailyRow,
    list_news_for_symbol,
    upsert_sentiment_daily,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreResult:
    """Output of `score_headline`. Score in [-1, +1] (positive = bullish)."""

    score: float
    category: str | None
    is_critical: bool


# A scorer is anything callable on a headline string. Production wires it to
# FinBERT; tests pass a deterministic stub.
Scorer = Callable[[str], ScoreResult]


# ---------------------------------------------------------------------------
# Critical-event detection (Section 5.1 hard veto)
# ---------------------------------------------------------------------------

# Word-boundary regexes — case-insensitive at match time. Tuned to fire on
# headlines like "SEBI bars X for ...", "Auditor of X resigns", "Promoter
# pledges further 5% of stake". False-positive cost is low (we re-check at
# narrative time); false-negative cost is missing a vetoable event.
CRITICAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bSEBI\b.*\b(bar|ban|fine|penal|probe|investigat|order)",
        r"\bSAT\b.*\b(order|appeal|case)",
        r"\bfraud\b",
        r"\bauditor\b.*\b(resign|step\s*down|quit)",
        r"\bqualified\s+opinion\b",
        r"\bgoing\s+concern\b",
        r"\bpromoter\b.*\bpledge",
        r"\bpledge.*\bincreas|invok|sold",
        r"\bdefault(s|ed|ing)?\b.*\b(loan|bond|payment)",
        r"\binsolvency\b",
        r"\bNCLT\b",
        r"\baccount(ing)?\s+irregularit",
        r"\bED\b.*\b(raid|attach|probe)",
        r"\bCBI\b.*\b(raid|probe|case)",
    )
)

# Category keywords. Order matters — first match wins. We deliberately keep
# this short and high-precision; the LightGBM ranker only consumes
# `negative_news_count` and `news_count` as features, so misclassification
# inside the negative bucket isn't load-bearing.
CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("results", ("results", "quarterly", "Q1", "Q2", "Q3", "Q4", "earnings", "profit", "revenue")),
    ("management", ("CEO", "CFO", "MD", "chairman", "resign", "appoint")),
    ("regulatory", ("SEBI", "RBI", "SAT", "tribunal", "court", "regulator", "compliance")),
    ("M&A", ("acquisition", "merger", "stake sale", "buyout", "takeover", "joint venture")),
    ("downgrade", ("downgrade", "cut", "lower", "underweight", "sell rating")),
    ("dividend", ("dividend", "bonus", "split", "buyback")),
    ("pledge", ("pledge", "pledged", "encumbrance")),
)


def is_critical_headline(text: str) -> bool:
    """True if any critical-event pattern fires. Empty → False."""
    if not text:
        return False
    return any(p.search(text) for p in CRITICAL_PATTERNS)


def classify_category(text: str) -> str | None:
    """Return the first matching category, or None if no keywords hit."""
    if not text:
        return None
    lower = text.lower()
    for label, kws in CATEGORY_KEYWORDS:
        if any(kw.lower() in lower for kw in kws):
            return label
    return None


# ---------------------------------------------------------------------------
# FinBERT scorer
# ---------------------------------------------------------------------------

FINBERT_MODEL_ID = "ProsusAI/finbert"
_FINBERT_PIPELINE: Any | None = None  # singleton, lazy


def load_finbert() -> Any:
    """Lazy singleton load of ProsusAI/finbert (CPU, ~440MB on first call).

    The first call downloads to `data/cache/models/` (set via
    `HF_HOME`). Subsequent calls return the cached pipeline. Returning
    `Any` keeps this file importable without `transformers` installed for
    the few callers that only need the keyword/category bits.
    """
    global _FINBERT_PIPELINE
    if _FINBERT_PIPELINE is not None:
        return _FINBERT_PIPELINE

    paths = get_paths()
    models_dir = paths.cache_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(models_dir))

    from transformers import pipeline  # local import: heavy

    _FINBERT_PIPELINE = pipeline(
        task="text-classification",
        model=FINBERT_MODEL_ID,
        top_k=None,  # return all class scores so we can compute pos − neg
        truncation=True,
        max_length=256,
    )
    return _FINBERT_PIPELINE


def finbert_score(text: str, *, pipe: Any | None = None) -> float:
    """Run a headline through FinBERT and return score = P(pos) − P(neg) ∈ [-1, +1].

    Empty/whitespace input → 0.0. Reuses the loaded pipeline unless one is
    explicitly passed (useful for parallel batches).
    """
    if not text or not text.strip():
        return 0.0
    pipe = pipe if pipe is not None else load_finbert()
    raw = pipe(text)
    # pipeline with top_k=None returns [[{label, score}, ...]] for one input.
    scores: list[dict[str, Any]] = raw[0] if raw and isinstance(raw[0], list) else raw
    by_label = {d["label"].lower(): float(d["score"]) for d in scores}
    return by_label.get("positive", 0.0) - by_label.get("negative", 0.0)


def _finbert_scorer(text: str) -> ScoreResult:
    """Production scorer: FinBERT + keyword category + critical regex."""
    return ScoreResult(
        score=finbert_score(text),
        category=classify_category(text),
        is_critical=is_critical_headline(text),
    )


def score_headline(text: str, *, scorer: Scorer | None = None) -> ScoreResult:
    """Public entry point. `scorer` injection is for tests; prod uses FinBERT."""
    if scorer is not None:
        return scorer(text)
    return _finbert_scorer(text)


def score_news_items(
    items: Iterable[NewsItem], *, scorer: Scorer | None = None
) -> list[NewsItem]:
    """Return new NewsItems with sentiment/category/is_critical populated.

    Pure: doesn't write to the DB. Caller is responsible for persisting the
    result (typically via a fresh insert into `news_items`).
    """
    out: list[NewsItem] = []
    for item in items:
        result = score_headline(item.headline, scorer=scorer)
        out.append(
            NewsItem(
                ts=item.ts,
                symbol=item.symbol,
                source=item.source,
                headline=item.headline,
                url=item.url,
                sentiment=result.score,
                category=result.category,
                is_critical=result.is_critical,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Daily aggregation
# ---------------------------------------------------------------------------

NEGATIVE_THRESHOLD = -0.20


def _mean_or_none(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    as_of: date,
) -> SentimentDailyRow:
    """Compute the trailing 7d & 30d rollup for one symbol as of `as_of`.

    `news_items.ts` is ISO 8601 (UTC); we compare by lexicographic prefix
    because that respects the same ordering as datetime comparison for
    ISO 8601. `as_of` is inclusive of its full day (end of day in UTC).
    """
    end_dt = datetime.combine(as_of + timedelta(days=1), datetime.min.time())
    since_30d = end_dt - timedelta(days=30)
    since_7d = end_dt - timedelta(days=7)

    items = list_news_for_symbol(conn, symbol, since_ts=since_30d.isoformat())
    items = [i for i in items if i.ts < end_dt.isoformat()]

    scored_30d = [i.sentiment for i in items if i.sentiment is not None]
    scored_7d = [
        i.sentiment for i in items if i.sentiment is not None and i.ts >= since_7d.isoformat()
    ]

    return SentimentDailyRow(
        date=as_of.isoformat(),
        symbol=symbol,
        score_7d=_mean_or_none(scored_7d),
        score_30d=_mean_or_none(scored_30d),
        news_count=len(items),
        negative_news_count=sum(
            1
            for i in items
            if i.sentiment is not None and i.sentiment <= NEGATIVE_THRESHOLD
        ),
        has_critical=any(i.is_critical for i in items),
    )


def aggregate_daily(
    conn: sqlite3.Connection,
    symbols: Iterable[str],
    as_of: date,
) -> list[SentimentDailyRow]:
    """Compute and persist one `sentiment_daily` row per symbol.

    Returns the rows that were written. Skips symbols with zero news in the
    30-day window so we don't backfill the table with empty rollups (those
    would still satisfy COALESCE(score, 0) downstream — but reading zero rows
    for an unwatched symbol is the cleaner signal of "we never saw news").
    """
    rows: list[SentimentDailyRow] = []
    for sym in symbols:
        row = aggregate_symbol(conn, sym, as_of)
        if row.news_count == 0:
            continue
        upsert_sentiment_daily(conn, row)
        rows.append(row)
    return rows
