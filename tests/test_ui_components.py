"""Tests for trading.ui.components pure helpers (no Streamlit runtime needed)."""

from __future__ import annotations

import pytest

from trading.strategy.rules import LAYER_A_RULE_NAMES
from trading.ui.components import _rule_chip_items


def test_rule_chip_items_dict_shape() -> None:
    """Legacy ``{name: bool}`` payloads pass through as (name, passed) pairs."""
    parsed = {"uptrend": True, "pullback": False}
    assert _rule_chip_items(parsed) == [("uptrend", True), ("pullback", False)]


def test_rule_chip_items_list_cross_references_full_ruleset() -> None:
    """The current writer stores a list of *passed* names only; failed rules
    are absent and must render as ✗ against the canonical set."""
    passed = [n for n in LAYER_A_RULE_NAMES if n != "no_critical_event"]  # 9/10
    items = _rule_chip_items(passed)
    assert [name for name, _ in items] == list(LAYER_A_RULE_NAMES)  # all 10, in order
    by_name = dict(items)
    assert by_name["uptrend"] is True
    assert by_name["no_critical_event"] is False  # absent -> failed


def test_rule_chip_items_all_passed_list() -> None:
    items = _rule_chip_items(list(LAYER_A_RULE_NAMES))
    assert len(items) == len(LAYER_A_RULE_NAMES)
    assert all(ok for _, ok in items)


def test_rule_chip_items_rejects_unexpected_type() -> None:
    with pytest.raises(TypeError):
        _rule_chip_items(42)
