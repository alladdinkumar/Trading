"""Tests for trading.data.reconcile — pure macro cross-source tolerance core (F-036)."""

from __future__ import annotations

from trading.data.macro import MacroSnapshot
from trading.data.macro_cross import MacroCrossSource
from trading.data.reconcile import reconcile_macro


def _snap(**over: object) -> MacroSnapshot:
    base = {
        "date": "2026-06-19",
        "sgx_nifty": None,
        "dow_fut": None,
        "nasdaq_fut": None,
        "sp500": None,
        "usdinr": 83.10,
        "crude": None,
        "vix": 19.40,
        "us_10y": None,
        "fii_flow_cr": 1234.0,
        "dii_flow_cr": -567.0,
        "regime": "NEUTRAL",
    }
    base.update(over)
    return MacroSnapshot(**base)  # type: ignore[arg-type]


def _cross(*, vix=None, usdinr=None) -> MacroCrossSource:
    return MacroCrossSource(
        source="kite_mcp",
        captured_at="2026-06-19T08:15:00",
        vix=vix,
        usdinr=usdinr,
    )


def _by_field(rows):
    return {r.field: r for r in rows}


def test_vix_within_tolerance_is_ok() -> None:
    rows = _by_field(reconcile_macro(_snap(vix=19.40), _cross(vix=19.55), checked_at="t"))
    assert rows["vix"].status == "ok"
    assert rows["vix"].primary_value == 19.40
    assert rows["vix"].secondary_value == 19.55
    assert abs(rows["vix"].abs_delta - 0.15) < 1e-9


def test_vix_beyond_tolerance_is_mismatch() -> None:
    rows = _by_field(reconcile_macro(_snap(vix=19.40), _cross(vix=22.0), checked_at="t"))
    assert rows["vix"].status == "mismatch"


def test_usdinr_uses_relative_tolerance() -> None:
    # 83.10 vs 83.40 → 0.36% < 0.5% → ok
    rows = _by_field(reconcile_macro(_snap(usdinr=83.10), _cross(usdinr=83.40), checked_at="t"))
    assert rows["usdinr"].status == "ok"
    # 83.10 vs 84.00 → ~1.08% > 0.5% → mismatch
    rows2 = _by_field(reconcile_macro(_snap(usdinr=83.10), _cross(usdinr=84.00), checked_at="t"))
    assert rows2["usdinr"].status == "mismatch"


def test_missing_secondary_is_flagged() -> None:
    rows = _by_field(reconcile_macro(_snap(vix=19.40), _cross(vix=None), checked_at="t"))
    assert rows["vix"].status == "missing_secondary"
    assert rows["vix"].abs_delta is None


def test_missing_primary_is_flagged() -> None:
    rows = _by_field(reconcile_macro(_snap(vix=None), _cross(vix=19.55), checked_at="t"))
    assert rows["vix"].status == "missing_primary"


def test_fii_dii_are_unreconciled() -> None:
    """Kite has no flow feed → FII/DII are always flagged unreconciled, never ok."""
    rows = _by_field(reconcile_macro(_snap(), _cross(vix=19.55, usdinr=83.1), checked_at="t"))
    assert rows["fii_flow_cr"].status == "unreconciled"
    assert rows["dii_flow_cr"].status == "unreconciled"
    assert rows["fii_flow_cr"].primary_value == 1234.0
    assert rows["fii_flow_cr"].secondary_value is None


def test_checked_at_is_stamped_on_every_row() -> None:
    rows = reconcile_macro(_snap(), _cross(vix=19.55), checked_at="2026-06-19T08:20:00")
    assert rows
    assert all(r.checked_at == "2026-06-19T08:20:00" for r in rows)


def test_sources_are_recorded() -> None:
    rows = _by_field(reconcile_macro(_snap(vix=19.4), _cross(vix=19.5), checked_at="t"))
    assert rows["vix"].primary_source == "yfinance"
    assert rows["vix"].secondary_source == "kite_mcp"
