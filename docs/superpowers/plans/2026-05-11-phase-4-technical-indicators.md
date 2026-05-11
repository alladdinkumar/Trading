# Phase 4 — Technical Indicators Implementation Plan

**Goal:** A small, focused indicator library — RSI, MACD, SMA, EMA, Bollinger Bands, ATR, ADX, VWAP, OBV, simple returns — plus `add_indicators(df)` that decorates an OHLCV DataFrame with all defaults. These functions are the building blocks Phase 5 (rule scanner) consumes.

**Architecture:** Module-level functions over the `ta` library (replaces `pandas-ta` from spec — see Phase 0 commit notes). Each function takes a `pd.Series` (or several, for OHLC indicators) and returns a `pd.Series`. `add_indicators` is the only function that mutates a DataFrame — and even then, it returns a copy.

**Tech Stack:** `ta>=0.11.0` (already in Phase 0 deps), `pandas`, `numpy`.

**Reference:** Spec Section 4.1 (rule filters that consume these indicators), Section 11 (`src/trading/features/technicals.py`).

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/trading/features/technicals.py` | All indicator functions + `add_indicators` |
| Create | `tests/test_technicals.py` | Indicator behavior tests |
| Modify | `PROGRESS.md` | Tick 4.1-4.4 |

---

## API

```python
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
SMA_DEFAULT_PERIODS = (20, 50, 200)
EMA_DEFAULT_PERIODS = (20, 50)
BB_PERIOD, BB_STDDEV = 20, 2
ATR_PERIOD = 14
ADX_PERIOD = 14
VWAP_PERIOD = 14

def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series: ...
def macd(close: pd.Series, ...) -> tuple[pd.Series, pd.Series, pd.Series]: ...
def sma(close: pd.Series, period: int) -> pd.Series: ...
def ema(close: pd.Series, period: int) -> pd.Series: ...
def bollinger_bands(close, period, n_std) -> tuple[upper, middle, lower]: ...
def atr(high, low, close, period) -> pd.Series: ...
def adx(high, low, close, period) -> pd.Series: ...
def vwap(high, low, close, volume, period) -> pd.Series: ...   # rolling VWAP
def obv(close, volume) -> pd.Series: ...
def returns(close: pd.Series, periods: int = 1) -> pd.Series:
    return close.pct_change(periods=periods)

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df enriched with all default indicators."""
```

**Columns produced by `add_indicators`:** `rsi_14`, `macd`, `macd_signal`, `macd_hist`, `sma_20`, `sma_50`, `sma_200`, `ema_20`, `ema_50`, `bb_upper`, `bb_middle`, `bb_lower`, `atr_14`, `adx_14`, `vwap_14`, `obv`, `returns_1d`.

**Lookback / NaN behavior:** Each indicator's first `period-1` values are `NaN` (no fill). Downstream callers filter.

## Tests

`tests/test_technicals.py`:

- **Shape & NaN pattern** for each indicator: input length preserved, first `period-1` NaN, rest finite
- **SMA exact value**: 5-day SMA of `[1..10]` at index 4 → 3.0
- **Returns exact value**: `pct_change(1)` of `[100, 110, 99]` → `[NaN, 0.1, -0.1]`
- **RSI bounds**: monotonic-up series → RSI > 70; monotonic-down → RSI < 30
- **Bollinger bands ordering**: upper > middle > lower at every non-NaN row
- **ATR non-negative** always
- **MACD signal lag**: signal line equals EMA-9 of the MACD line
- **add_indicators**: all expected columns present, original `open/high/low/close/volume` preserved

## Tasks

1. Write `tests/test_technicals.py` (failing).
2. Implement `src/trading/features/technicals.py`.
3. Run lint / type / tests → green.
4. PROGRESS.md → commit.
