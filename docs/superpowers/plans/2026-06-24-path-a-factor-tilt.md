# Path A — OHLCV Cross-Sectional Factor Tilt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a transparent two-factor cross-sectional composite (12-1 momentum + low realized volatility) that ranks the universe each day, plus an offline evaluation harness that proves whether it has edge — before any live trading behavior changes.

**Architecture:** A pure, point-in-time factor module (`strategy/factors.py`, mirroring the purity of `ranking/ranker_features.py`) computes per-symbol factors and a cross-sectional z-score composite. A read-only evaluation module (`backtest/factor_eval.py`) measures Information Coefficient and runs a factor-gated vs rules-only per-trade backtest, reusing the existing `ranker_labels.realized_return` for cost/exit consistency. A `trading factor-eval` Typer CLI command drives it over on-disk parquet. Phase 2 (wiring the eligible set into `pre_open` behind a config flag) is **gated on Phase 1 producing positive evidence** and is the final, conditional task.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), pandas, numpy, Typer CLI, pytest. No new third-party dependency — Spearman correlation uses `pandas.Series.corr(method="spearman")`.

## Global Constraints

- Python 3.9+ compatible; every module starts with `from __future__ import annotations`. (verbatim from codebase convention)
- `src/trading/strategy/factors.py` is **pure — no I/O, no DB, no file reads**. It receives data in memory, exactly like `ranking/ranker_features.py`.
- All factor functions are **point-in-time**: a factor at date `t` uses only bars with index ≤ `t`. Appending future bars must not change a past value.
- Per-trade Sharpe uses `periods_per_year=12` (monthly-equivalent), matching the F-045 OOS convention in `ranking/ranker_train.py`.
- Locked parameter defaults: `vol_window=90`, `top_quantile=0.30`, momentum window `close[t-21]/close[t-252]-1` with a 273-bar minimum, forward-return horizon `max_days=25`.
- Linting/format/type gates must stay green: `ruff check .`, `ruff format .`, `mypy src/`.
- Commit after each task. Do not push (the user pushes per-phase). Work stays on branch `factor-tilt-path-a`.
- Reuse `trading.ranking.ranker_labels.realized_return` for forward returns — do not reimplement exit logic.

---

### Task 1: `momentum_12_1` factor

**Files:**
- Create: `src/trading/strategy/factors.py`
- Test: `tests/test_factors.py`

**Interfaces:**
- Consumes: a per-symbol OHLCV/enriched `pd.DataFrame` indexed by `pd.Timestamp` with a `close` column.
- Produces: `momentum_12_1(df: pd.DataFrame, as_of: pd.Timestamp) -> float | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factors.py
from __future__ import annotations

import numpy as np
import pandas as pd

from trading.strategy.factors import momentum_12_1


def _close_series(values: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range("2020-01-01", periods=len(values))
    return pd.DataFrame({"close": values}, index=idx)


def test_momentum_12_1_is_close_t21_over_close_t252() -> None:
    # 300 bars so as_of has 273+ bars of history through it.
    closes = list(np.linspace(100.0, 400.0, 300))
    df = _close_series(closes)
    as_of = df.index[-1]
    pos = df.index.get_loc(as_of)
    expected = closes[pos - 21] / closes[pos - 252] - 1.0
    assert momentum_12_1(df, as_of) == expected


def test_momentum_12_1_none_when_fewer_than_273_bars() -> None:
    df = _close_series(list(np.linspace(100.0, 200.0, 272)))
    assert momentum_12_1(df, df.index[-1]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -v`
Expected: FAIL with `ImportError: cannot import name 'momentum_12_1'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/strategy/factors.py
"""Path A — pure, point-in-time cross-sectional factor builders.

No I/O. Mirrors the purity of ``ranking/ranker_features.py``: the caller
supplies in-memory OHLCV frames and an ``as_of`` date. Every factor at date
``t`` uses only bars with index <= ``t`` (no look-ahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MOMENTUM_MIN_BARS = 273  # 252-day lookback + 21-day skip buffer
MOMENTUM_SKIP = 21
MOMENTUM_LOOKBACK = 252


def momentum_12_1(df: pd.DataFrame, as_of: pd.Timestamp) -> float | None:
    """12-1 momentum: ``close[t-21] / close[t-252] - 1``.

    Skips the most recent ~21 trading days to avoid short-term reversal.
    Returns ``None`` when fewer than ``MOMENTUM_MIN_BARS`` bars are available
    at/through ``as_of``.
    """
    if as_of not in df.index:
        return None
    until = df.loc[:as_of]
    if len(until) < MOMENTUM_MIN_BARS:
        return None
    recent = float(until["close"].iloc[-MOMENTUM_SKIP - 1])
    base = float(until["close"].iloc[-MOMENTUM_LOOKBACK - 1])
    if base == 0:
        return None
    return recent / base - 1.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/trading/strategy/factors.py tests/test_factors.py
git commit -m "feat(factors): 12-1 momentum factor (Path A)"
```

