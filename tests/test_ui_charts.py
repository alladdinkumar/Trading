"""Unit tests for trading.ui.charts — pure Plotly figure builders.

Each builder takes a DataFrame/list and returns a plotly Figure. We assert
shape (at least one trace, axes present, theme applied) rather than pixel
output — visual verification happens via Playwright on the live app.
"""

from __future__ import annotations

import pandas as pd

from trading.ui.charts import (
    _apply_theme,
    candlestick,
    drawdown_curve,
    equity_curve,
    pnl_distribution,
    prediction_calibration,
    regime_history,
    sector_pie,
    win_loss_donut,
)


def test_apply_theme_sets_dark_template_and_bg():
    import plotly.graph_objects as go

    fig = _apply_theme(go.Figure())
    assert fig.layout.template.layout.colorway is not None
    assert fig.layout.paper_bgcolor == "#0E1117"
    assert fig.layout.plot_bgcolor == "#0E1117"


# ---------------------------------------------------------------------------
# Equity / drawdown
# ---------------------------------------------------------------------------


def _snapshots_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-20", "2026-05-21", "2026-05-22"]),
            "cash": [100000.0, 100100.0, 99800.0],
            "equity": [100000.0, 100250.0, 100050.0],
            "drawdown_pct": [0.0, 0.0, -0.20],
        }
    )


def test_equity_curve_with_data_has_traces():
    fig = equity_curve(_snapshots_df())
    assert len(fig.data) >= 1
    # The equity trace should be a Scatter line with Y matching equity values
    eq_trace = fig.data[0]
    assert eq_trace.type == "scatter"
    assert list(eq_trace.y) == [100000.0, 100250.0, 100050.0]


def test_equity_curve_empty_renders_empty_state():
    fig = equity_curve(pd.DataFrame())
    # Empty-state figure has zero data traces but exactly one annotation
    assert len(fig.data) == 0
    assert any("No portfolio snapshots" in (a.text or "") for a in fig.layout.annotations)


def test_drawdown_curve_handles_nan_drawdowns():
    df = _snapshots_df()
    df.loc[df.index[0], "drawdown_pct"] = float("nan")
    fig = drawdown_curve(df)
    assert len(fig.data) == 1
    assert fig.data[0].fill == "tozeroy"


def test_drawdown_curve_empty():
    fig = drawdown_curve(pd.DataFrame())
    assert len(fig.data) == 0


# ---------------------------------------------------------------------------
# Candlestick
# ---------------------------------------------------------------------------


def _ohlcv_df() -> pd.DataFrame:
    idx = pd.date_range("2026-05-01", periods=10, freq="B")
    df = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 102, 104, 105, 106, 105, 107],
            "high": [101, 102, 103, 104, 103, 105, 106, 107, 106, 108],
            "low": [99, 100, 101, 102, 101, 103, 104, 105, 104, 106],
            "close": [100.5, 101.5, 102.5, 103.5, 102.5, 104.5, 105.5, 106.5, 105.5, 107.5],
            "volume": [1000, 1100, 1200, 1300, 1100, 1400, 1500, 1600, 1500, 1700],
            "sma_20": [100.0] * 10,
            "sma_50": [99.5] * 10,
        },
        index=idx,
    )
    return df


def test_candlestick_includes_ohlc_and_volume():
    fig = candlestick(_ohlcv_df(), symbol="TEST")
    # First trace candlestick + SMAs + volume bar = ≥ 3 traces
    assert len(fig.data) >= 3
    assert fig.data[0].type == "candlestick"
    types = [t.type for t in fig.data]
    assert "bar" in types  # volume sub-plot


def test_candlestick_empty():
    fig = candlestick(pd.DataFrame(), symbol="ABCD")
    assert len(fig.data) == 0
    assert any("ABCD" in (a.text or "") for a in fig.layout.annotations)


# ---------------------------------------------------------------------------
# Sector pie
# ---------------------------------------------------------------------------


def test_sector_pie_basic():
    rows = [
        {"label": "Energy", "value": 50000.0},
        {"label": "IT", "value": 30000.0},
        {"label": "Banks", "value": 20000.0},
    ]
    fig = sector_pie(rows)
    assert len(fig.data) == 1
    assert fig.data[0].type == "pie"
    assert list(fig.data[0].labels) == ["Energy", "IT", "Banks"]


def test_sector_pie_empty():
    fig = sector_pie([])
    assert len(fig.data) == 0


# ---------------------------------------------------------------------------
# Paper trade charts
# ---------------------------------------------------------------------------


def _trades_df(n_wins: int = 3, n_losses: int = 2) -> pd.DataFrame:
    pnl_pcts = [1.5, 2.0, 0.8][:n_wins] + [-1.0, -2.5][:n_losses]
    pnls = [100, 200, 50][:n_wins] + [-80, -200][:n_losses]
    return pd.DataFrame(
        {
            "pnl_pct": pnl_pcts,
            "pnl": pnls,
            "symbol": [f"SYM{i}" for i in range(n_wins + n_losses)],
        }
    )


def test_pnl_distribution_histogram():
    fig = pnl_distribution(_trades_df())
    assert len(fig.data) == 1
    assert fig.data[0].type == "histogram"


def test_pnl_distribution_empty():
    fig = pnl_distribution(pd.DataFrame())
    assert len(fig.data) == 0


def test_win_loss_donut_counts():
    fig = win_loss_donut(_trades_df(n_wins=3, n_losses=2))
    assert len(fig.data) == 1
    assert fig.data[0].type == "pie"
    # Values: wins, losses, flat
    assert list(fig.data[0].values) == [3, 2, 0]


def test_win_loss_donut_empty():
    fig = win_loss_donut(pd.DataFrame())
    assert len(fig.data) == 0


# ---------------------------------------------------------------------------
# Prediction calibration
# ---------------------------------------------------------------------------


def test_prediction_calibration_with_data():
    df = pd.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "predicted_return_pct": [5.0, -2.0, 1.5],
            "actual_return_at_horizon": [4.0, -1.0, 2.0],
        }
    )
    fig = prediction_calibration(df)
    # Scatter trace + y=x reference line
    assert len(fig.data) == 2


def test_prediction_calibration_handles_all_unmatured():
    df = pd.DataFrame(
        {
            "symbol": ["A"],
            "predicted_return_pct": [5.0],
            "actual_return_at_horizon": [None],
        }
    )
    fig = prediction_calibration(df)
    assert len(fig.data) == 0


def test_prediction_calibration_empty():
    fig = prediction_calibration(pd.DataFrame())
    assert len(fig.data) == 0


# ---------------------------------------------------------------------------
# Regime history
# ---------------------------------------------------------------------------


def test_regime_history_step_plot():
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-20", "2026-05-21", "2026-05-22"]),
            "regime": ["RISK_OFF", "NEUTRAL", "RISK_ON"],
        }
    )
    fig = regime_history(df)
    assert len(fig.data) == 1
    assert list(fig.data[0].y) == [-1, 0, 1]


def test_regime_history_empty():
    fig = regime_history(pd.DataFrame())
    assert len(fig.data) == 0
