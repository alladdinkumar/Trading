# Phase 16 — LightGBM Ranker (Layer B) — Design

**Status:** approved (brainstorming complete 2026-05-24)
**Spec basis:** `docs/superpowers/specs/2026-05-11-trading-system-design.md` §4.2
**Predecessor phases:** Phase 5 (rules), Phase 6 (sizing/exits), Phase 7 (backtest + walk-forward harness — already exposes `SignalProvider` hook), Phase 8 (sentiment), Phase 9 (macro/regime), Phase 13 (`pre_open` orchestrator).

## 1. Scope

A pilot LightGBM ranker that:

- Scores rules-passing candidates with a probability ∈ [0, 1] of "this setup will produce a positive net P&L after the real Phase 6 exit logic runs."
- Filters the day's auto-opens to top-K (K = 5, matching the concurrency cap from spec §4.4).
- Persists `ml_score` on every rules-passing signal (selected and not) for dashboard / brief visibility.
- Trains offline via a manual CLI; promotion to "active" is gated by a 0.05 walk-forward Sharpe-improvement deadband.

**Pilot scope explicitly excludes** (deferred to follow-up mini-phases when the data lands):

- Sector features (`sector_rs_5d/20d/60d`, `sector_regime`) — needs Phase 12.6.
- F&O features (PCR, OI buildup, bid-ask spread bps) — needs OI ingest.
- Behavioural features (bulk/block deals, insider buy net) — needs ingest.
- Automated Sunday retrain via Task Scheduler — consistent with Phase 17's deliberate deferral of `weekly_train`. CLI only this phase.

**Why pilot, not full feature set:** today we have 12 parquet symbols on disk, sentiment alias-map coverage for ~6, no sector data, no OI. Training a ranker on the spec's full 25-30 features would produce mostly-NaN columns. Better to ship the cycle (feature builder → labels → walk-forward → registry → inference) with the features we have and grow the feature surface as data fills in. Phase 18's live paper-trade run feeds the labelled-data pool.

## 2. Architecture

```
                        ┌───────────────────────────┐
   Offline (manual):    │ trading train-ranker      │
                        │  --start --end [--promote]│
                        └─────────────┬─────────────┘
                                      │
   1. Walk through (start, end) at WF cadence (3y / 6mo / 3mo)
   2. For each fold:
      a. X = features for every rules-passing candidate in train slice
      b. y = label_candidate(...) replaying Phase 6 exit logic
      c. Fit LightGBM on (X, y) with conservative small-data hyperparameters
      d. Run engine on test slice with RankerSignalProvider → trades, Sharpe
   3. Aggregate OOS Sharpe across folds
   4. Train FINAL model on most recent train window (3y up to `end`)
   5. Save .pkl + update registry.csv (active flips iff --promote AND Sharpe+0.05)

                                      │ produces
                                      ▼
                        models/ranker_YYYY-MM-DD.pkl
                        models/registry.csv  ← single source of truth

                                      │ consumed by
                                      ▼
   Inference (live):
   pre_open._step_scan → passing(candidates)
                    └─→ ranker.score_and_filter(candidates, paths, conn, as_of, k=5)
                           ├─ load active model from registry
                           ├─ build live ctx (macro_snapshot + sentiment_daily)
                           ├─ feature row per candidate; model.predict_proba
                           ├─ sort desc, mark top-K selected=True
                           └─ return list[ScoredCandidate]
                    └─→ _step_auto_open opens trades ONLY for selected=True;
                           low-scoring rules-passing candidates still go into
                           signals table (ml_score populated) for visibility
```

**Cold start:** if `models/registry.csv` is empty or no row has `active=true`, `score_and_filter` returns all candidates unchanged with `ml_score=None, selected=True`. Pre_open behaviour reverts to today's (auto-open every rules-pass).

## 3. Modules

Five new files; each has one job and is independently testable.

```
src/trading/strategy/
├── ranker.py           # Inference: load active model, score, top-K filter
├── ranker_features.py  # Pure feature builder: candidate + ctx → feature row
├── ranker_labels.py    # Pure label builder: replays Phase 6 exit logic
└── ranker_train.py     # Walk-forward train loop + final fit

src/trading/store/
└── model_registry.py   # registry.csv read/write/promote (file I/O isolated here)
```

### 3.1 `ranker_features.py`

Pure functions only — no I/O.