---

### Task 2: `realized_vol` factor

**Files:**
- Modify: `src/trading/strategy/factors.py`
- Test: `tests/test_factors.py`

**Interfaces:**
- Produces: `realized_vol(df: pd.DataFrame, as_of: pd.Timestamp, *, window: int = 90) -> float | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factors.py (append)
from trading.strategy.factors import realized_vol


def test_realized_vol_constant_growth_is_near_zero() -> None:
    # Constant 1%/bar growth → constant log return → ~0 stdev.
    closes = [100.0 * (1.01 ** i) for i in range(120)]
    df = _close_series(closes)
    vol = realized_vol(df, df.index[-1], window=90)
    assert vol is not None
    assert vol < 1e-9


def test_realized_vol_matches_sample_stdev_of_log_returns() -> None:
    rng = np.random.default_rng(0)
    closes = list(100.0 * np.cumprod(1 + rng.normal(0, 0.02, size=200)))
    df = _close_series(closes)
    as_of = df.index[-1]
    log_ret = np.diff(np.log(df["close"].to_numpy()))
    expected = float(np.std(log_ret[-90:], ddof=1))
    assert realized_vol(df, as_of, window=90) == expected


def test_realized_vol_none_when_insufficient_history() -> None:
    df = _close_series([100.0] * 90)  # need window+1=91 bars
    assert realized_vol(df, df.index[-1], window=90) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -k realized_vol -v`
Expected: FAIL with `ImportError: cannot import name 'realized_vol'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/strategy/factors.py (append)
def realized_vol(df: pd.DataFrame, as_of: pd.Timestamp, *, window: int = 90) -> float | None:
    """Sample stdev (ddof=1) of trailing daily log returns over ``window`` bars.

    Returns ``None`` when fewer than ``window + 1`` bars are available
    at/through ``as_of`` (need window+1 prices for window returns).
    """
    if as_of not in df.index:
        return None
    until = df.loc[:as_of]
    if len(until) < window + 1:
        return None
    log_ret = np.diff(np.log(until["close"].to_numpy()))
    return float(np.std(log_ret[-window:], ddof=1))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -k realized_vol -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/trading/strategy/factors.py tests/test_factors.py
git commit -m "feat(factors): trailing realized-volatility factor (Path A)"
```

---

### Task 3: `factor_score` cross-sectional composite

**Files:**
- Modify: `src/trading/strategy/factors.py`
- Test: `tests/test_factors.py`

**Interfaces:**
- Consumes: `momentum_12_1`, `realized_vol` (Task 1, 2).
- Produces: `factor_score(panel: Mapping[str, pd.DataFrame], as_of: pd.Timestamp, *, vol_window: int = 90) -> dict[str, float]` — symbol → composite score (mean of momentum z-score and low-vol z-score `-z_vol`). Symbols with either factor `None` are absent from the result. Returns `{}` when fewer than 2 symbols survive.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factors.py (append)
from trading.strategy.factors import factor_score


def _trending_panel() -> dict[str, pd.DataFrame]:
    """3 symbols, 300 bars, differing drift+noise so momentum/vol differ."""
    idx = pd.bdate_range("2020-01-01", periods=300)
    panel: dict[str, pd.DataFrame] = {}
    for i, (drift, vol) in enumerate([(0.30, 0.005), (0.10, 0.02), (0.50, 0.05)]):
        rng = np.random.default_rng(i)
        steps = drift / 300 + rng.normal(0, vol, size=300)
        closes = 100.0 * np.cumprod(1 + steps)
        panel[f"SYM{i}"] = pd.DataFrame({"close": closes}, index=idx)
    return panel


def test_factor_score_is_zero_mean_unit_population_std_composite() -> None:
    panel = _trending_panel()
    as_of = panel["SYM0"].index[-1]
    scores = factor_score(panel, as_of, vol_window=90)
    assert set(scores) == {"SYM0", "SYM1", "SYM2"}
    # Composite is the mean of two z-scores, each population-standardized.
    vals = np.array(list(scores.values()))
    assert abs(float(vals.mean())) < 1e-9


def test_factor_score_drops_symbols_with_insufficient_history() -> None:
    panel = _trending_panel()
    short = pd.DataFrame(
        {"close": [100.0] * 50},
        index=pd.bdate_range("2020-01-01", periods=50),
    )
    panel["SHORT"] = short
    as_of = panel["SYM0"].index[-1]
    scores = factor_score(panel, as_of, vol_window=90)
    assert "SHORT" not in scores


