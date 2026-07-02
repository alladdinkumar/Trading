"""Layer A — deterministic rule scanner (spec §4.1).

A `Candidate` is the output of running every rule against one symbol's
end-of-day bar. Rules are pure functions returning `RuleResult`. The
orchestrator never raises for "rule failed" — it raises only when a programmer
misuses the API (e.g. passes a DataFrame missing required columns).

Rules dependent on data that isn't ready yet (macro regime, F&O ban list,
critical sentiment events) live behind an opt-in `ScanContext`. When the
context is empty, those rules **pass** so the scanner is useful today and
tightens automatically once Phases 8/9 light up the missing inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from trading.config import Paths
from trading.features.technicals import add_indicators
from trading.store.ohlcv import list_symbols, read_ohlcv

MIN_HISTORY_BARS = 200  # need this many to compute sma_200

# Skip a symbol whose most recent bar is older than this many calendar days
# before scan_date (covers weekends + holiday clusters). Guards against scanning
# on silently-stale parquet when the daily OHLCV refresh failed or was skipped.
MAX_BAR_AGE_DAYS = 5

INSUFFICIENT_HISTORY = "insufficient history"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleResult:
    name: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class ScanContext:
    """Per-scan-run context. Symbol-specific rule inputs go here."""

    scan_date: date
    india_vix: float | None = None
    nifty200_drawdown_5d_pct: float | None = None
    fno_ban_symbols: frozenset[str] = field(default_factory=frozenset)
    t2t_symbols: frozenset[str] = field(default_factory=frozenset)
    critical_event_symbols: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    scan_date: date
    close: float
    rsi_14: float
    sma_20: float
    sma_50: float
    sma_200: float
    atr_14: float
    rules: tuple[RuleResult, ...]
    bar_date: date  # date of the bar the metrics were computed on (data basis)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.rules)


# ---------------------------------------------------------------------------
# Indicator-based rules (DataFrame must already be enriched by add_indicators)
# ---------------------------------------------------------------------------


def passes_uptrend(df: pd.DataFrame) -> RuleResult:
    """close > 200-DMA AND 50-DMA > 200-DMA."""
    last = df.iloc[-1]
    sma_50 = last.get("sma_50")
    sma_200 = last.get("sma_200")
    close = last["close"]
    if pd.isna(sma_50) or pd.isna(sma_200):
        return RuleResult("uptrend", False, INSUFFICIENT_HISTORY)
    if close > sma_200 and sma_50 > sma_200:
        return RuleResult("uptrend", True)
    return RuleResult(
        "uptrend",
        False,
        f"close={close:.2f} sma50={sma_50:.2f} sma200={sma_200:.2f}",
    )


def passes_pullback(df: pd.DataFrame, max_pct: float = 0.03, small_tol: float = 0.005) -> RuleResult:
    """sma*(1-max_pct) <= close <= sma*(1+small_tol), for sma_20 OR sma_50.

    Directional band (F-050): the old symmetric `|close - sma| <= max_pct`
    let names *extended above* their average pass as "pullbacks". A dip entry
    means price sitting at or below its short average, so only a small upside
    tolerance (`small_tol`) is allowed while the full `max_pct` floor still
    applies on the downside.
    """
    last = df.iloc[-1]
    close = last["close"]
    pct_from: list[tuple[str, float]] = []
    for col in ("sma_20", "sma_50"):
        val = last.get(col)
        if val is None or pd.isna(val):
            continue
        pct = (close - val) / val
        pct_from.append((col, pct))
        if -max_pct <= pct <= small_tol:
            return RuleResult("pullback", True, f"{pct * 100:+.1f}% from {col}")
    if not pct_from:
        return RuleResult("pullback", False, INSUFFICIENT_HISTORY)
    nearest_col, nearest_pct = min(pct_from, key=lambda t: abs(t[1]))
    return RuleResult(
        "pullback",
        False,
        f"{nearest_pct * 100:+.1f}% from {nearest_col} "
        f"(outside [-{max_pct * 100:.0f}%, +{small_tol * 100:.1f}%])",
    )


def passes_rsi_band(df: pd.DataFrame, low: float = 30, high: float = 45) -> RuleResult:
    """low ≤ RSI(14) ≤ high."""
    rsi_val = df["rsi_14"].iloc[-1]
    if pd.isna(rsi_val):
        return RuleResult("rsi_band", False, INSUFFICIENT_HISTORY)
    if low <= rsi_val <= high:
        return RuleResult("rsi_band", True, f"RSI={rsi_val:.1f}")
    return RuleResult("rsi_band", False, f"RSI={rsi_val:.1f} outside [{low}, {high}]")


def passes_volume_exhaustion(df: pd.DataFrame) -> RuleResult:
    """On a selling day (close < open), today's volume must be below the
    trailing 20-day average. On non-selling days, the rule trivially passes."""
    last = df.iloc[-1]
    if last["close"] >= last["open"]:
        return RuleResult("volume_exhaustion", True, "not a selling day")
    # 20 bars BEFORE today
    history = df["volume"].iloc[-21:-1]
    if len(history) < 20:
        return RuleResult("volume_exhaustion", False, INSUFFICIENT_HISTORY)
    avg_vol = history.mean()
    today_vol = last["volume"]
    if today_vol < avg_vol:
        return RuleResult(
            "volume_exhaustion", True, f"vol {today_vol:,.0f} < 20d avg {avg_vol:,.0f}"
        )
    return RuleResult("volume_exhaustion", False, f"vol {today_vol:,.0f} >= 20d avg {avg_vol:,.0f}")


def passes_liquidity(df: pd.DataFrame, min_turnover_cr: float = 10.0) -> RuleResult:
    """20-day average turnover (close × volume, in ₹) > min_turnover_cr × 1e7."""
    last_20 = df.iloc[-20:]
    if len(last_20) < 20:
        return RuleResult("liquidity", False, INSUFFICIENT_HISTORY)
    avg_turnover_inr = (last_20["close"] * last_20["volume"]).mean()
    avg_turnover_cr = avg_turnover_inr / 1e7
    if avg_turnover_cr >= min_turnover_cr:
        return RuleResult("liquidity", True, f"20d avg turnover ₹{avg_turnover_cr:.1f}cr")
    return RuleResult(
        "liquidity",
        False,
        f"20d avg turnover ₹{avg_turnover_cr:.1f}cr < ₹{min_turnover_cr}cr",
    )


def passes_no_recent_breakdown(
    df: pd.DataFrame, lookback: int = 30, max_drop: float = 0.15
) -> RuleResult:
    """close has not dropped more than `max_drop` from its `lookback`-bar high."""
    if len(df) < lookback + 1:
        return RuleResult("no_recent_breakdown", False, INSUFFICIENT_HISTORY)
    today_close = df["close"].iloc[-1]
    window_high = df["close"].iloc[-lookback - 1 : -1].max()
    drop = (today_close - window_high) / window_high
    if drop >= -max_drop:
        return RuleResult("no_recent_breakdown", True, f"{drop * 100:+.1f}% from {lookback}d high")
    return RuleResult(
        "no_recent_breakdown",
        False,
        f"down {abs(drop) * 100:.1f}% from {lookback}d high",
    )


# ---------------------------------------------------------------------------
# Context-based rules
# ---------------------------------------------------------------------------


def passes_market_filter(ctx: ScanContext) -> RuleResult:
    """Extreme-stress hard veto: fail iff India VIX ≥ 25 OR Nifty 200 5-day
    drawdown ≤ -5%.

    Named ``market_filter`` (F-020) to avoid colliding with
    :func:`trading.features.regime.classify_regime`, which is a *different*
    concept: a graded 4-axis RISK_ON/NEUTRAL/RISK_OFF voter (VIX 14/20 +
    futures/FII/USDINR) that drives a continuous position-size multiplier. The
    two deliberately use different thresholds for different jobs — the voter
    *scales* exposure in mild risk-off, this gate only *blocks* on extreme
    stress — so they are kept separate, not merged.
    """
    reasons: list[str] = []
    if ctx.india_vix is not None and ctx.india_vix >= 25:
        reasons.append(f"VIX={ctx.india_vix:.1f} >= 25")
    if ctx.nifty200_drawdown_5d_pct is not None and ctx.nifty200_drawdown_5d_pct <= -5:
        reasons.append(f"Nifty200 5d {ctx.nifty200_drawdown_5d_pct:.1f}% <= -5")
    if reasons:
        return RuleResult("market_filter", False, "; ".join(reasons))
    if ctx.india_vix is None and ctx.nifty200_drawdown_5d_pct is None:
        return RuleResult("market_filter", True, "no macro data — passing by default")
    return RuleResult("market_filter", True, "OK")


def passes_not_fno_banned(symbol: str, ctx: ScanContext) -> RuleResult:
    if symbol in ctx.fno_ban_symbols:
        return RuleResult("not_fno_banned", False, "in F&O ban list")
    return RuleResult("not_fno_banned", True)


def passes_not_t2t(symbol: str, ctx: ScanContext) -> RuleResult:
    if symbol in ctx.t2t_symbols:
        return RuleResult("not_t2t", False, "in T2T segment")
    return RuleResult("not_t2t", True)


def passes_no_critical_event(symbol: str, ctx: ScanContext) -> RuleResult:
    if symbol in ctx.critical_event_symbols:
        return RuleResult("no_critical_event", False, "critical sentiment event in last 30d")
    return RuleResult("no_critical_event", True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# Canonical Layer-A rule names, in the exact order evaluate_symbol emits them.
# signals.rules_passed_json persists only the *passed* names, so consumers (the
# dashboard's rule_chip_grid) reconstruct the full pass/fail grid against this
# tuple. Kept in sync with evaluate_symbol by
# test_layer_a_rule_names_match_evaluate_symbol.
LAYER_A_RULE_NAMES: tuple[str, ...] = (
    "uptrend",
    "pullback",
    "rsi_band",
    "volume_exhaustion",
    "liquidity",
    "no_recent_breakdown",
    "market_filter",
    "not_fno_banned",
    "not_t2t",
    "no_critical_event",
)


def _f(series_val: object) -> float:
    """Convert a (possibly NaN) value to plain float, NaN preserved."""
    try:
        return float(series_val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


def evaluate_symbol(symbol: str, df: pd.DataFrame, ctx: ScanContext) -> Candidate:
    """Run every rule on one symbol's enriched DataFrame and return a Candidate."""
    if len(df) == 0:
        raise ValueError(f"evaluate_symbol: empty DataFrame for {symbol}")
    rules: tuple[RuleResult, ...] = (
        passes_uptrend(df),
        passes_pullback(df),
        passes_rsi_band(df),
        passes_volume_exhaustion(df),
        passes_liquidity(df),
        passes_no_recent_breakdown(df),
        passes_market_filter(ctx),
        passes_not_fno_banned(symbol, ctx),
        passes_not_t2t(symbol, ctx),
        passes_no_critical_event(symbol, ctx),
    )
    last = df.iloc[-1]
    return Candidate(
        symbol=symbol,
        scan_date=ctx.scan_date,
        close=_f(last.get("close")),
        rsi_14=_f(last.get("rsi_14")),
        sma_20=_f(last.get("sma_20")),
        sma_50=_f(last.get("sma_50")),
        sma_200=_f(last.get("sma_200")),
        atr_14=_f(last.get("atr_14")),
        rules=rules,
        bar_date=pd.Timestamp(df.index[-1]).date(),
    )


