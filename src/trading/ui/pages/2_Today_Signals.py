"""Phase 15 — Today's Signals page.

Funnel from scanner universe → rules-pass → IEP-pass → auto-opened paper
trades. Per-candidate drill-down with chart + rule chip grid + brief.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from trading.ui import data
from trading.ui.charts import candlestick
from trading.ui.components import (
    divider,
    empty_state,
    kpi_tile,
    regime_badge,
    rule_chip_grid,
    section_header,
    sidebar_date_picker,
)

st.set_page_config(page_title="Today Signals · Trading", page_icon="🎯", layout="wide")

st.sidebar.title("🎯 Today Signals")
as_of = sidebar_date_picker("As-of date")

# Header
macro = data.load_macro_snapshot(as_of)
regime = macro.get("regime") if macro else None
c1, c2 = st.columns([3, 1])
with c1:
    st.markdown(f"## Today's Signals · {as_of}")
    st.caption("Scanner output, why each candidate passed/failed, and what got opened.")
with c2:
    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
    regime_badge(regime)
divider()

# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------

signals_df = data.load_signals_by_date(as_of)
paper_df = data.load_paper_trades()
paper_today_df = (
    paper_df[paper_df["ts_entry"].str.startswith(as_of)]
    if not paper_df.empty
    else pd.DataFrame()
)
candidate_briefs = data.list_candidate_briefs(as_of)

n_signals = len(signals_df)
n_candidates = len(candidate_briefs)
n_opened = len(paper_today_df)

# We don't currently persist the full universe count — surface what we know.
section_header("Funnel")
k1, k2, k3 = st.columns(3)
with k1:
    kpi_tile("Candidates analysed", str(n_candidates))
with k2:
    kpi_tile(
        "Signals generated",
        str(n_signals),
        help="Rows in `signals` table for this date.",
    )
with k3:
    kpi_tile(
        "Auto-opened paper trades",
        str(n_opened),
        help="`paper_trades` rows whose ts_entry is on this date.",
    )

divider()

# ---------------------------------------------------------------------------
# Regime banner
# ---------------------------------------------------------------------------

if regime == "RISK_OFF":
    st.error(
        "**RISK_OFF regime** — pre_open opens 0 new trades. Signals here would "
        "be paper-only for review."
    )
elif regime == "RISK_ON":
    st.success("**RISK_ON regime** — full position size multiplier applies.")
elif regime == "NEUTRAL":
    st.info("**NEUTRAL regime** — position size multiplier 0.75x.")
else:
    st.warning("Macro snapshot missing — regime unknown.")

divider()

# ---------------------------------------------------------------------------
# Signals table
# ---------------------------------------------------------------------------

section_header("Signals")

if signals_df.empty:
    empty_state(
        "No signals persisted for this date",
        f"Run <code>trading pre-open {as_of}</code> to score candidates; the scanner "
        "writes a `signals` row for each rules-pass + opens a paper trade.",
    )
else:
    cols = [
        "symbol",
        "side",
        "entry",
        "stop",
        "target",
        "horizon_days",
        "conviction",
        "ml_score",
    ]
    available = [c for c in cols if c in signals_df.columns]
    show = signals_df[available].copy()
    risk = signals_df["entry"] - signals_df["stop"]
    reward = signals_df["target"] - signals_df["entry"]
    show["R:R"] = (reward / risk.where(risk != 0)).round(2)
    st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        column_config={
            "entry": st.column_config.NumberColumn(format="₹%.2f"),
            "stop": st.column_config.NumberColumn(format="₹%.2f"),
            "target": st.column_config.NumberColumn(format="₹%.2f"),
            "ml_score": st.column_config.NumberColumn(format="%.3f"),
        },
    )

    section_header("Rule evaluation per signal")
    for _, row in signals_df.iterrows():
        st.markdown(f"**{row['symbol']}** · {row.get('conviction') or '—'}")
        rule_chip_grid(row.get("rules_passed_json"))
        st.markdown("")

divider()

# ---------------------------------------------------------------------------
# Per-candidate drill-down
# ---------------------------------------------------------------------------

section_header(
    "Per-candidate detail",
    "Pick a candidate to inspect its chart + brief excerpt.",
)

# Prefer the briefs list (covers candidates even when no signal persisted);
# fall back to signals when no briefs exist.
options = candidate_briefs or (
    sorted(signals_df["symbol"].unique().tolist()) if not signals_df.empty else []
)

if not options:
    empty_state("No candidate briefs yet")
else:
    pick = st.selectbox("Candidate", options=options, key="signal_pick")
    if pick:
        left, right = st.columns([3, 2])
        with left:
            hist = data.load_ohlcv_enriched(pick, tail=180)
            if hist.empty:
                empty_state(f"No parquet history for {pick}")
            else:
                st.plotly_chart(candlestick(hist, symbol=pick), width="stretch", key=f"sig_candle_{pick}")
        with right:
            brief_md = data.load_brief_section(as_of, f"candidates/{pick}.md")
            if brief_md:
                st.markdown(brief_md)
            else:
                empty_state(
                    f"No brief for {pick}",
                    "Run <code>/analyst</code> after <code>trading brief assemble-context</code>.",
                )