def test_factor_score_rewards_low_vol_and_high_momentum() -> None:
    panel = _trending_panel()
    as_of = panel["SYM0"].index[-1]
    scores = factor_score(panel, as_of, vol_window=90)
    # SYM0 has the lowest vol; its low-vol z-score is the highest of the three.
    from trading.strategy.factors import realized_vol

    vols = {s: realized_vol(df, as_of, window=90) for s, df in panel.items()}
    lowest_vol = min(vols, key=lambda s: vols[s])  # type: ignore[arg-type]
    # The lowest-vol symbol gets a positive low-vol contribution → above-mean.
    assert scores[lowest_vol] > float(np.mean(list(scores.values())))


def test_factor_score_empty_when_fewer_than_two_survive() -> None:
    one = {"ONLY": _trending_panel()["SYM0"]}
    as_of = one["ONLY"].index[-1]
    assert factor_score(one, as_of, vol_window=90) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -k factor_score -v`
Expected: FAIL with `ImportError: cannot import name 'factor_score'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/strategy/factors.py (append imports + function)
from collections.abc import Mapping


def _zscore(values: dict[str, float]) -> dict[str, float]:
    """Population z-score (ddof=0). Zero-stdev cross-section → all zeros."""
    arr = np.array(list(values.values()), dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=0))
    if std < 1e-12:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / std for k, v in values.items()}


def factor_score(
    panel: Mapping[str, pd.DataFrame],
    as_of: pd.Timestamp,
    *,
    vol_window: int = 90,
) -> dict[str, float]:
    """Cross-sectional equal-weight composite of 12-1 momentum and low vol.

    For each symbol compute both factors point-in-time; drop any symbol whose
    either factor is ``None``. Z-score each factor across the survivors
    (momentum: higher is better; volatility negated so lower is better).
    Composite = mean(z_momentum, z_lowvol). Returns ``{}`` if < 2 survive.
    """
    mom: dict[str, float] = {}
    vol: dict[str, float] = {}
    for sym, df in panel.items():
        m = momentum_12_1(df, as_of)
        v = realized_vol(df, as_of, window=vol_window)
        if m is None or v is None:
            continue
        mom[sym] = m
        vol[sym] = v
    if len(mom) < 2:
        return {}
    z_mom = _zscore(mom)
    z_vol = _zscore(vol)
    return {sym: (z_mom[sym] + (-z_vol[sym])) / 2.0 for sym in mom}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -k factor_score -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/trading/strategy/factors.py tests/test_factors.py
git commit -m "feat(factors): cross-sectional z-score composite (Path A)"
```

---

### Task 4: `eligible_set` top-quantile selection

**Files:**
- Modify: `src/trading/strategy/factors.py`
- Test: `tests/test_factors.py`

**Interfaces:**
- Consumes: a `Mapping[str, float]` of composite scores (output of `factor_score`).
- Produces: `eligible_set(scores: Mapping[str, float], *, top_quantile: float = 0.30) -> set[str]` — the top `top_quantile` of symbols by score; ties broken by score then symbol; always returns ≥1 name for a non-empty input.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factors.py (append)
from trading.strategy.factors import eligible_set


def test_eligible_set_keeps_top_quantile_count() -> None:
    scores = {f"S{i}": float(i) for i in range(10)}  # S9 highest
    chosen = eligible_set(scores, top_quantile=0.30)  # 10 * 0.30 = 3
    assert chosen == {"S9", "S8", "S7"}


def test_eligible_set_ties_broken_deterministically_by_symbol() -> None:
    scores = {"B": 1.0, "A": 1.0, "C": 0.0}  # A and B tie at the top
    chosen = eligible_set(scores, top_quantile=0.34)  # 3 * 0.34 = 1 → keep 1
    assert chosen == {"A"}  # tie resolved by symbol ascending


def test_eligible_set_empty_input_returns_empty() -> None:
    assert eligible_set({}, top_quantile=0.30) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -k eligible_set -v`
Expected: FAIL with `ImportError: cannot import name 'eligible_set'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/strategy/factors.py (append)
def eligible_set(
    scores: Mapping[str, float],
    *,
    top_quantile: float = 0.30,
) -> set[str]:
    """Top ``top_quantile`` of symbols by composite score.

    Count = ``max(1, int(n * top_quantile))`` for non-empty input. Ties broken
    deterministically: higher score first, then symbol ascending.
    """
    if not scores:
        return set()
    n = len(scores)
    k = max(1, int(n * top_quantile))
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return {sym for sym, _ in ordered[:k]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -k eligible_set -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/trading/strategy/factors.py tests/test_factors.py
git commit -m "feat(factors): top-quantile eligible set (Path A)"
```

