"""Phase 15 — Plotly figure builders for the Streamlit dashboard.

Pure functions: each takes a DataFrame (or list of dicts) and returns a
``plotly.graph_objects.Figure``. No Streamlit imports — keeps them
unit-testable. Apply a shared dark theme via ``_apply_theme(fig)`` so the
whole dashboard has a consistent look.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Shared palette — must match components.py
COLOR_POSITIVE = "#26A69A"
COLOR_NEGATIVE = "#EF5350"
COLOR_NEUTRAL = "#FFB300"
COLOR_INFO = "#42A5F5"
COLOR_MUTED = "#9E9E9E"
COLOR_BG = "#0E1117"
COLOR_GRID = "#262730"


def _apply_theme(fig: go.Figure, *, height: int | None = None) -> go.Figure:
    """Apply the dashboard's shared dark theme + tight margins."""
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=COLOR_BG,
        plot_bgcolor=COLOR_BG,
        margin={"l": 40, "r": 20, "t": 30, "b": 30},
        font={"family": "Inter, ui-sans-serif, sans-serif", "size": 12},
        hovermode="x unified",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1,
            "bgcolor": "rgba(0,0,0,0)",
        },
    )
    fig.update_xaxes(gridcolor=COLOR_GRID, zeroline=False)
    fig.update_yaxes(gridcolor=COLOR_GRID, zeroline=False)
    if height is not None:
        fig.update_layout(height=height)
    return fig


def _empty_figure(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        showarrow=False,
        font={"color": COLOR_MUTED, "size": 14},
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
    )
    fig.update_layout(
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return _apply_theme(fig)


# ---------------------------------------------------------------------------
# Equity / drawdown
# ---------------------------------------------------------------------------


def equity_curve(snapshots: pd.DataFrame) -> go.Figure:
    """Equity over time with drawdown shaded on a secondary axis."""
    if snapshots is None or snapshots.empty:
        return _empty_figure("No portfolio snapshots yet")
    df = snapshots.sort_values("date")
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["equity"],
            mode="lines",
            name="Equity",
            line={"color": COLOR_INFO, "width": 2},
            fill="tozeroy",
            fillcolor="rgba(66,165,245,0.08)",
        ),
        secondary_y=False,
    )
    if "drawdown_pct" in df.columns and df["drawdown_pct"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["date"],
                y=df["drawdown_pct"],
                mode="lines",
                name="Drawdown %",
                line={"color": COLOR_NEGATIVE, "width": 1, "dash": "dot"},
            ),
            secondary_y=True,
        )
    fig.update_yaxes(title_text="Equity (₹)", secondary_y=False)
    fig.update_yaxes(
        title_text="Drawdown %",
        secondary_y=True,
        showgrid=False,
        tickformat=".1f",
    )
    return _apply_theme(fig, height=320)


def drawdown_curve(snapshots: pd.DataFrame) -> go.Figure:
    if snapshots is None or snapshots.empty:
        return _empty_figure("No drawdown history yet")
    df = snapshots.sort_values("date").copy()
    df["drawdown_pct"] = df["drawdown_pct"].fillna(0.0)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["drawdown_pct"],
            mode="lines",
            fill="tozeroy",
            line={"color": COLOR_NEGATIVE, "width": 1},
            fillcolor="rgba(239,83,80,0.20)",
            name="Drawdown %",
        )
    )
    fig.update_yaxes(title_text="Drawdown %", tickformat=".1f")
    return _apply_theme(fig, height=200)


# ---------------------------------------------------------------------------
# Candlestick + indicators
# ---------------------------------------------------------------------------


