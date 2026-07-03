"""Phase 15 — Paper Journal page.

Track record: open + closed trades, equity curve, hit rate / profit factor /
Sharpe, win-loss donut, prediction calibration scatter.
"""

from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from trading.clock import today_ist
from trading.paper.journal import deviation_label, expected_target_date
from trading.ui import data
from trading.ui.charts import (
    equity_curve,
    pnl_distribution,
    prediction_calibration,
    win_loss_donut,
)
from trading.ui.components import (
    divider,
    empty_state,
    format_currency,
    kpi_tile,
    section_header,
    sidebar_date_picker,
)

st.set_page_config(page_title="Paper Journal · Trading", page_icon="📒", layout="wide")

st.sidebar.title("📒 Paper Journal")
_ = sidebar_date_picker("As-of date")  # currently unused — kept for sidebar consistency

st.markdown("## Paper Journal")
st.caption("All paper trades since inception. Read-only.")
divider()

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

trades = data.load_paper_trades()
snapshots = data.load_portfolio_snapshots()
predictions = data.load_predictions()

open_trades = trades[trades["ts_exit"].isna()] if not trades.empty else pd.DataFrame()
closed_trades = trades[trades["ts_exit"].notna()] if not trades.empty else pd.DataFrame()

# ---------------------------------------------------------------------------
# Metric tiles
# ---------------------------------------------------------------------------


def _hit_rate(df: pd.DataFrame) -> float | None:
    if df.empty:
        return None
    wins = int((df["pnl"] > 0).sum())
    return wins / len(df) * 100.0


def _profit_factor(df: pd.DataFrame) -> float | None:
    if df.empty:
        return None
    pos = df.loc[df["pnl"] > 0, "pnl"].sum()
    neg = -df.loc[df["pnl"] < 0, "pnl"].sum()
    if neg == 0:
        return float("inf") if pos > 0 else None
    return float(pos / neg)


def _sharpe(df: pd.DataFrame) -> float | None:
    if df.empty or "pnl_pct" not in df.columns:
        return None
    series = df["pnl_pct"].dropna()
    if len(series) < 2 or series.std(ddof=1) == 0:
        return None
    # Per-trade Sharpe (not annualised) — enough for a "are we in the black" tile
    return float(series.mean() / series.std(ddof=1))


def _expectancy(df: pd.DataFrame) -> float | None:
    if df.empty or "pnl" not in df.columns:
        return None
    return float(df["pnl"].mean())


def _schedule_cols(df: pd.DataFrame, *, closed: bool) -> pd.DataFrame:
    """Add Bought / Target date / Deviation columns from ts_entry + horizon_days."""
    out = df.copy()
    today = today_ist()
    boughts, targets, deviations = [], [], []
    for _, row in out.iterrows():
        entry_iso = str(row["ts_entry"])
        horizon = int(row["horizon_days"]) if pd.notna(row.get("horizon_days")) else 0
        target = expected_target_date(entry_iso, horizon)
        exit_iso = str(row["ts_exit"]) if closed and pd.notna(row.get("ts_exit")) else None
        boughts.append(entry_iso[:10])
        targets.append(target.isoformat())
        deviations.append(deviation_label(target, exit_iso=exit_iso, as_of=today))
    out["Bought"] = boughts
    out["Target date"] = targets
    out["Deviation"] = deviations
    return out


k1, k2, k3, k4, k5 = st.columns(5)
with k1:
    kpi_tile("Closed trades", str(len(closed_trades)))
with k2:
    hr = _hit_rate(closed_trades)
    kpi_tile("Hit rate", f"{hr:.1f}%" if hr is not None else "—")
with k3:
    pf = _profit_factor(closed_trades)
    kpi_tile(
        "Profit factor",
        "∞" if pf is not None and math.isinf(pf) else (f"{pf:.2f}" if pf is not None else "—"),
    )
with k4:
    sr = _sharpe(closed_trades)
    kpi_tile("Sharpe (per-trade)", f"{sr:.2f}" if sr is not None else "—")
with k5:
    exp = _expectancy(closed_trades)
    kpi_tile("Expectancy", format_currency(exp))

divider()

# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

section_header("Equity curve (full history)")
if snapshots.empty:
    empty_state(
        "No portfolio snapshots yet",
        "Run <code>trading post-close --apply &lt;date&gt;</code> after each session.",
    )
else:
    st.plotly_chart(equity_curve(snapshots), width="stretch", key="pj_equity_curve")

divider()

# ---------------------------------------------------------------------------
# Open trades
# ---------------------------------------------------------------------------

section_header("Open trades")
if open_trades.empty:
    empty_state("No open paper trades")
else:
    show = open_trades.copy()
    if "ts_entry" in show.columns:
        show["entry_date"] = show["ts_entry"].str[:10]
    show = _schedule_cols(show, closed=False)
    cols = [
        "entry_date",
        "symbol",
        "side",
        "qty",
        "entry_price",
        "current_stop",
        "target",
        "Bought",
        "Target date",
        "Deviation",
        "days_held",
    ]
    cols = [c for c in cols if c in show.columns]
    st.dataframe(
        show[cols],
        hide_index=True,
        width="stretch",
        column_config={
            "entry_price": st.column_config.NumberColumn(format="₹%.2f"),
            "current_stop": st.column_config.NumberColumn(format="₹%.2f"),
            "target": st.column_config.NumberColumn(format="₹%.2f"),
        },
    )

divider()

# ---------------------------------------------------------------------------
# Closed trades
# ---------------------------------------------------------------------------

section_header("Closed trades")
if closed_trades.empty:
    empty_state(
        "No closed trades yet",
        "After a paper trade hits stop/target/time, "
        "<code>trading post-close --apply</code> writes the exit row here.",
    )
else:
    show = closed_trades.copy()
    if "ts_entry" in show.columns:
        show["entry"] = show["ts_entry"].str[:10]
    if "ts_exit" in show.columns:
        show["exit"] = show["ts_exit"].str[:10]
    show = _schedule_cols(show, closed=True)
    cols = [
        "entry",
        "exit",
        "symbol",
        "side",
        "qty",
        "entry_price",
        "exit_price",
        "pnl",
        "pnl_pct",
        "exit_reason",
        "Bought",
        "Target date",
        "Deviation",
        "days_held",
    ]
    cols = [c for c in cols if c in show.columns]
    st.dataframe(
        show[cols],
        hide_index=True,
        width="stretch",
        column_config={
            "entry_price": st.column_config.NumberColumn(format="₹%.2f"),
            "exit_price": st.column_config.NumberColumn(format="₹%.2f"),
            "pnl": st.column_config.NumberColumn(format="₹%.0f"),
            "pnl_pct": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )

divider()

# ---------------------------------------------------------------------------
# Win/loss + P&L distribution
# ---------------------------------------------------------------------------

c1, c2 = st.columns(2)
with c1:
    section_header("Win / Loss")
    st.plotly_chart(win_loss_donut(closed_trades), width="stretch", key="pj_winloss")
with c2:
    section_header("P&L distribution")
    st.plotly_chart(pnl_distribution(closed_trades), width="stretch", key="pj_pnldist")

divider()

# ---------------------------------------------------------------------------
# Prediction calibration
# ---------------------------------------------------------------------------

section_header(
    "Prediction calibration",
    "Each dot = one matured prediction (actual vs predicted return).",
)
st.plotly_chart(prediction_calibration(predictions), width="stretch", key="pj_calibration")