---

### Task 5: Point-in-time correctness guard

**Files:**
- Test: `tests/test_factors.py`

**Interfaces:**
- Consumes: `momentum_12_1`, `realized_vol`, `factor_score` (Tasks 1-3). No new production code — this task locks the no-look-ahead invariant the spec requires.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factors.py (append)
def test_factors_are_point_in_time_unchanged_by_future_bars() -> None:
    panel = _trending_panel()
    as_of = panel["SYM0"].index[200]  # a date with future bars after it

    before_mom = momentum_12_1(panel["SYM0"], as_of)
    before_vol = realized_vol(panel["SYM0"], as_of, window=90)
    before_scores = factor_score(panel, as_of, vol_window=90)

    # Append wildly different future bars to every symbol.
    future_idx = pd.bdate_range(panel["SYM0"].index[-1] + pd.Timedelta(days=1), periods=30)
    mutated = {
        s: pd.concat([df, pd.DataFrame({"close": [df["close"].iloc[-1] * 5] * 30}, index=future_idx)])
        for s, df in panel.items()
    }

    assert momentum_12_1(mutated["SYM0"], as_of) == before_mom
    assert realized_vol(mutated["SYM0"], as_of, window=90) == before_vol
    assert factor_score(mutated, as_of, vol_window=90) == before_scores
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factors.py -k point_in_time -v`
Expected: PASS immediately (the implementations already slice `df.loc[:as_of]`). If it FAILS, a factor is leaking future data — fix the offending factor to slice on `as_of` before proceeding. This guard test is the deliverable; document that it passed because of the `.loc[:as_of]` discipline.

- [ ] **Step 3: Commit**

```bash
git add tests/test_factors.py
git commit -m "test(factors): point-in-time no-look-ahead guard (Path A)"
```

---

### Task 6: Information Coefficient evaluation

**Files:**
- Create: `src/trading/backtest/factor_eval.py`
- Test: `tests/test_factor_eval.py`

**Interfaces:**
- Consumes: `trading.strategy.factors.factor_score`, `trading.ranking.ranker_labels.realized_return`, `trading.backtest.metrics.sharpe`.
- Produces:
  - `spearman_ic(scores: Mapping[str, float], fwd: Mapping[str, float], *, min_names: int = 5) -> float | None`
  - `@dataclass(frozen=True) ICResult` with fields `mean_ic: float`, `ic_std: float`, `ic_t_stat: float`, `hit_rate_positive_days: float`, `n_days: int`.
  - `aggregate_ic(ic_values: Sequence[float]) -> ICResult`
  - `forward_returns(panel: Mapping[str, pd.DataFrame], as_of: pd.Timestamp, *, max_days: int = 25) -> dict[str, float]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factor_eval.py
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from trading.backtest.factor_eval import (
    aggregate_ic,
    forward_returns,
    spearman_ic,
)


def test_spearman_ic_perfect_rank_is_one() -> None:
    scores = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.0, "E": -1.0}
    fwd = {"A": 0.10, "B": 0.08, "C": 0.05, "D": 0.01, "E": -0.02}
    assert spearman_ic(scores, fwd, min_names=5) == 1.0


def test_spearman_ic_inverted_rank_is_minus_one() -> None:
    scores = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.0, "E": -1.0}
    fwd = {"A": -0.10, "B": -0.08, "C": -0.05, "D": -0.01, "E": 0.02}
    assert spearman_ic(scores, fwd, min_names=5) == -1.0


def test_spearman_ic_none_when_too_few_overlapping_names() -> None:
    assert spearman_ic({"A": 1.0}, {"A": 0.1}, min_names=5) is None


def test_aggregate_ic_computes_tstat_and_hit_rate() -> None:
    ics = [0.1, 0.2, -0.05, 0.15]
    res = aggregate_ic(ics)
    assert res.n_days == 4
    assert res.mean_ic == np.mean(ics)
    expected_t = float(np.mean(ics)) / (float(np.std(ics, ddof=1)) / math.sqrt(4))
    assert res.ic_t_stat == expected_t
    assert res.hit_rate_positive_days == 0.75


def test_forward_returns_matches_realized_return() -> None:
    from trading.ranking.ranker_labels import realized_return
    from trading.features.technicals import add_indicators

    rng = np.random.default_rng(1)
    closes = 100.0 * np.cumprod(1 + rng.normal(0.001, 0.02, size=320))
    idx = pd.bdate_range("2020-01-01", periods=320)
    raw = pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99,
         "close": closes, "volume": [1_000_000] * 320},
        index=idx,
    )
    df = add_indicators(raw)
    panel = {"SYM": df}
    as_of = df.index[250]
    fwd = forward_returns(panel, as_of, max_days=25)
    assert fwd["SYM"] == realized_return(df, as_of, max_days=25)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factor_eval.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading.backtest.factor_eval'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/backtest/factor_eval.py
