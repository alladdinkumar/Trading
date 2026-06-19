"""Paper Portfolio — Kite-style dashboard for the paper-trading book.

Per-symbol holdings (qty/avg/invested/LTP/current/P&L/today's P&L), an
invested/current/P&L summary, and a separately-tracked funds panel (initial
₹1L + top-ups). Marks are offline — the latest portfolio snapshot's close —
so the page is labelled "as of last close". The Add-funds widget is the one
intentional UI writer; everything else is read-only.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from trading.config import get_paths
from trading.paper.funds import add_funds
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.ui import data
from trading.ui.components import (
    divider,
    empty_state,
    format_currency,
    format_pct,
    kpi_tile,
    section_header,
)

st.set_page_config(page_title="Paper Portfolio · Trading", page_icon="🧪", layout="wide")
st.sidebar.title("🧪 Paper Portfolio")

st.markdown("## Paper Portfolio")
as_of = date.today().isoformat()
summary = data.load_paper_summary(as_of)

if summary.as_of_mark:
    st.caption(f"Paper book · marks as of last close ({summary.as_of_mark}).")
else:
    st.caption("Paper book · no snapshots yet — values shown at cost.")
divider()

# ---------------------------------------------------------------------------
# Summary tiles
# ---------------------------------------------------------------------------

k1, k2, k3, k4, k5, k6 = st.columns(6)
with k1:
    kpi_tile("Invested", format_currency(summary.invested))
with k2:
    kpi_tile("Current value", format_currency(summary.current_value))
with k3:
    kpi_tile(
        "Total P&L",
        format_currency(summary.total_pnl),
        delta=format_pct(summary.total_pnl_pct),
        delta_color="normal",
    )
with k4:
    kpi_tile("Today's P&L", format_currency(summary.today_pnl))
with k5:
    kpi_tile("Cash available", format_currency(summary.cash))
with k6:
    kpi_tile("Account value", format_currency(summary.account_value))

divider()

# ---------------------------------------------------------------------------
# Holdings table
# ---------------------------------------------------------------------------

section_header("Holdings")
positions = data.load_paper_positions(as_of)
if positions.empty:
    empty_state(
        "No open paper positions",
        "Open a paper trade with <code>trading paper-open</code>; it appears here once filled.",
    )
else:
    show = positions.rename(
        columns={
            "symbol": "Symbol",
            "qty": "Qty",
            "avg": "Avg",
            "invested": "Invested",
            "ltp": "LTP",
            "current_value": "Current",
            "pnl": "P&L ₹",
            "pnl_pct": "P&L %",
            "today_pnl": "Today's P&L ₹",
        }
    )
    st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        column_config={
            "Avg": st.column_config.NumberColumn(format="₹%.2f"),
            "Invested": st.column_config.NumberColumn(format="₹%.0f"),
            "LTP": st.column_config.NumberColumn(format="₹%.2f"),
            "Current": st.column_config.NumberColumn(format="₹%.0f"),
            "P&L ₹": st.column_config.NumberColumn(format="₹%.0f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
            "Today's P&L ₹": st.column_config.NumberColumn(format="₹%.0f"),
        },
    )

divider()

# ---------------------------------------------------------------------------
# Funds panel + Add-funds widget
# ---------------------------------------------------------------------------

section_header("Funds")
ledger = data.load_cash_ledger()
total_in = 100_000.0 + summary.funds_added

f1, f2 = st.columns([2, 1])
with f1:
    st.write(f"**Initial capital** {format_currency(100_000.0)}")
    if ledger.empty:
        st.caption("No top-ups recorded yet.")
    else:
        st.dataframe(
            ledger.rename(columns={"date": "Date", "amount": "Amount ₹", "note": "Note"}),
            hide_index=True,
            width="stretch",
            column_config={"Amount ₹": st.column_config.NumberColumn(format="₹%.0f")},
        )
    st.write(f"**Total funds in** {format_currency(total_in)}")
    st.write(f"**Cash available** {format_currency(summary.cash)}")

with f2:
    st.markdown("**Add funds**")
    amount = st.number_input("Amount (₹)", min_value=0.0, step=1000.0, value=0.0)
    note = st.text_input("Note (optional)")
    if st.button("Add funds", type="primary"):
        if amount <= 0:
            st.error("Amount must be positive.")
        else:
            paths = get_paths()
            with get_conn(paths.db_path) as conn:
                run_migrations(conn)
                add_funds(conn, amount=amount, date=date.today().isoformat(), note=note or None)
            st.cache_data.clear()
            st.success(f"Added {format_currency(amount)}.")
            st.rerun()