def candlestick(
    df: pd.DataFrame,
    *,
    symbol: str = "",
    show_sma: tuple[int, ...] = (20, 50, 200),
) -> go.Figure:
    """OHLC candlestick with optional SMA overlays + volume sub-plot."""
    if df is None or df.empty:
        return _empty_figure(f"No price history for {symbol}".strip())

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.78, 0.22],
        subplot_titles=("", ""),
    )

    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            increasing_line_color=COLOR_POSITIVE,
            decreasing_line_color=COLOR_NEGATIVE,
            name=symbol or "OHLC",
            showlegend=False,
        ),
        row=1,
        col=1,
    )

    sma_colors = [COLOR_NEUTRAL, COLOR_INFO, COLOR_MUTED]
    for window, color in zip(show_sma, sma_colors, strict=False):
        col = f"sma_{window}"
        if col in df.columns and df[col].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=df.index,
                    y=df[col],
                    mode="lines",
                    name=f"SMA {window}",
                    line={"color": color, "width": 1},
                ),
                row=1,
                col=1,
            )

    if "volume" in df.columns:
        vol_colors = [
            COLOR_POSITIVE if c >= o else COLOR_NEGATIVE
            for c, o in zip(df["close"], df["open"], strict=False)
        ]
        fig.add_trace(
            go.Bar(
                x=df.index,
                y=df["volume"],
                marker_color=vol_colors,
                opacity=0.5,
                name="Volume",
                showlegend=False,
            ),
            row=2,
            col=1,
        )

    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_yaxes(title_text="Price (₹)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1, showgrid=False)
    return _apply_theme(fig, height=480)


# ---------------------------------------------------------------------------
# Sector / concentration
# ---------------------------------------------------------------------------


def sector_pie(rows: list[dict[str, Any]], *, value_key: str = "value") -> go.Figure:
    """Donut chart of sector concentration. `rows` ~= [{label, value}]."""
    if not rows:
        return _empty_figure("No sector breakdown")
    labels = [r["label"] for r in rows]
    values = [r[value_key] for r in rows]
    fig = go.Figure(
        go.Pie(
            labels=labels,
            values=values,
            hole=0.55,
            marker={
                "colors": [
                    COLOR_INFO,
                    COLOR_POSITIVE,
                    COLOR_NEUTRAL,
                    "#AB47BC",
                    "#FF7043",
                    "#26C6DA",
                    COLOR_NEGATIVE,
                    COLOR_MUTED,
                ]
            },
            textinfo="label+percent",
            textposition="outside",
        )
    )
    fig.update_layout(showlegend=False)
    return _apply_theme(fig, height=320)


# ---------------------------------------------------------------------------
# Paper trade metrics
# ---------------------------------------------------------------------------


def pnl_distribution(trades: pd.DataFrame) -> go.Figure:
    """Histogram of closed-trade P&L%."""
    if trades is None or trades.empty or "pnl_pct" not in trades.columns:
        return _empty_figure("No closed trades yet")
    closed = trades.dropna(subset=["pnl_pct"])
    if closed.empty:
        return _empty_figure("No closed trades yet")
    fig = go.Figure(
        go.Histogram(
            x=closed["pnl_pct"],
            nbinsx=20,
            marker_color=COLOR_INFO,
            opacity=0.8,
        )
    )
    fig.add_vline(x=0, line_color=COLOR_MUTED, line_dash="dash")
    fig.update_xaxes(title_text="P&L %")
    fig.update_yaxes(title_text="Trades")
    return _apply_theme(fig, height=260)


def win_loss_donut(trades: pd.DataFrame) -> go.Figure:
    """Donut of WIN / LOSS / BREAKEVEN counts from closed trades."""
    if trades is None or trades.empty or "pnl" not in trades.columns:
        return _empty_figure("No closed trades yet")
    closed = trades.dropna(subset=["pnl"])
    if closed.empty:
        return _empty_figure("No closed trades yet")
    wins = int((closed["pnl"] > 0).sum())
    losses = int((closed["pnl"] < 0).sum())
    flats = int((closed["pnl"] == 0).sum())
    fig = go.Figure(
        go.Pie(
            labels=["Wins", "Losses", "Flat"],
            values=[wins, losses, flats],
            hole=0.55,
            marker={"colors": [COLOR_POSITIVE, COLOR_NEGATIVE, COLOR_MUTED]},
            textinfo="label+value",
        )
    )
    fig.update_layout(showlegend=False)
    return _apply_theme(fig, height=260)


def prediction_calibration(predictions: pd.DataFrame) -> go.Figure:
    """Scatter of predicted vs actual return for matured predictions."""
    if predictions is None or predictions.empty:
        return _empty_figure("No matured predictions yet")
    matured = predictions.dropna(subset=["actual_return_at_horizon"])
    if matured.empty:
        return _empty_figure("No matured predictions yet")
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=matured["predicted_return_pct"],
            y=matured["actual_return_at_horizon"],
            mode="markers",
            marker={
                "size": 8,
                "color": matured["actual_return_at_horizon"],
                "colorscale": [
                    [0, COLOR_NEGATIVE],
                    [0.5, COLOR_MUTED],
                    [1, COLOR_POSITIVE],
                ],
                "line": {"color": COLOR_BG, "width": 1},
            },
            text=matured["symbol"],
            hovertemplate="%{text}<br>pred %{x:.2f}%<br>actual %{y:.2f}%<extra></extra>",
        )
    )
    # y = x reference
    lo = min(matured["predicted_return_pct"].min(), matured["actual_return_at_horizon"].min())
    hi = max(matured["predicted_return_pct"].max(), matured["actual_return_at_horizon"].max())
    fig.add_trace(
        go.Scatter(
            x=[lo, hi],
            y=[lo, hi],
            mode="lines",
            line={"color": COLOR_MUTED, "dash": "dash", "width": 1},
            name="y = x",
            showlegend=False,
        )
    )
    fig.update_xaxes(title_text="Predicted return %")
    fig.update_yaxes(title_text="Actual return %")
    return _apply_theme(fig, height=320)


# ---------------------------------------------------------------------------
# Regime history
# ---------------------------------------------------------------------------


def regime_history(macro: pd.DataFrame) -> go.Figure:
    """Step plot of regime over time (RISK_ON=+1, NEUTRAL=0, RISK_OFF=-1)."""
    if macro is None or macro.empty:
        return _empty_figure("No regime history yet")
    code_map = {"RISK_ON": 1, "NEUTRAL": 0, "RISK_OFF": -1}
    df = macro.copy()
    df["code"] = df["regime"].map(code_map).fillna(0)
    fig = go.Figure(
        go.Scatter(
            x=df["date"],
            y=df["code"],
            mode="lines+markers",
            line={"shape": "hv", "color": COLOR_INFO, "width": 2},
            marker={"size": 6, "color": COLOR_INFO},
            name="Regime",
        )
    )
    fig.update_yaxes(
        tickvals=[-1, 0, 1],
        ticktext=["RISK_OFF", "NEUTRAL", "RISK_ON"],
        range=[-1.3, 1.3],
    )
    return _apply_theme(fig, height=180)