"""Path A — offline, read-only factor evaluation.

Measures whether the cross-sectional factor composite has edge:
Information Coefficient (Spearman corr of score vs forward return) and a
factor-gated vs rules-only per-trade backtest. Forward returns reuse
``ranker_labels.realized_return`` so they resolve exactly as trades would.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading.ranking.ranker_labels import realized_return


def spearman_ic(
    scores: Mapping[str, float],
    fwd: Mapping[str, float],
    *,
    min_names: int = 5,
) -> float | None:
    """Spearman rank correlation between scores and forward returns.

    Uses only symbols present in both maps. Returns ``None`` when fewer than
    ``min_names`` overlap (too thin a cross-section to be meaningful).
    """
    common = sorted(set(scores) & set(fwd))
    if len(common) < min_names:
        return None
    s = pd.Series([scores[c] for c in common])
    f = pd.Series([fwd[c] for c in common])
    ic = s.corr(f, method="spearman")
    return None if pd.isna(ic) else float(ic)


@dataclass(frozen=True)
class ICResult:
    mean_ic: float
    ic_std: float
    ic_t_stat: float
    hit_rate_positive_days: float
    n_days: int


def aggregate_ic(ic_values: Sequence[float]) -> ICResult:
    """Aggregate per-day ICs into mean, stdev, t-stat and positive-day rate."""
    n = len(ic_values)
    if n == 0:
        return ICResult(0.0, 0.0, 0.0, 0.0, 0)
    arr = np.array(ic_values, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    t_stat = mean / (std / math.sqrt(n)) if std > 1e-12 else 0.0
    hit = float((arr > 0).mean())
    return ICResult(mean, std, t_stat, hit, n)


def forward_returns(
    panel: Mapping[str, pd.DataFrame],
    as_of: pd.Timestamp,
    *,
    max_days: int = 25,
) -> dict[str, float]:
    """Realized forward return per symbol from ``as_of`` (None entries dropped)."""
    out: dict[str, float] = {}
    for sym, df in panel.items():
        r = realized_return(df, as_of, max_days=max_days)
        if r is not None:
            out[sym] = r
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factor_eval.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/trading/backtest/factor_eval.py tests/test_factor_eval.py
git commit -m "feat(factor-eval): IC computation + forward returns (Path A)"
```

---

### Task 7: Factor-gated vs rules-only per-trade comparison

**Files:**
- Modify: `src/trading/backtest/factor_eval.py`
- Test: `tests/test_factor_eval.py`

**Interfaces:**
- Consumes: `factor_score`, `eligible_set` (factors), `forward_returns` (Task 6), `realized_return`, `trading.strategy.rules.{evaluate_symbol, ScanContext, MIN_HISTORY_BARS}`, `trading.backtest.metrics.sharpe`.
- Produces:
  - `@dataclass(frozen=True) TradeMetrics` with `n: int`, `sharpe: float`, `profit_factor: float`, `hit_rate: float`, `payoff: float`.
  - `per_trade_metrics(returns: Sequence[float]) -> TradeMetrics`
  - `@dataclass(frozen=True) GatedComparison` with `baseline: TradeMetrics`, `gated: TradeMetrics`.
  - `factor_gated_metrics(panel, *, start, end, top_quantile=0.30, vol_window=90, max_days=25) -> GatedComparison`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factor_eval.py (append)
from trading.backtest.factor_eval import per_trade_metrics


def test_per_trade_metrics_on_known_returns() -> None:
    from trading.backtest.metrics import sharpe as _sharpe

    rets = [0.06, -0.04, 0.05, -0.03, 0.07]
    m = per_trade_metrics(rets)
    assert m.n == 5
    assert m.hit_rate == 3 / 5
    assert m.profit_factor == (0.06 + 0.05 + 0.07) / (0.04 + 0.03)
    assert m.payoff == ((0.06 + 0.05 + 0.07) / 3) / ((0.04 + 0.03) / 2)
    assert m.sharpe == _sharpe(pd.Series(rets), periods_per_year=12)


def test_per_trade_metrics_empty_is_zeroed() -> None:
    m = per_trade_metrics([])
    assert m.n == 0
    assert m.sharpe == 0.0
    assert m.profit_factor == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factor_eval.py -k per_trade_metrics -v`
Expected: FAIL with `ImportError: cannot import name 'per_trade_metrics'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/backtest/factor_eval.py (append)
from trading.backtest.metrics import sharpe as _sharpe
from trading.strategy.factors import eligible_set, factor_score
from trading.strategy.rules import MIN_HISTORY_BARS, ScanContext, evaluate_symbol


@dataclass(frozen=True)
class TradeMetrics:
    n: int
    sharpe: float
    profit_factor: float
    hit_rate: float
    payoff: float


def per_trade_metrics(returns: Sequence[float]) -> TradeMetrics:
    """Honest per-trade metrics over fractional realized returns."""
    n = len(returns)
    if n == 0:
        return TradeMetrics(0, 0.0, 0.0, 0.0, 0.0)
    arr = np.array(returns, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else (math.inf if avg_win > 0 else 0.0)
    return TradeMetrics(
        n=n,
        sharpe=_sharpe(pd.Series(arr), periods_per_year=12),
        profit_factor=pf,
        hit_rate=float((arr > 0).mean()),
        payoff=payoff,
    )


@dataclass(frozen=True)
class GatedComparison:
    baseline: TradeMetrics
    gated: TradeMetrics


def factor_gated_metrics(
    panel: Mapping[str, pd.DataFrame],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    top_quantile: float = 0.30,
    vol_window: int = 90,
    max_days: int = 25,
) -> GatedComparison:
    """Per-trade metrics for every rules-passing candidate (baseline) vs only
    those also in that day's factor eligible set (gated), over [start, end]."""
    baseline: list[float] = []
    gated: list[float] = []
    # Iterate the union of trading dates within the window.
    all_dates = sorted({d for df in panel.values() for d in df.index if start <= d <= end})
    for sd in all_dates:
        scores = factor_score(panel, sd, vol_window=vol_window)
        eligible = eligible_set(scores, top_quantile=top_quantile)
        for sym, df in panel.items():
            if sd not in df.index:
                continue
            sub = df.loc[:sd]
            if len(sub) < MIN_HISTORY_BARS:
                continue
            if not evaluate_symbol(sym, sub, ScanContext(scan_date=sd.date())).all_passed:
                continue
            r = realized_return(df, sd, max_days=max_days)
            if r is None:
                continue
            baseline.append(r)
            if sym in eligible:
                gated.append(r)
    return GatedComparison(baseline=per_trade_metrics(baseline), gated=per_trade_metrics(gated))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factor_eval.py -k per_trade_metrics -v`
Expected: PASS (2 passed). Then run the whole file: `PYTHONUTF8=1 uv run pytest tests/test_factor_eval.py -v` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trading/backtest/factor_eval.py tests/test_factor_eval.py
git commit -m "feat(factor-eval): factor-gated vs rules-only per-trade metrics (Path A)"
```