def scan(
    paths: Paths,
    scan_date: date,
    *,
    symbols: list[str] | None = None,
    ctx: ScanContext | None = None,
    warnings: list[str] | None = None,
) -> list[Candidate]:
    """Run Layer A across the universe and return one Candidate per symbol.

    Symbols with fewer than `MIN_HISTORY_BARS` available bars are silently
    skipped (they can't compute SMA 200, so no rule can pass meaningfully).
    A symbol whose most recent bar is more than `MAX_BAR_AGE_DAYS` before
    `scan_date` is skipped with a warning appended to `warnings` (when given) —
    so a stale parquet degrades the run visibly instead of silently feeding the
    scanner month-old prices.
    """
    if ctx is None:
        ctx = ScanContext(scan_date=scan_date)
    if symbols is None:
        symbols = list_symbols(paths)

    candidates: list[Candidate] = []
    for sym in symbols:
        try:
            raw = read_ohlcv(sym, paths, end=scan_date)
        except FileNotFoundError:
            continue
        if len(raw) < MIN_HISTORY_BARS:
            continue
        last_bar = pd.Timestamp(raw.index[-1]).date()
        if (scan_date - last_bar).days > MAX_BAR_AGE_DAYS:
            if warnings is not None:
                warnings.append(f"{sym}: last bar {last_bar.isoformat()} is stale — skipped")
            continue
        enriched = add_indicators(raw)
        candidates.append(evaluate_symbol(sym, enriched, ctx))
    return candidates


def passing(candidates: list[Candidate]) -> list[Candidate]:
    """Filter to only the candidates that passed every rule."""
    return [c for c in candidates if c.all_passed]
