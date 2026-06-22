"""Neutral domain module — the single home for cross-layer DTOs (F-006).

These types describe *what a row is*; both `data` (producer) and `store`
(persister) import them from here so persistence no longer depends "up" into
the ingestion layer.
"""

from dataclasses import fields, is_dataclass

from trading.domain import (
    NSE_SUFFIX,
    REQUIRED_COLUMNS,
    MacroSnapshot,
    NewsItem,
    SectorRow,
)


def test_dtos_are_frozen_dataclasses() -> None:
    for dto in (SectorRow, MacroSnapshot, NewsItem):
        assert is_dataclass(dto)
        # frozen=True surfaces as a FrozenInstanceError on assignment; the
        # cheap structural proxy is the dataclass params flag.
        assert dto.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_ohlcv_schema_constants() -> None:
    assert NSE_SUFFIX == ".NS"
    assert REQUIRED_COLUMNS == ("open", "high", "low", "close", "volume")


def test_macro_snapshot_has_the_schema_columns() -> None:
    names = {f.name for f in fields(MacroSnapshot)}
    assert {"date", "regime", "vix", "usdinr", "fii_flow_cr"} <= names
