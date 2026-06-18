"""Phase 15 — Streamlit dashboard entry point ("Overview" page).

Run with: ``uv run streamlit run src/trading/ui/app.py``
"""

from __future__ import annotations

import streamlit as st

from trading.ui import data
from trading.ui.charts import equity_curve, regime_history
from trading.ui.components import (
    divider,
    empty_state,
    format_currency,
    format_pct,
    kpi_tile,
    regime_badge,
    section_header,
    sidebar_date_picker,
)

st.set_page_config(
    page_title="Trading System",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("📈 Trading System")
st.sidebar.caption("Read-only dashboard · Phases 1-14")
as_of = sidebar_date_picker("As-of date")

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

macro = data.load_macro_snapshot(as_of)
regime = macro.get("regime") if macro else None

header_col1, header_col2 = st.columns([3, 1])
with header_col1:
    st.markdown(f"## Overview · {as_of}")
    st.caption("Daily snapshot of regime, equity, and what changed today.")
with header_col2:
    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
    regime_badge(regime)

divider()

# ---------------------------------------------------------------------------
# KPI tiles
# ---------------------------------------------------------------------------

equity, prev_equity, drawdown = data.latest_equity_snapshot()
open_trades_df = data.load_paper_trades(open_only=True)
n_open = 0 if open_trades_df is None or open_trades_df.empty else len(open_trades_df)

delta_pct: str | None = None
if equity is not None and prev_equity is not None and prev_equity != 0:
    pct = (equity - prev_equity) / prev_equity * 100.0
    delta_pct = format_pct(pct)

k1, k2, k3, k4 = st.columns(4)
with k1:
    kpi_tile("Equity", format_currency(equity), help="Latest portfolio_snapshots row")
with k2:
    kpi_tile(
        "Today",
        delta_pct or "—",
        delta_color="off" if delta_pct is None else "normal",
        help="Equity change vs previous snapshot",
    )
with k3:
    dd_label = f"{drawdown:.1f}%" if drawdown is not None else "—"
    kpi_tile("Drawdown", dd_label, delta_color="off")
with k4:
    kpi_tile("Open trades", str(n_open))

divider()

# ---------------------------------------------------------------------------
# Equity curve + macro snapshot
# ---------------------------------------------------------------------------

col_chart, col_macro = st.columns([2, 1])

with col_chart:
    section_header("Equity (last 90 snapshots)")
    snaps = data.load_portfolio_snapshots()
    if snaps.empty:
        empty_state(
            "No portfolio snapshots yet",
            "Run <code>trading post-close --apply &lt;date&gt;</code> after each session.",
        )
    else:
        st.plotly_chart(equity_curve(snaps.tail(90)), width="stretch", key="ov_equity")

with col_macro:
    section_header("Macro snapshot")
    if macro is None:
        empty_state(
            "No macro snapshot",
            f"Run <code>trading macro snapshot --date {as_of}</code>.",
        )
    else:
        rows = [
            ("VIX", macro.get("vix")),
            ("USDINR", macro.get("usdinr")),
            ("FII (₹ Cr)", macro.get("fii_flow_cr")),
            ("DII (₹ Cr)", macro.get("dii_flow_cr")),
            ("Crude", macro.get("crude")),
            ("US 10y", macro.get("us_10y")),
        ]
        for label, value in rows:
            disp = f"{value:.2f}" if isinstance(value, (int, float)) else "—"
            cl, cv = st.columns([1, 1])
            cl.markdown(f"<small>{label}</small>", unsafe_allow_html=True)
            cv.markdown(f"<b>{disp}</b>", unsafe_allow_html=True)

divider()

# ---------------------------------------------------------------------------
# Regime history
# ---------------------------------------------------------------------------

section_header("Regime history", "Step plot of RISK_ON / NEUTRAL / RISK_OFF over time")
hist = data.load_macro_history(limit=90)
if hist.empty:
    empty_state("No regime history yet")
else:
    st.plotly_chart(regime_history(hist), width="stretch", key="ov_regime_hist")

divider()

# ---------------------------------------------------------------------------
# Today's brief
# ---------------------------------------------------------------------------

section_header(
    "Today's brief",
    f"From <code>data/research/{as_of}/brief.md</code> · run <code>/analyst</code> then <code>trading brief compile {as_of}</code>",
)

brief_md = data.load_brief_section(as_of, "brief")
if brief_md:
    preview = brief_md[:1800]
    st.markdown(preview)
    if len(brief_md) > 1800:
        with st.expander("Show full brief"):
            st.markdown(brief_md)
else:
    mid_day = data.load_brief_section(as_of, "mid_day")
    post_close = data.load_brief_section(as_of, "post_close")
    if mid_day or post_close:
        if mid_day:
            st.markdown("**Mid-day update**")
            st.markdown(mid_day)
        if post_close:
            st.markdown("**Post-close summary**")
            st.markdown(post_close)
    else:
        empty_state(
            "No brief compiled for this date",
            "Run the daily pipeline: "
            f"<code>trading pre-open {as_of}</code> → <code>/analyst</code> → "
            f"<code>trading brief compile {as_of}</code>",
        )