```python
FEATURE_NAMES: tuple[str, ...] = (
    # setup
    "rsi_14", "pullback_pct_20", "pullback_pct_50", "atr_pct",
    "dist_from_52w_high",
    # trend
    "sma_20_slope_5d", "sma_50_slope_10d", "sma_200_slope_20d",
    "adx_14", "dist_from_52w_low",
    # volume
    "volume_vs_20d_avg", "obv_slope_5d",
    # macro
    "vix", "vix_change_5d", "fii_flow_5d_sum",
    "usdinr_change_5d", "regime_ord",
    # sentiment
    "sentiment_7d", "sentiment_30d", "negative_news_count_7d",
)

@dataclass(frozen=True)
class LiveContext:
    macro: MacroSnapshot | None       # from macro_snapshot for as_of
    sentiment: SentimentRow | None    # from sentiment_daily for (as_of, sym)
    macro_history: pd.DataFrame | None  # for vix_change_5d, usdinr_change_5d, fii_flow_5d_sum

def build_feature_row(
    enriched_df: pd.DataFrame,  # OHLCV + add_indicators output, sliced ≤ signal_date
    signal_date: pd.Timestamp,
    live_ctx: LiveContext,
) -> dict[str, float]: ...
```

NaN-safe: any missing component (e.g. no `sentiment_daily` row) yields NaN for that column. LightGBM handles NaN natively (treats it as a split direction). `FEATURE_NAMES` is the single source of truth — both training and inference iterate it to assemble the matrix, eliminating column-order skew.

### 3.2 `ranker_labels.py`

Pure function over `enriched_df` and a forward window. Re-uses `strategy.exits.evaluate_exit` and `backtest.costs.{apply_slippage, buy_charges, sell_charges}` — no duplicated exit/cost math.

```python
def label_candidate(
    enriched_df: pd.DataFrame,
    signal_date: pd.Timestamp,
    *,
    atr_stop_multiple: float = 1.5,
    max_days: int = 25,                   # Phase 6 time-exit horizon
    cost_config: CostConfig = ...,
) -> int | None:
    """Return 1 if net P&L > 0 at exit, 0 if ≤ 0, None if insufficient
    forward bars to resolve (< max_days bars after signal_date)."""
```

Implementation outline: replay one simulated trade — entry at next-bar open with slippage, daily `evaluate_exit` until action ≠ HOLD or `days_held = max_days`, accumulate buy + sell charges, return `int(net_pnl > 0)`.

### 3.3 `ranker_train.py`

Orchestrator. Owns the walk-forward loop and the final fit. Persists nothing — caller (CLI) writes to disk.

```python
@dataclass(frozen=True)
class FoldMetrics:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    n_train_examples: int
    n_trades_oos: int
    sharpe_oos: float
    hit_rate_oos: float
    skipped: bool                  # True if < 30 train examples

@dataclass(frozen=True)
class TrainResult:
    folds: tuple[FoldMetrics, ...]
    final_model: lgb.LGBMClassifier
    final_train_start: pd.Timestamp
    final_train_end: pd.Timestamp
    n_final_examples: int
    oos_sharpe_mean: float          # NaN-safe mean across non-skipped folds
    oos_hit_rate_mean: float
    feature_names: tuple[str, ...]

def train_walkforward(
    enriched: Mapping[str, pd.DataFrame],
    macro_history: pd.DataFrame,
    sentiment_lookup: SentimentLookup,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    wf_cfg: WalkForwardConfig = WalkForwardConfig(),
    bt_cfg: BacktestConfig | None = None,
) -> TrainResult: ...
```

**LightGBM hyperparameters** (small-data conservative):
- `objective="binary"`, `metric="binary_logloss"`
- `num_leaves=15`, `min_data_in_leaf=10`, `learning_rate=0.05`
- `n_estimators=200`, `early_stopping_rounds=20` against 20% within-fold validation slice (random, fixed seed)
- `is_unbalance=True` (positive class typically ~40%)
- `feature_pre_filter=False` (lets us keep low-coverage sentiment features)
- `random_state=42`

**Fold protocol:**
1. Iterate `windows(start, end, wf_cfg)` (existing helper).
2. For each fold: enumerate every (symbol, date) where the Phase 5 scanner all-passes within `[train_start, train_end)`; build feature row + label.
3. Drop rows where `label is None`.
4. If `n_train_examples < 30` → mark fold `skipped=True`; continue (do not fit).
5. Fit. Run `run_backtest` on `[test_start, test_end)` with `signal_provider=RankerSignalProvider(model, top_k=5)` → record OOS metrics.

**Final model:** fit on candidates from the most-recent train window — `[end - train_years, end)` — with the same hyperparameters. No walk-forward held-out; this is the production weights.

**Refusal:** if every fold is skipped (entire period < 30 examples), `train_walkforward` raises `InsufficientDataError`. CLI catches and exits 2.

