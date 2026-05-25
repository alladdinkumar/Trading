"""Phase 16 feature builders for the LightGBM ranker.

Pure functions only — no I/O. Caller supplies the enriched OHLCV slice,
the signal date, and a `LiveContext` with macro / sentiment slots already
fetched. `FEATURE_NAMES` is the single source of truth for column order;
training and inference both iterate it to assemble the matrix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from trading.data.macro import MacroSnapshot
from trading.store.news_store import SentimentDailyRow

FEATURE_NAMES: tuple[str, ...] = (
    # setup
    "rsi_14",
    "pullback_pct_20",
    "pullback_pct_50",
    "atr_pct",
    "dist_from_52w_high",
    # trend
    "sma_20_slope_5d",
    "sma_50_slope_10d",
    "sma_200_slope_20d",
    "adx_14",
    "dist_from_52w_low",
    # volume
    "volume_vs_20d_avg",
    "obv_slope_5d",
    # macro
    "vix",
    "vix_change_5d",
    "fii_flow_5d_sum",
    "usdinr_change_5d",
    "regime_ord",
    # sentiment
    "sentiment_7d",
    "sentiment_30d",
    "negative_news_count_7d",
)


@dataclass(frozen=True)
class LiveContext:
    """Per-candidate live context. Any slot may be None → NaN for those features.

    `macro_history` is a DataFrame indexed by ISO date with `vix`, `usdinr`,
    `fii_flow_cr` columns. Used to compute 5-day changes / sums centred on
    `as_of`. When unavailable, the macro change/sum features fall back to NaN.

    `negative_news_count_7d` is a precomputed scalar so this module stays
    DB-free; the caller (training loop / inference) pulls it from
    `news_items` once.
    """

    macro: MacroSnapshot | None = None
    sentiment: SentimentDailyRow | None = None
    macro_history: pd.DataFrame | None = None
    negative_news_count_7d: int | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_pct_change(series: pd.Series, periods: int) -> float:
    """Pct change of the most recent value vs `periods` ago. NaN-safe."""
    if len(series) <= periods:
        return math.nan
    a = float(series.iloc[-periods - 1])
    b = float(series.iloc[-1])
    if math.isnan(a) or math.isnan(b) or a == 0:
        return math.nan
    return (b - a) / a


def _f(value: object) -> float:
    """Convert to float, NaN on failure."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def _setup_features(df: pd.DataFrame, sd: pd.Timestamp) -> dict[str, float]:
    last = df.loc[sd]
    close = _f(last.get("close"))
    sma_20 = _f(last.get("sma_20"))
    sma_50 = _f(last.get("sma_50"))
    atr_14 = _f(last.get("atr_14"))
    rsi_14 = _f(last.get("rsi_14"))

    high_252 = float(df["close"].rolling(252, min_periods=1).max().loc[sd])
    pullback_20 = (close - sma_20) / sma_20 if sma_20 and not math.isnan(sma_20) else math.nan
    pullback_50 = (close - sma_50) / sma_50 if sma_50 and not math.isnan(sma_50) else math.nan
    atr_pct = atr_14 / close if close and not math.isnan(atr_14) else math.nan
    dist_high = (close - high_252) / high_252 if high_252 else math.nan
    return {
        "rsi_14": rsi_14,
        "pullback_pct_20": pullback_20,
        "pullback_pct_50": pullback_50,
        "atr_pct": atr_pct,
        "dist_from_52w_high": dist_high,
    }


def _trend_features(df: pd.DataFrame, sd: pd.Timestamp) -> dict[str, float]:
    until = df.loc[:sd]
    sma_20 = until["sma_20"].dropna() if "sma_20" in until.columns else pd.Series([], dtype=float)
    sma_50 = until["sma_50"].dropna() if "sma_50" in until.columns else pd.Series([], dtype=float)
    sma_200 = until["sma_200"].dropna() if "sma_200" in until.columns else pd.Series([], dtype=float)
    adx_series = until["adx_14"] if "adx_14" in until.columns else pd.Series([], dtype=float)
    adx_val = _f(adx_series.iloc[-1]) if len(adx_series) else math.nan

    low_252 = float(until["close"].rolling(252, min_periods=1).min().loc[sd])
    close = _f(until.at[sd, "close"])
    dist_low = (close - low_252) / low_252 if low_252 else math.nan

    return {
        "sma_20_slope_5d": _safe_pct_change(sma_20, 5) if len(sma_20) else math.nan,
        "sma_50_slope_10d": _safe_pct_change(sma_50, 10) if len(sma_50) else math.nan,
        "sma_200_slope_20d": _safe_pct_change(sma_200, 20) if len(sma_200) else math.nan,
        "adx_14": adx_val,
        "dist_from_52w_low": dist_low,
    }


