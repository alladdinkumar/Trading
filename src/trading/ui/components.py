"""Phase 15 — shared Streamlit widgets for the dashboard.

Small Streamlit-aware helpers that pages compose. Anything that draws a
chart should live in ``charts.py``; this module is for layout, badges,
metric tiles, and empty-state panels.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal

import streamlit as st

from trading.strategy.rules import LAYER_A_RULE_NAMES
from trading.ui.charts import (
    COLOR_INFO,
    COLOR_MUTED,
    COLOR_NEGATIVE,
    COLOR_NEUTRAL,
    COLOR_POSITIVE,
)
from trading.ui.data import available_research_dates

DeltaColor = Literal["normal", "inverse", "off"]

_REGIME_COLORS = {
    "RISK_ON": COLOR_POSITIVE,
    "NEUTRAL": COLOR_NEUTRAL,
    "RISK_OFF": COLOR_NEGATIVE,
}

_HEALTH_COLORS = {
    "HOLD": COLOR_POSITIVE,
    "TRIM": COLOR_NEUTRAL,
    "EXIT": COLOR_NEGATIVE,
}


def regime_badge(regime: str | None) -> None:
    """Render a coloured regime chip inline (HTML, no chart)."""
    label = regime or "—"
    color = _REGIME_COLORS.get(label, COLOR_MUTED)
    st.markdown(
        f"""
        <div style="display:inline-block; padding:6px 14px;
                    background:{color}22; border:1px solid {color};
                    border-radius:14px; font-weight:600; color:{color};
                    font-size:0.9rem; letter-spacing:0.5px;">
          ● {label}
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi_tile(
    label: str,
    value: str,
    *,
    delta: str | None = None,
    delta_color: DeltaColor = "normal",
    help: str | None = None,
) -> None:
    """Thin wrapper over ``st.metric`` so call-sites stay uniform.

    ``delta_color`` matches st.metric's API: "normal", "inverse", "off".
    """
    st.metric(label=label, value=value, delta=delta, delta_color=delta_color, help=help)


def health_chip(verdict: str | None, score: int | None = None) -> str:
    """Return HTML for a HOLD/TRIM/EXIT chip — use with st.markdown(unsafe_allow_html)."""
    if verdict is None:
        verdict = "—"
    color = _HEALTH_COLORS.get(verdict, COLOR_MUTED)
    body = verdict if score is None else f"{verdict} · {score}"
    return (
        f'<span style="padding:2px 8px; background:{color}22;'
        f" border:1px solid {color}; border-radius:10px;"
        f' color:{color}; font-weight:600; font-size:0.8rem;">{body}</span>'
    )


# Legacy rule-name aliases — map a name persisted under an old identity onto the
# current canonical name (F-020: the Layer-A "regime" gate was renamed
# "market_filter" to stop colliding with the macro regime classifier).
_LEGACY_RULE_ALIASES: dict[str, str] = {"regime": "market_filter"}


def _rule_chip_items(parsed: object) -> list[tuple[str, bool]]:
    """Normalize the parsed ``rules_passed_json`` payload into (name, passed) pairs.

    Two on-disk shapes are supported:

    - ``dict`` ``{name: bool}`` — legacy/explicit pass-fail map, used verbatim.
    - ``list`` ``[name, …]`` — the current writer (``pre_open``) persists only
      the *passed* rule names. Failed rules are absent, so we cross-reference
      :data:`LAYER_A_RULE_NAMES` to render the full grid (absent ⇒ failed).

    Legacy rule-name aliases (F-020: ``regime`` → ``market_filter``) are applied
    so signals persisted before a rule rename still render against the current
    canonical set.

    Raises ``TypeError`` on any other shape so the caller can show a fallback.
    """
    if isinstance(parsed, dict):
        return [
            (_LEGACY_RULE_ALIASES.get(str(name), str(name)), bool(ok))
            for name, ok in parsed.items()
        ]
    if isinstance(parsed, list):
        passed = {_LEGACY_RULE_ALIASES.get(str(name), str(name)) for name in parsed}
        return [(name, name in passed) for name in LAYER_A_RULE_NAMES]
    raise TypeError(f"unexpected rules_passed payload: {type(parsed).__name__}")