### 3.4 `ranker.py`

Inference. Loads the active model once per `score_and_filter` call. No model caching across calls in this phase — `pre_open` runs once a day.

```python
@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate            # from rules.evaluate_symbol
    ml_score: float | None          # None ⇒ cold-start path
    selected: bool                  # True if in top-K (or cold-start)

def score_and_filter(
    candidates: list[Candidate],    # all rules-pass
    paths: Paths,
    conn: sqlite3.Connection,
    as_of: date,
    *,
    k: int = 5,
) -> list[ScoredCandidate]: ...
```

Companion class used by training:

```python
class RankerSignalProvider:
    def __init__(self, model: lgb.LGBMClassifier, top_k: int = 5) -> None: ...
    def __call__(self, d, enriched, ctx, config) -> list[Signal]:
        # 1. base = rule_signal_provider(d, enriched, ctx, config)
        # 2. score each base signal via build_feature_row + model.predict_proba
        # 3. return top-K by score (truncated list of Signal — engine sees nothing else)
```

### 3.5 `model_registry.py`

CSV chosen over JSON for two reasons: human-editable (you can flip `active` by hand if needed), and the file is short (one row per training run). Atomic write via temp-file + os.replace.

```
columns:
  version          # YYYY-MM-DD
  trained_at       # ISO timestamp UTC
  train_start      # YYYY-MM-DD
  train_end        # YYYY-MM-DD
  oos_sharpe       # float
  oos_hit_rate     # float
  n_train_examples # int
  n_features       # int
  path             # relative path to .pkl
  active           # 'true' | 'false' (lowercased)
  notes            # free text
```

API:

```python
@dataclass(frozen=True)
class ActiveModel:
    row: RegistryRow                # parsed registry row
    model: lgb.LGBMClassifier       # loaded from row.path
    feature_names: tuple[str, ...]  # persisted in pickle alongside model

def active(paths: Paths) -> ActiveModel | None: ...
def register(paths: Paths, *, row: RegistryRow, promote: bool) -> bool: ...
def all_rows(paths: Paths) -> list[RegistryRow]: ...
```

`register(promote=True)` checks `new.oos_sharpe > current_active.oos_sharpe + 0.05`. If yes: writes new row with `active=true`, flips previous active row to `active=false`. If no: writes new row with `active=false` and prints a warning. The `0.05` deadband is constant `SHARPE_PROMOTION_DEADBAND` for testability.

**Pickle format:** `joblib.dump({"model": lgbm, "feature_names": FEATURE_NAMES, "trained_at": ...})` — feature names travel with the model so inference can detect a mismatch and refuse to score (raises `RegistryFeatureMismatch`).

## 4. Integration with `pre_open.py`

One new step between scan and auto-open.

```python
# was:
candidates = _step_scan(...)
passing_candidates = passing(candidates)
opened = _step_auto_open(conn, as_of, passing_candidates, ...)

# becomes:
candidates = _step_scan(...)
passing_candidates = passing(candidates)
scored = _step_rank(conn, paths, as_of, passing_candidates, warnings)
opened = _step_auto_open(conn, as_of, scored, ...)   # uses .selected
```

- `_step_rank` calls `ranker.score_and_filter(...)`. On any error (missing model row, pickle load failure, feature-mismatch) it logs a warning, falls back to cold-start behaviour, and pre_open continues.
- `_step_auto_open` is updated to iterate `list[ScoredCandidate]`, persist `ml_score` to `signals.ml_score` for every scored candidate (selected and not), and open a paper-trade only when `selected=True`.
- `PreOpenResult` gains a `candidates_scored: int` field and a `candidates_selected: int` field (the latter ≤ K).

**Brief integration:** `assemble_context` in `llm/context.py` gains an optional `Layer B ranker` section — a small markdown table of top-K scores. Skipped silently when no active model. This is additive across all modes (`pre_open`, `mid_day`, `post_close`); kept minimal so the analyst skill's existing templates don't need rework.

## 5. CLI

```
trading train-ranker --start 2023-05-01 --end 2026-05-01 [--promote] [--report]
trading ranker-status
```