_REGIME_ORD: dict[str, int] = {"RISK_OFF": 0, "NEUTRAL": 1, "RISK_ON": 2}


def _macro_features(live_ctx: LiveContext, sd: pd.Timestamp) -> dict[str, float]:
    out: dict[str, float] = {
        "vix": math.nan,
        "vix_change_5d": math.nan,
        "fii_flow_5d_sum": math.nan,
        "usdinr_change_5d": math.nan,
        "regime_ord": math.nan,
    }
    snap = live_ctx.macro
    if snap is not None:
        if snap.vix is not None:
            out["vix"] = float(snap.vix)
        if snap.regime is not None and snap.regime in _REGIME_ORD:
            out["regime_ord"] = float(_REGIME_ORD[snap.regime])

    hist = live_ctx.macro_history
    if hist is not None and len(hist) >= 5:
        sd_iso = sd.strftime("%Y-%m-%d")
        # Locate the rightmost row whose index ≤ sd_iso. Falls back to the last
        # row when sd_iso isn't present.
        try:
            end_pos = int(hist.index.get_loc(sd_iso))
        except KeyError:
            mask = hist.index <= sd_iso
            if not mask.any():
                return out
            end_pos = int(mask.sum() - 1)
        if end_pos >= 4:
            window = hist.iloc[end_pos - 4 : end_pos + 1]  # 5 rows ending at sd
            if "vix" in window.columns:
                vix_now = window["vix"].iloc[-1]
                vix_5d_ago = window["vix"].iloc[0]
                if pd.notna(vix_now) and pd.notna(vix_5d_ago) and vix_5d_ago != 0:
                    out["vix_change_5d"] = float((vix_now - vix_5d_ago) / vix_5d_ago)
            if "usdinr" in window.columns:
                u_now = window["usdinr"].iloc[-1]
                u_5d_ago = window["usdinr"].iloc[0]
                if pd.notna(u_now) and pd.notna(u_5d_ago) and u_5d_ago != 0:
                    out["usdinr_change_5d"] = float((u_now - u_5d_ago) / u_5d_ago)
            if "fii_flow_cr" in window.columns and window["fii_flow_cr"].notna().any():
                out["fii_flow_5d_sum"] = float(window["fii_flow_cr"].sum(skipna=True))
    return out


def _sentiment_features(live_ctx: LiveContext) -> dict[str, float]:
    out: dict[str, float] = {
        "sentiment_7d": math.nan,
        "sentiment_30d": math.nan,
        "negative_news_count_7d": math.nan,
    }
    sent = live_ctx.sentiment
    if sent is not None:
        if sent.score_7d is not None:
            out["sentiment_7d"] = float(sent.score_7d)
        if sent.score_30d is not None:
            out["sentiment_30d"] = float(sent.score_30d)
    if live_ctx.negative_news_count_7d is not None:
        out["negative_news_count_7d"] = float(live_ctx.negative_news_count_7d)
    return out


def _volume_features(df: pd.DataFrame, sd: pd.Timestamp) -> dict[str, float]:
    until = df.loc[:sd]
    vol_today = _f(until.at[sd, "volume"])
    vol_history = until["volume"].iloc[-21:-1]  # 20 bars before sd
    avg = float(vol_history.mean()) if len(vol_history) >= 5 else math.nan
    ratio = vol_today / avg if avg and not math.isnan(avg) else math.nan

    obv = until["obv"] if "obv" in until.columns else None
    if obv is not None:
        obv_clean = obv.dropna()
        obv_slope = _safe_pct_change(obv_clean, 5) if len(obv_clean) else math.nan
    else:
        obv_slope = math.nan
    return {
        "volume_vs_20d_avg": ratio,
        "obv_slope_5d": obv_slope,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_feature_row(
    enriched_df: pd.DataFrame,
    signal_date: pd.Timestamp,
    live_ctx: LiveContext,
) -> dict[str, float]:
    """Build a single feature row keyed by FEATURE_NAMES.

    Returns a dict with NaN for any feature whose inputs are missing.
    Caller stacks rows into a DataFrame using FEATURE_NAMES as column order.
    """
    if signal_date not in enriched_df.index:
        raise KeyError(f"signal_date {signal_date} not in enriched_df.index")
    row: dict[str, float] = {}
    row.update(_setup_features(enriched_df, signal_date))
    row.update(_trend_features(enriched_df, signal_date))
    row.update(_volume_features(enriched_df, signal_date))
    row.update(_macro_features(live_ctx, signal_date))
    row.update(_sentiment_features(live_ctx))
    assert set(row.keys()) == set(FEATURE_NAMES), (
        f"build_feature_row mismatch: extra={set(row) - set(FEATURE_NAMES)}, "
        f"missing={set(FEATURE_NAMES) - set(row)}"
    )
    return row
