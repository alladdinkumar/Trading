# Path A — OHLCV cross-sectional factor tilt (design)

_Date: 2026-06-24_

## Problem & motivation

The LightGBM ranker has no out-of-sample edge: after the F-045 fixes (honest
return-based OOS Sharpe, magnitude-weighted training, meta-labeling threshold),
the model still does not clear the F-043 promotion floor — its confident picks
underperform the rules-layer base rate OOS. Root cause is the **feature set**:
all 20 ranker features are price/volume technicals, market-wide macro (identical
across stocks each day, so no cross-sectional signal), or news sentiment. None
are the cross-sectional factors that carry documented equity-return premia.

Market evidence we are acting on:
- **Gu–Kelly–Xiu (2020):** across 900 predictors the dominant, model-agnostic
  signals are momentum, liquidity, volatility — predicting the *cross-section*
  of individual stock returns.
- **Indian factor studies (S&P DJI; 18-yr NSE backtests):** low-volatility and
  momentum are the strongest single factors (low-vol ~12.4% vs Nifty ~10.4%
  CAGR, lower drawdown; momentum highest information ratio).

Both factors are computable from **OHLCV alone** — no new data source, no
look-ahead risk. This is the cheapest, evidence-backed lever.

## Goal

Introduce a transparent, cross-sectional **two-factor composite** (12-1
momentum + low realized volatility) that ranks the universe each day and gates
which names are eligible for entry. Prove it has edge **offline** before
changing any live trading behavior.

Non-goals (YAGNI for this cut): fundamental/value/quality factors (Path B),
learning-to-rank, universe broadening, volatility-scaled sizing, the ML ranker.

## Design decisions (locked)

| Decision | Choice |
|---|---|
| Architecture | Direct factor score (no ML) |
| Integration | Factor-first, rules time entry |
| Universe | Existing on-disk symbols (~62); ranking written universe-agnostic |
| Factors | Two-factor, equal-weight cross-sectional z-score: 12-1 momentum + low realized vol |
| Delivery | Two-phase: prove offline (Phase 1) before wiring live `pre_open` (Phase 2) |
| Eligible set | Top **30%** by composite (configurable quantile) |

## Components

### 1. Factor computation — `src/trading/strategy/factors.py` (pure, no I/O)

Mirrors the purity of `ranker_features.py`. All functions are point-in-time:
a factor at date `t` uses only bars with index ≤ `t`.

- `momentum_12_1(df, as_of) -> float | None`
  - Return from `t-252` to `t-21` trading days: `close[t-21] / close[t-252] - 1`.
  - Skips the most recent ~21 trading days (standard, to avoid short-term
    reversal contaminating the momentum signal).
  - `None` if fewer than 273 bars are available at/through `as_of`.
- `realized_vol(df, as_of, window=90) -> float | None`
  - Standard deviation of daily log returns over the trailing `window` bars.
  - `None` if fewer than `window + 1` bars.
- `factor_score(panel, as_of, *, vol_window=90) -> dict[str, float]`
  - `panel: Mapping[str, pd.DataFrame]` — symbol → enriched/OHLCV frame.
  - For each symbol compute momentum and realized vol (PIT).
  - Drop symbols whose either factor is `None` (insufficient history) from that
    day's cross-section.
  - Cross-sectionally **z-score** each factor across the surviving symbols.
    Momentum: higher is better. Volatility: lower is better → use the negated
    z-score (`z_lowvol = -z_vol`).
  - Composite = mean(`z_momentum`, `z_lowvol`). Higher = more attractive.
  - Return `{symbol: composite}` for surviving symbols (NaN/excluded symbols
    absent from the mapping).
- `eligible_set(scores, *, top_quantile=0.30) -> set[str]`
  - The top `top_quantile` of symbols by composite. With ~62 names and 0.30 →
    ~18 names. Ties broken deterministically by score then symbol.

Edge cases: a cross-section with <2 surviving symbols → z-score undefined →
return empty/degenerate ranking (no eligible set that day). Zero stdev in a
factor across the cross-section → that factor contributes 0 (all z = 0).

### 2. Evaluation harness — `trading factor-eval` CLI + supporting module

Repeatable, read-only, offline. Operates on existing parquet over a date range.

- **Information Coefficient (IC):** for each trading day in the range, compute
  the composite score cross-section and the realized forward 25-day return per
  symbol (reuse `ranker_labels.realized_return` for cost/exit consistency, or a
  simple forward close-to-close return — see open question), then the Spearman
  rank correlation between score and forward return. Report mean IC, IC stdev,
  IC t-stat (`mean / (stdev / sqrt(n_days))`), and hit rate of positive-IC days.
  Positive, stable IC ⇒ the composite sorts forward returns.
- **Factor-gated backtest:** replay the existing Phase-6 exit logic, but allow a
  trade to open only when the symbol is in that day's `eligible_set`. Compare
  against the rules-only baseline (no factor gate) on the **honest return-based**
  metrics: per-trade Sharpe, profit factor, CAGR, win rate, payoff ratio,
  max drawdown. Walk-forward over the range if feasible; otherwise full-sample
  with the same purge discipline as training.
- **Success criteria:** mean OOS IC > 0 with t-stat ≳ 2, **and** the gated
  backtest improves Sharpe/PF over the rules-only baseline (which we measured at
  PF ≈ 0.99). If neither holds, the factor tilt does not earn Phase 2.

### 3. Phase 2 — live integration (gated on Phase 1 success)

Only if Phase 1 validates: in `pre_open` candidate selection, compute the
day's `eligible_set` over the universe and keep only rules-passing candidates
whose symbol is in it. This slots in where the (cold-start) ML ranker sits;
the pending-entry → open-fill → exit machinery is unchanged. Behind a config
flag, defaulting off until validated.

## Data flow

```
parquet OHLCV (per symbol)
   → momentum_12_1 / realized_vol   (per symbol, point-in-time)
   → factor_score                   (cross-sectional z-score + composite, per day)
   → eligible_set (top 30%)         (ranked universe → eligible names)
   → [Phase 2] gate rules-scan candidates
   → pullback rules time the entry  (existing)
   → pending entries → open fills → exits (existing, unchanged)
```

## Testing (TDD) — `tests/test_factors.py`

- `momentum_12_1`: known synthetic series → expected value; <273 bars → None;
  uses `close[t-21]/close[t-252]-1` (verify the 21-day skip).
- `realized_vol`: constant-return series → ~0 vol; known series → expected
  stdev; insufficient history → None.
- **Point-in-time correctness:** factor at date `t` is unchanged when future
  bars are appended (no look-ahead).
- `factor_score`: cross-sectional z-scoring (mean ~0, stdev ~1); low-vol sign
  flip (lowest-vol name gets the highest low-vol z); NaN/short-history symbols
  excluded; degenerate cross-sections handled.
- `eligible_set`: correct top-quantile size and membership; deterministic tie
  handling.
- IC computation: synthetic where score perfectly ranks forward returns → IC ≈ 1;
  inverted → IC ≈ -1.

## Open questions / notes

- **Forward-return basis for IC:** use `realized_return` (full exit replay, with
  costs) for consistency with how trades actually resolve, vs a plain forward
  25-day close-to-close return (simpler, factor-purist). Default to
  `realized_return` for consistency; revisit if it muddies the IC signal.
- **vol_window:** default 90 trading days (~4.5 months). 60–120 all defensible;
  fixed at 90 for the first cut, exposed as a parameter.
- Universe-agnostic by construction, so Path B (broaden universe / add
  fundamentals) reuses `factor_score` unchanged.