---

### Task 8: `trading factor-eval` CLI command

**Files:**
- Modify: `src/trading/cli.py`
- Test: `tests/test_cli_factor_eval.py`

**Interfaces:**
- Consumes: `information_coefficient` (add in this task), `factor_gated_metrics`, `list_symbols`, `read_ohlcv`, `add_indicators`, `get_paths`.
- Produces: a Typer command `factor-eval` with options `--start`, `--end`, `--top-quantile`, `--vol-window`. Also adds the orchestrator `information_coefficient(panel, *, start, end, vol_window=90, max_days=25, min_names=5) -> ICResult` to `factor_eval.py` (consumed by the CLI).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factor_eval.py (append) — the orchestrator
def test_information_coefficient_runs_over_a_window() -> None:
    from trading.backtest.factor_eval import information_coefficient
    from trading.features.technicals import add_indicators

    idx = pd.bdate_range("2020-01-01", periods=320)
    panel = {}
    for i in range(6):
        rng = np.random.default_rng(i)
        closes = 100.0 * np.cumprod(1 + rng.normal(0.0005 * i, 0.015, size=320))
        raw = pd.DataFrame(
            {"open": closes, "high": closes * 1.01, "low": closes * 0.99,
             "close": closes, "volume": [1_000_000] * 320},
            index=idx,
        )
        panel[f"SYM{i}"] = add_indicators(raw)
    res = information_coefficient(
        panel, start=idx[273], end=idx[280], vol_window=90, max_days=25, min_names=3
    )
    assert res.n_days >= 1
    assert isinstance(res.mean_ic, float)
```

```python
# tests/test_cli_factor_eval.py
from __future__ import annotations

from typer.testing import CliRunner

from trading.cli import app

runner = CliRunner()