- `train-ranker` enriches all parquet symbols (re-uses `backtest`'s helper), pulls macro_history + sentiment_lookup from the DB, calls `train_walkforward`, writes `models/ranker_<end>.pkl`, calls `model_registry.register(promote=...)`. Prints Rich table of per-fold metrics + aggregate.
  - `--report` writes a markdown summary to `data/research/ranker_<ts>.md` (per-fold table, feature-importance bar chart as ASCII, sample predictions on a held-out tail).
  - `--promote` triggers the soft-promotion gate. Without it, the new model is registered with `active=false` (you can still inspect it via `ranker-status`).
- `ranker-status` prints the registry table from disk; flags the active row.

Both subcommands re-use the existing typer app in `src/trading/cli.py`.

## 6. Error handling

| Scenario | Behaviour |
|---|---|
| `train-ranker` and all folds skipped (< 30 examples per fold) | `InsufficientDataError` → CLI exits 2 with the count and the suggestion to expand universe or extend `--start`. |
| `train-ranker` and `--promote` but Sharpe doesn't clear deadband | Model still saved + registered (`active=false`). Warning printed; exit 0. |
| `pre_open` and `models/registry.csv` missing or empty | Cold-start path — warning, all candidates `selected=True`, behaviour identical to today. |
| `pre_open` and active row references missing `.pkl` | Warning, cold-start path, pre_open continues. |
| `pre_open` and pickle's `feature_names` doesn't match current `FEATURE_NAMES` | Warning, cold-start path (we changed features; old model can't score). Re-train required. |
| `model_registry.register` and `models/` doesn't exist | Created via `mkdir(parents=True, exist_ok=True)`. |

All four `pre_open` failure paths funnel into the same fallback: log a warning, return candidates with `ml_score=None, selected=True`. The pipeline never breaks because the ranker is unavailable.

## 7. Testing

~30 new tests; pure-function-heavy.

- `test_ranker_features.py` (8): per-feature builder correctness on synthetic OHLCV; `FEATURE_NAMES` parity check (every name resolves to a builder); NaN propagation when `LiveContext` slots are None; 52w high/low handling when df < 252 bars.
- `test_ranker_labels.py` (5): TARGET → 1, STOP → 0, TIME-positive → 1, TIME-negative → 0, insufficient-forward-bars → None.
- `test_ranker_train.py` (6): walk-forward fold enumeration matches `windows()`; fits separable synthetic labels above random; small-data refusal (< 30 examples → fold skipped); empty universe raises `InsufficientDataError`; feature/label row count alignment per fold; final model trained on the most-recent train window only.
- `test_model_registry.py` (5): write/read round-trip; exactly-one-active invariant; promote with deadband (improves vs doesn't); pickle round-trip preserves `feature_names`.
- `test_ranker.py` (4): cold-start (no registry rows) returns candidates with `selected=True`, `ml_score=None`; top-K filter respects K; `ml_score` populated on all candidates; missing macro/sentiment rows degrade to NaN (not exception).
- `test_jobs_pre_open.py` patches (2): pre_open with active model auto-opens only top-K; pre_open with no model behaves identical to pre-Phase-16.

Lint + mypy clean. Existing test suite (566 passed at end of Phase 15) must stay green.

## 8. Smoke test plan

After lint/types/tests green:

1. `trading train-ranker --start 2023-05-01 --end 2026-04-01 --report` over the 12-symbol parquet universe.
2. Expect: per-fold table, OOS Sharpe (may be NaN if data too sparse — that's a documented outcome).
3. Inspect `data/research/ranker_<ts>.md` for sanity (feature importances should rank technicals first; sentiment last given coverage).
4. If at least one fold trained, run `trading train-ranker --start ... --end ... --promote`. Confirm registry now has `active=true`.
5. `trading pre-open 2026-05-22` — confirm `candidates_scored`, `candidates_selected` populated in result; `signals.ml_score` populated in DB.
6. `trading ranker-status` — confirm CLI table renders.

If the pilot model trains with so few examples that OOS Sharpe is uninformative, that is itself a deliverable — the cycle is in place; Phase 18 will accumulate the data that makes the model meaningful. Document the result in `PROGRESS.md`.

## 9. Out of scope

- Automated Sunday retrain via Task Scheduler (deferred with Phase 17's `weekly_train`).
- Sector / F&O / behavioural features.
- Universe expansion (still 12 parquet symbols — separate concern).
- Using `ml_score` to influence position sizing or `pre_open_iep`'s rerank composite — score is read-only this phase.
- Hyperparameter search — fixed values; revisit only if results are clearly broken.

## 10. Progression after Phase 16

- PROGRESS.md status snapshot row 16 flips to `[x]`.
- Phase 18 (live paper-trading) becomes the labelled-data factory. Each closed paper-trade is a future training row.
- Mini-phase candidates surfaced by this work: (a) automate Sunday retrain via Task Scheduler, (b) universe expansion to ~Nifty 100, (c) bring sector/F&O features online once Phase 12.6 + OI ingest land.
