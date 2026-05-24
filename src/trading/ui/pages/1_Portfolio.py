"""Phase 15 — Portfolio page.

Live holdings, sector concentration, GTT viability, per-symbol detail.
Reads from data/raw/<as_of>/holdings.json + gtts.json (via kite_snapshot)
and falls back to empty-states when those snapshots are missing.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from trading.portfolio.gtt import project_all_gtts
from trading.ui import data
from trading.ui.charts import candlestick, sector_pie
from trading.ui.components import (
    divider,
    empty_state,
    format_currency,
    format_pct,
    kpi_tile,
    regime_badge,
    section_header,
    sidebar_date_picker,
    stale_quote_tag,
)

st.set_page_config(page_title="Portfolio · Trading", page_icon="📊", layout="wide")

st.sidebar.title("📊 Portfolio")
as_of = sidebar_date_picker("As-of date")

# Header
macro = data.load_macro_snapshot(as_of)
regime = macro.get("regime") if macro else None
c1, c2 = st.columns([3, 1])
with c1:
    st.markdown(f"## Portfolio · {as_of}")
    st.caption("Live holdings, GTT viability, sector concentration.")
with c2:
    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
    regime_badge(regime)
divider()

# ---------------------------------------------------------------------------
# Load Kite snapshot
# ---------------------------------------------------------------------------

holdings = data.load_holdings(as_of)
gtts = data.load_gtts(as_of)
quote_blob = data.load_quote_snapshot(as_of)
quote_map: dict[str, dict[str, Any]] = quote_blob[0] if quote_blob else {}
quote_ts = quote_blob[1] if quote_blob else None

if not holdings:
    empty_state(
        "No Kite snapshot for this date",
        f"Run <code>/kite-snapshot</code> in Claude Code for {as_of} to fetch "
        "holdings + GTTs from your Zerodha account.",
    )
    st.stop()

# Equity totals from holdings (LTP × qty)
total_value = sum(h["last_price"] * h["quantity"] for h in holdings)
total_invested = sum(h["average_price"] * h["quantity"] for h in holdings)
total_pnl = total_value - total_invested
n_holdings = len(holdings)
n_gtts = len(gtts)

k1, k2, k3, k4 = st.columns(4)
with k1:
    kpi_tile("Holdings value", format_currency(total_value))
with k2:
    pct = (total_pnl / total_invested * 100.0) if total_invested else 0.0
    kpi_tile(
        "Unrealised P&L",
        format_currency(total_pnl),
        delta=format_pct(pct),
        # "normal": positive=green, negative=red — correct for P&L
        delta_color="normal",
    )
with k3:
    kpi_tile("Holdings", str(n_holdings))
with k4:
    kpi_tile("Active GTTs", str(n_gtts))

stale_quote_tag(quote_ts)

divider()

# ---------------------------------------------------------------------------
# Holdings table
# ---------------------------------------------------------------------------

section_header("Holdings")

rows = []
for h in holdings:
    sym = h["tradingsymbol"]
    qty = h["quantity"]
    avg = h["average_price"]
    ltp = h["last_price"]
    invested = avg * qty
    value = ltp * qty
    pnl = value - invested
    pnl_pct = (pnl / invested * 100.0) if invested else 0.0
    weight = (value / total_value * 100.0) if total_value else 0.0
    day_chg = h.get("day_change_percentage")
    rows.append(
        {
            "Symbol": sym,
            "Qty": qty,
            "Avg": avg,
            "LTP": ltp,
            "P&L ₹": pnl,
            "P&L %": pnl_pct,
            "Day %": day_chg,
            "Weight %": weight,
        }
    )
holdings_df = pd.DataFrame(rows).sort_values("Weight %", ascending=False)

st.dataframe(
    holdings_df,
    width="stretch",
    hide_index=True,
    column_config={
        "Avg": st.column_config.NumberColumn(format="₹%.2f"),
        "LTP": st.column_config.NumberColumn(format="₹%.2f"),
        "P&L ₹": st.column_config.NumberColumn(format="₹%.0f"),
        "P&L %": st.column_config.NumberColumn(format="%.2f%%"),
        "Day %": st.column_config.NumberColumn(format="%.2f%%"),
        "Weight %": st.column_config.ProgressColumn(
            format="%.1f%%", min_value=0.0, max_value=max(50.0, float(holdings_df["Weight %"].max() or 1.0))
        ),
    },
)

divider()

# ---------------------------------------------------------------------------
# Sector pie (placeholder mapping — symbol → sector not yet in DB)
# ---------------------------------------------------------------------------

# Until Phase 12.6 lands a sector mapping, group by sector if present in
# the holdings JSON, otherwise treat each symbol as its own bucket.
sector_rows = []
by_label: dict[str, float] = {}
for h in holdings:
    label = h.get("sector") or h["tradingsymbol"]
    by_label[label] = by_label.get(label, 0.0) + h["last_price"] * h["quantity"]
for label, val in by_label.items():
    sector_rows.append({"label": label, "value": val})

c1, c2 = st.columns([1, 1])
with c1:
    section_header("Concentration", "By symbol (sector mapping arrives in Phase 12.6)")
    st.plotly_chart(sector_pie(sector_rows), width="stretch", key="port_sector_pie")
with c2:
    section_header("Top concentration")
    top = (
        pd.DataFrame(sector_rows)
        .sort_values("value", ascending=False)
        .head(5)
        .assign(weight=lambda d: d["value"] / d["value"].sum() * 100.0)
    )
    st.dataframe(
        top.rename(columns={"label": "Symbol/Sector", "value": "Value", "weight": "Weight %"}),
        hide_index=True,
        width="stretch",
        column_config={
            "Value": st.column_config.NumberColumn(format="₹%.0f"),
            "Weight %": st.column_config.ProgressColumn(
                format="%.1f%%", min_value=0.0, max_value=100.0
            ),
        },
    )

divider()

# ---------------------------------------------------------------------------
# GTT viability
# ---------------------------------------------------------------------------

section_header("GTT viability", "Monte-Carlo P(target hit within horizon)")

if not gtts:
    empty_state("No active GTTs", "Place GTTs in Zerodha then re-run <code>/kite-snapshot</code>.")
else:
    typed_holdings = data.load_holdings_typed(as_of)
    typed_gtts = data.load_gtts_typed(as_of)
    # Build symbol → enriched OHLCV for the projector
    history_by_symbol = {
        h.tradingsymbol: data.load_ohlcv_enriched(h.tradingsymbol, tail=None)
        for h in typed_holdings
    }
    # Filter out empty histories
    history_by_symbol = {k: v for k, v in history_by_symbol.items() if not v.empty}
    try:
        projections = project_all_gtts(
            typed_gtts, history_by_symbol, horizon_days=20, n_paths=500, seed=42
        )
    except Exception as exc:  # defensive — bad data shouldn't crash the page
        st.error(f"Couldn't project GTTs: {exc}")
        projections = []

    if not projections:
        empty_state(
            "No GTTs could be projected",
            "All projections skipped (insufficient history or zero realised volatility).",
        )
    else:
        gtt_rows = []
        for p in projections:
            trigger = p.trigger_values[0] if p.trigger_values else None
            spot = p.last_price
            dist = (
                (trigger - spot) / spot * 100.0
                if (trigger is not None and spot)
                else None
            )
            gtt_rows.append(
                {
                    "Symbol": p.symbol,
                    "Trigger": trigger,
                    "Spot": spot,
                    "Distance %": dist,
                    "P(hit)": p.probability_hit * 100.0 if p.probability_hit is not None else None,
                    "Expected days": p.expected_days_to_hit,
                    "Note": p.note or "",
                }
            )
        gtt_df = pd.DataFrame(gtt_rows)
        st.dataframe(
            gtt_df,
            hide_index=True,
            width="stretch",
            column_config={
                "Trigger": st.column_config.NumberColumn(format="₹%.2f"),
                "Spot": st.column_config.NumberColumn(format="₹%.2f"),
                "Distance %": st.column_config.NumberColumn(format="%+.2f%%"),
                "P(hit)": st.column_config.ProgressColumn(
                    format="%.0f%%", min_value=0.0, max_value=100.0
                ),
                "Expected days": st.column_config.NumberColumn(format="%.1f"),
            },
        )

divider()

# ---------------------------------------------------------------------------
# Per-symbol drill-down
# ---------------------------------------------------------------------------

section_header("Per-symbol chart", "Pick a holding to inspect 180-day price action.")
symbols = [h["tradingsymbol"] for h in holdings]
pick = st.selectbox("Symbol", options=symbols, key="portfolio_symbol_pick")
if pick:
    hist = data.load_ohlcv_enriched(pick, tail=180)
    if hist.empty:
        empty_state(
            f"No parquet history for {pick}",
            f"Run <code>trading ingest-history --symbol {pick}</code> to backfill.",
        )
    else:
        st.plotly_chart(candlestick(hist, symbol=pick), width="stretch", key=f"port_candle_{pick}")