def test_factor_eval_errors_cleanly_with_no_data(tmp_path, monkeypatch) -> None:
    # Point the CLI at an empty data dir → no parquet → exit 1, no traceback.
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["factor-eval", "--start", "2023-01-01"])
    assert result.exit_code == 1
    assert "No parquet" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factor_eval.py -k information_coefficient tests/test_cli_factor_eval.py -v`
Expected: FAIL — `cannot import name 'information_coefficient'` and no `factor-eval` command.

> Note: confirm the data-dir env var name. Grep `get_paths` / `Paths` in `src/trading/config.py`; if the override env var differs from `TRADING_DATA_DIR`, use the real name in the test. If `get_paths` is not env-overridable, instead `monkeypatch.setattr("trading.cli.get_paths", lambda: Paths(data_dir=tmp_path, ...))` matching the real `Paths` constructor.

- [ ] **Step 3: Write minimal implementation**

```python
# src/trading/backtest/factor_eval.py (append the orchestrator)
def information_coefficient(
    panel: Mapping[str, pd.DataFrame],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    vol_window: int = 90,
    max_days: int = 25,
    min_names: int = 5,
) -> ICResult:
    """Mean/stdev/t-stat of per-day Spearman IC of composite vs forward return."""
    all_dates = sorted({d for df in panel.values() for d in df.index if start <= d <= end})
    ics: list[float] = []
    for sd in all_dates:
        scores = factor_score(panel, sd, vol_window=vol_window)
        if not scores:
            continue
        fwd = forward_returns(panel, sd, max_days=max_days)
        ic = spearman_ic(scores, fwd, min_names=min_names)
        if ic is not None:
            ics.append(ic)
    return aggregate_ic(ics)
```

```python
# src/trading/cli.py — add near the other @app.command definitions.
# Imports to add at top (only those not already present):
#   from trading.backtest.factor_eval import factor_gated_metrics, information_coefficient

