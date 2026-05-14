"""Macro snapshot fetchers — yfinance for global futures/indices + commodities
+ rates + India VIX, plus NSE FII/DII daily flows via nsepython.

Each fetcher is wrapped so a single failing source contributes None instead of
killing the snapshot. Matches the Phase 8 source-isolation pattern.

GIFT/SGX Nifty has no reliable yfinance ticker since the SGX→IFSC transition
in mid-2023, so `sgx_nifty` stays None until Phase 13 picks a feed; the
`macro_snapshot` schema already allows NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import yfinance as yf

if TYPE_CHECKING:
    from trading.features.regime import RegimeResult

# ---------------------------------------------------------------------------
# Tickers — yfinance symbols
# ---------------------------------------------------------------------------

# All free, no auth required. Note: ^DJI/^GSPC/^IXIC are the spot indices —
# we use them as proxies for "global equity direction overnight"; intraday
# US futures (YM=F, ES=F, NQ=F) are an alternative but the spot closes are
# enough for a daily snapshot taken pre-market in India.
YF_TICKERS: dict[str, str] = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "usdinr": "INR=X",
    "crude": "BZ=F",   # Brent
    "vix": "^INDIAVIX",
    "us_10y": "^TNX",
}


# ---------------------------------------------------------------------------
# Snapshot datatype — mirrors the macro_snapshot schema (spec §13)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroSnapshot:
    """One row of `macro_snapshot`. All fields nullable per schema."""

    date: str  # YYYY-MM-DD
    sgx_nifty: float | None
    dow_fut: float | None
    nasdaq_fut: float | None
    sp500: float | None
    usdinr: float | None
    crude: float | None
    vix: float | None
    us_10y: float | None
    fii_flow_cr: float | None
    dii_flow_cr: float | None
    regime: str | None  # 'RISK_ON' | 'NEUTRAL' | 'RISK_OFF' | None


@dataclass(frozen=True)
class YfQuote:
    """Latest close + 1-day % change for one yfinance ticker."""

    ticker: str
    close: float | None
    pct_change_1d: float | None


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_yf_quote(ticker: str, *, lookback_days: int = 10) -> YfQuote:
    """Return the most recent close + 1-day pct-change for `ticker`.

    Best-effort: any yfinance error (HTTP, rate-limit, deprecated symbol)
    yields `(None, None)` so the wider snapshot keeps going. We pull
    `lookback_days` bars instead of `period="2d"` because weekends and
    holidays can leave a 2-day window empty.
    """
    try:
        raw: Any = yf.download(
            ticker,
            period=f"{lookback_days}d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            actions=False,
        )
    except Exception:
        return YfQuote(ticker=ticker, close=None, pct_change_1d=None)

    if raw is None or getattr(raw, "empty", True):
        return YfQuote(ticker=ticker, close=None, pct_change_1d=None)
    # yfinance returns a MultiIndex for single-ticker calls in some versions;
    # flatten so we can index by 'Close'.
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    if "Close" not in raw.columns or len(raw) == 0:
        return YfQuote(ticker=ticker, close=None, pct_change_1d=None)

    closes = raw["Close"].dropna()
    if len(closes) == 0:
        return YfQuote(ticker=ticker, close=None, pct_change_1d=None)
    last = float(closes.iloc[-1])
    if len(closes) < 2:
        return YfQuote(ticker=ticker, close=last, pct_change_1d=None)
    prev = float(closes.iloc[-2])
    pct = (last - prev) / prev * 100.0 if prev != 0 else None
    return YfQuote(ticker=ticker, close=last, pct_change_1d=pct)


def fetch_all_yf_quotes() -> dict[str, YfQuote]:
    """Pull every yfinance ticker in `YF_TICKERS`. Returns name → quote."""
    return {name: fetch_yf_quote(t) for name, t in YF_TICKERS.items()}


# ---------------------------------------------------------------------------
# FII / DII flows (NSE)
# ---------------------------------------------------------------------------


def fetch_fii_dii() -> tuple[float | None, float | None]:
    """Pull the latest FII + DII net cash-market flows in ₹ crore.

    Wraps `nsepython.nse_fiidii`. NSE's endpoint returns rows keyed by
    `category` ('FII/FPI', 'DII', sometimes others); we sum net values per
    category and return `(fii_cr, dii_cr)`.

    Defensively coded: any failure (rate-limit, schema drift, network)
    yields `(None, None)` so the surrounding snapshot remains writeable.
    """
    try:
        from nsepython import nse_fiidii

        df = nse_fiidii(mode="pandas")
    except Exception:
        return None, None

    if df is None or df.empty:
        return None, None

    # The endpoint's column names have shifted historically; check both
    # the modern (`category`, `netValue`) and legacy (`type`, `netVal`) forms.
    cat_col = next((c for c in ("category", "Category", "type") if c in df.columns), None)
    val_col = next(
        (c for c in ("netValue", "netVal", "net", "Net Value") if c in df.columns), None
    )
    if cat_col is None or val_col is None:
        return None, None

    fii = _sum_for(df, cat_col, val_col, ("FII", "FPI", "FII/FPI"))
    dii = _sum_for(df, cat_col, val_col, ("DII",))
    return fii, dii


def _sum_for(df: Any, cat_col: str, val_col: str, needles: tuple[str, ...]) -> float | None:
    """Sum `val_col` over rows whose `cat_col` contains any of `needles`."""
    try:
        mask = df[cat_col].astype(str).str.upper().str.contains(
            "|".join(n.upper() for n in needles)
        )
        if not mask.any():
            return None
        return float(df.loc[mask, val_col].astype(float).sum())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Snapshot orchestrator
# ---------------------------------------------------------------------------


def build_snapshot(
    as_of: date,
    *,
    quotes: dict[str, YfQuote] | None = None,
    fii_dii: tuple[float | None, float | None] | None = None,
) -> MacroSnapshot:
    """Assemble a `MacroSnapshot` for `as_of`. Fetches inputs unless passed.

    The optional `quotes` / `fii_dii` params let the orchestrator (and
    tests) fetch once and re-use the data for both the snapshot row and
    the regime classifier — without those overrides, this function does
    its own fetching. The snapshot's `regime` field is always None;
    callers fill it after running the classifier.
    """
    q = quotes if quotes is not None else fetch_all_yf_quotes()
    fii_cr, dii_cr = fii_dii if fii_dii is not None else fetch_fii_dii()
    return MacroSnapshot(
        date=as_of.isoformat(),
        sgx_nifty=None,  # see module docstring — no reliable yf ticker
        dow_fut=q["dow"].close,
        nasdaq_fut=q["nasdaq"].close,
        sp500=q["sp500"].close,
        usdinr=q["usdinr"].close,
        crude=q["crude"].close,
        vix=q["vix"].close,
        us_10y=q["us_10y"].close,
        fii_flow_cr=fii_cr,
        dii_flow_cr=dii_cr,
        regime=None,
    )


def snapshot_and_classify(as_of: date) -> tuple[MacroSnapshot, "RegimeResult"]:
    """End-to-end pipeline: fetch once, build snapshot, classify regime.

    Returns the snapshot row with the regime bucket already filled in,
    plus the full `RegimeResult` (per-axis votes + reasons) so callers can
    surface the narrative into the daily briefing.
    """
    from trading.features.regime import classify_regime, regime_input_from_quotes

    quotes = fetch_all_yf_quotes()
    fii_cr, dii_cr = fetch_fii_dii()
    snap = build_snapshot(as_of, quotes=quotes, fii_dii=(fii_cr, dii_cr))
    regime_inp = regime_input_from_quotes(quotes, fii_cr)
    result = classify_regime(regime_inp)
    # Replace `regime` on the frozen dataclass via constructor:
    classified = MacroSnapshot(**{**snap.__dict__, "regime": result.regime})
    return classified, result
