"""Phase 16 inference — score rules-passing candidates + top-K filter.

Cold-start: when no active model is registered, every candidate is returned
with `selected=True, ml_score=None` so pre_open behaviour is unchanged.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from trading.strategy.rules import Candidate

if TYPE_CHECKING:
    from trading.backtest.engine import BacktestConfig, Signal
    from trading.config import Paths
    from trading.strategy.rules import ScanContext


@dataclass(frozen=True)
class ScoredCandidate:
    """A rules-passing candidate after the ranker stage."""

    candidate: Candidate
    ml_score: float | None
    selected: bool


def score_and_filter(
    candidates: list[Candidate],
    paths: "Paths",
    conn: sqlite3.Connection,
    as_of: date,
    *,
    k: int = 5,
) -> list[ScoredCandidate]:
    raise NotImplementedError


class RankerSignalProvider:
    """Companion to `rule_signal_provider` — used inside walk-forward test folds."""

    def __init__(self, model: object, top_k: int = 5) -> None:
        raise NotImplementedError

    def __call__(
        self,
        d: object,
        enriched: object,
        ctx: "ScanContext",
        config: "BacktestConfig",
    ) -> list["Signal"]:
        raise NotImplementedError