def rule_chip_grid(rules_passed_json: str | None) -> None:
    """Render a horizontal grid of pass/fail chips for the 10 Layer-A rules.

    Accepts the JSON column verbatim — either a ``{"rule_name": true/false}``
    map or the writer's ``["passed_rule", …]`` list (see :func:`_rule_chip_items`).
    Falls back to "no rule data" when missing/empty/unparseable.
    """
    if not rules_passed_json:
        st.caption("_(no rule evaluation captured)_")
        return
    try:
        items = _rule_chip_items(json.loads(rules_passed_json))
    except (json.JSONDecodeError, TypeError):
        st.caption("_(rule data unparseable)_")
        return
    if not items:
        st.caption("_(no rule data)_")
        return
    chips = []
    for name, ok in items:
        color = COLOR_POSITIVE if ok else COLOR_NEGATIVE
        glyph = "✓" if ok else "✗"
        short = name.replace("passes_", "").replace("_", " ")
        chips.append(
            f'<span style="margin:2px 4px 2px 0; padding:2px 8px;'
            f" background:{color}22; border:1px solid {color};"
            f' border-radius:8px; color:{color}; font-size:0.75rem;">'
            f"{glyph} {short}</span>"
        )
    st.markdown("".join(chips), unsafe_allow_html=True)


def empty_state(title: str, hint: str | None = None) -> None:
    """Large grey placeholder for missing data with an optional next-step hint."""
    body = (
        f'<div style="padding:32px; border:1px dashed {COLOR_MUTED};'
        f' border-radius:8px; text-align:center; color:{COLOR_MUTED};">'
        f'<div style="font-size:1.1rem; font-weight:600; margin-bottom:8px;">'
        f"{title}</div>"
    )
    if hint:
        body += f'<div style="font-size:0.9rem;">{hint}</div>'
    body += "</div>"
    st.markdown(body, unsafe_allow_html=True)


def stale_quote_tag(capture_iso: str | None) -> None:
    """Inline italic tag noting when the intraday quote snapshot was captured."""
    if not capture_iso:
        return
    try:
        captured = datetime.fromisoformat(capture_iso)
    except ValueError:
        return
    age_min = int((datetime.now() - captured).total_seconds() // 60)
    color = COLOR_NEUTRAL if age_min > 30 else COLOR_MUTED
    label = f"quote captured {captured.strftime('%H:%M')} ({age_min} min ago)"
    st.markdown(
        f'<div style="color:{color}; font-style:italic; font-size:0.8rem;">{label}</div>',
        unsafe_allow_html=True,
    )


def sidebar_date_picker(label: str = "Date", *, key: str = "as_of") -> str:
    """Sidebar date picker that snaps to the set of dates with research artefacts.

    Returns an ISO date string. If no research dirs exist, falls back to today.
    """
    dates = available_research_dates()
    if not dates:
        d = st.sidebar.date_input(label, value=date.today(), key=key)
        return d.isoformat() if isinstance(d, date) else str(d)
    # Use a select box so the picker is constrained to dates with data
    default_idx = 0  # newest first
    selected = st.sidebar.selectbox(label, options=dates, index=default_idx, key=key)
    return str(selected)


def section_header(title: str, sub: str | None = None) -> None:
    """Tight section header with optional muted sub-text."""
    st.markdown(
        f'<h3 style="margin-bottom:0.2rem;">{title}</h3>'
        + (
            f'<div style="color:{COLOR_MUTED}; margin-bottom:0.6rem; font-size:0.85rem;">{sub}</div>'
            if sub
            else ""
        ),
        unsafe_allow_html=True,
    )


def format_currency(value: float | int | None, *, prefix: str = "₹") -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{prefix}{v:,.0f}"


def format_pct(value: float | int | None, *, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.{digits}f}%"


def divider() -> None:
    st.markdown(
        '<hr style="border-color:#262730; margin:0.6rem 0 1rem 0;" />',
        unsafe_allow_html=True,
    )


# Re-export so pages can `from trading.ui.components import COLOR_*`
__all__ = [
    "COLOR_INFO",
    "COLOR_MUTED",
    "COLOR_NEGATIVE",
    "COLOR_NEUTRAL",
    "COLOR_POSITIVE",
    "divider",
    "empty_state",
    "format_currency",
    "format_pct",
    "health_chip",
    "kpi_tile",
    "regime_badge",
    "rule_chip_grid",
    "section_header",
    "sidebar_date_picker",
    "stale_quote_tag",
]