@app.command("factor-eval")
def factor_eval_cmd(
    start: Annotated[
        str, typer.Option(help="Inclusive eval start date (YYYY-MM-DD).")
    ] = "2023-01-01",
    end: Annotated[
        str | None, typer.Option(help="Inclusive end date (defaults to latest bar).")
    ] = None,
    top_quantile: Annotated[
        float, typer.Option(help="Top fraction of the universe to deem eligible.")
    ] = 0.30,
    vol_window: Annotated[
        int, typer.Option(help="Trailing window (bars) for realized volatility.")
    ] = 90,
) -> None:
    """Offline read-only evaluation of the Path A cross-sectional factor tilt.

    Reports Information Coefficient and a factor-gated vs rules-only per-trade
    metric comparison. Writes nothing to the trade book.
    """
    paths = get_paths()
    symbols = list_symbols(paths)
    if not symbols:
        console.print("[red]No parquet data on disk. Run `trading ingest-history` first.[/red]")
        raise typer.Exit(code=1)

    enriched: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            raw = read_ohlcv(sym, paths)
        except FileNotFoundError:
            continue
        if len(raw) >= 200:
            enriched[sym] = add_indicators(raw)
    if not enriched:
        console.print("[red]No symbol had >=200 bars after enrichment.[/red]")
        raise typer.Exit(code=1)

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else max(df.index.max() for df in enriched.values())
    console.print(
        f"[bold]Factor-eval:[/bold] {start_ts.date()} to {end_ts.date()} "
        f"on {len(enriched)} symbols (top {top_quantile:.0%}, vol window {vol_window})"
    )

    ic = information_coefficient(
        enriched, start=start_ts, end=end_ts, vol_window=vol_window
    )
    cmp = factor_gated_metrics(
        enriched, start=start_ts, end=end_ts, top_quantile=top_quantile, vol_window=vol_window
    )

    ic_table = Table(show_header=True, header_style="bold", title="Information Coefficient")
    ic_table.add_column("Metric")
    ic_table.add_column("Value", justify="right")
    ic_table.add_row("Mean IC", f"{ic.mean_ic:.4f}")
    ic_table.add_row("IC stdev", f"{ic.ic_std:.4f}")
    ic_table.add_row("IC t-stat", f"{ic.ic_t_stat:.2f}")
    ic_table.add_row("Positive-day rate", f"{ic.hit_rate_positive_days * 100:.1f}%")
    ic_table.add_row("Days", str(ic.n_days))
    console.print(ic_table)

    cmp_table = Table(show_header=True, header_style="bold", title="Per-trade: gated vs rules-only")
    cmp_table.add_column("Metric")
    cmp_table.add_column("Rules-only", justify="right")
    cmp_table.add_column("Factor-gated", justify="right")
    for label, attr, fmt in [
        ("Trades", "n", "{}"),
        ("Sharpe (×12)", "sharpe", "{:.2f}"),
        ("Profit factor", "profit_factor", "{:.2f}"),
        ("Hit rate", "hit_rate", "{:.2%}"),
        ("Payoff", "payoff", "{:.2f}"),
    ]:
        b = getattr(cmp.baseline, attr)
        g = getattr(cmp.gated, attr)
        cmp_table.add_row(label, fmt.format(b), fmt.format(g))
    console.print(cmp_table)

    console.print(
        "\n[bold]Phase-2 gate:[/bold] wire the eligible set into pre_open only if "
        "mean IC > 0 with t-stat ~>= 2 AND gated Sharpe/PF beat rules-only."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 uv run pytest tests/test_factor_eval.py tests/test_cli_factor_eval.py -v`
Expected: PASS. Then a manual smoke run against real data:
`PYTHONUTF8=1 uv run trading factor-eval --start 2023-01-01`
Expected: two tables print; no traceback.

- [ ] **Step 5: Commit**

```bash
git add src/trading/cli.py src/trading/backtest/factor_eval.py tests/test_factor_eval.py tests/test_cli_factor_eval.py
git commit -m "feat(cli): trading factor-eval command (Path A Phase 1)"
```

---

### Task 9 (GATED — do NOT execute until Phase 1 produces positive evidence): live `pre_open` integration

**Do not start this task** until a real `trading factor-eval` run shows **mean IC > 0 with t-stat ≳ 2 AND** the factor-gated per-trade Sharpe/profit-factor beat the rules-only baseline. If the evidence is negative, stop here: the factor tilt does not earn live wiring, and that is a valid, documented outcome. Report the numbers to the user and let them decide.

**Files (when unlocked):**
- Modify: `src/trading/config.py` (add a `factor_tilt_enabled: bool = False` flag, following the existing config pattern there)
- Modify: the `pre_open` candidate-selection path (locate via `grep -rn "pre_open\|pre-open" src/trading/jobs/`) to, when the flag is on, intersect rules-passing candidates with `eligible_set(factor_score(panel, scan_date))`.
- Test: extend the relevant `tests/test_pre_open*.py`.

**Interfaces:**
- Consumes: `trading.strategy.factors.{factor_score, eligible_set}`.
- Produces: behavior change in `pre_open` only when `factor_tilt_enabled` is true; default-off preserves current behavior exactly.

- [ ] **Step 1:** Write a failing test asserting that with the flag **off**, `pre_open` candidate output is byte-identical to today's (regression guard), and with the flag **on**, candidates not in the eligible set are dropped. (Fill in concrete fixtures from the actual `pre_open` test module once unlocked — match its existing fixture style.)
- [ ] **Step 2:** Run it; verify it fails for the flag-on case.
- [ ] **Step 3:** Add the `factor_tilt_enabled` flag and the gate in `pre_open`.
- [ ] **Step 4:** Run the full `pre_open` test module; verify green and that the flag-off regression guard still passes.
- [ ] **Step 5:** Commit `feat(pre-open): factor-tilt eligibility gate behind config flag (Path A Phase 2)`.

---

## Self-Review

**Spec coverage:**
- Factor computation (`factors.py`: `momentum_12_1`, `realized_vol`, `factor_score`, `eligible_set`) → Tasks 1-4. ✓
- Point-in-time correctness → Task 5. ✓
- IC evaluation (mean, stdev, t-stat, positive-day rate) → Task 6. ✓
- Factor-gated vs rules-only honest per-trade backtest → Task 7. ✓
- `trading factor-eval` CLI → Task 8. ✓
- Success criteria (IC>0 t-stat≳2 AND gated beats baseline) surfaced in CLI output and as the Task 9 gate. ✓
- Phase 2 live wiring, gated on Phase 1 → Task 9 (locked). ✓
- Open question resolved: forward-return basis uses `realized_return` (Task 6, per spec default). ✓

**Placeholder scan:** Task 9 intentionally defers concrete `pre_open` fixtures because it must not be implemented until Phase 1 validates and because the exact fixture style depends on the real `pre_open` test module; this is a deliberate gate, not a code placeholder. All Phase 1 tasks (1-8) contain complete, runnable code and tests.

**Type consistency:** `factor_score` returns `dict[str, float]`; `eligible_set` consumes `Mapping[str, float]`. `forward_returns` returns `dict[str, float]`; `spearman_ic` consumes two `Mapping[str, float]`. `ICResult`/`TradeMetrics`/`GatedComparison` field names match between definition and CLI consumption (`mean_ic`, `ic_std`, `ic_t_stat`, `hit_rate_positive_days`, `n_days`; `n`, `sharpe`, `profit_factor`, `hit_rate`, `payoff`). `realized_return` called with `max_days=` keyword matching its signature. ✓
