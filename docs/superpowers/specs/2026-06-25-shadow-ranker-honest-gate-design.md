# Shadow Ranker — silent, weekly-evaluated learner (honest OOS gate)

**Date:** 2026-06-25
**Status:** Design approved, pending spec review
**Findings:** closes part of **F-044**; opens **F-046**
**Branch base:** `factor-tilt-path-a` (folds in the current uncommitted ranker work)

## Problem

The LightGBM ranker has no demonstrated out-of-sample edge, yet the machinery
that decides whether it earns live weight is untrustworthy:

- A walk-forward over 62 symbols / 2021–2026 with the current uncommitted
  objective (magnitude-aware return + abstention threshold + top-K cap) reports
  `oos_sharpe_mean = +2.574` — which *clears* the F-043 promotion floor (0.0)
  and would promote the model under `weekly_train --promote`.
- That +2.574 is an artifact. It is the arithmetic **mean of per-fold Sharpes**,
  dominated by one lucky 3-trade fold (+83.53). The honest picture: **median
  fold Sharpe −0.26, only 2 of 7 folds positive, pooled hit-rate 0.27**. The
  abstention + top-K machinery shrank each fold to 1–15 trades, where a Sharpe is
  pure noise — and ironically made a spurious floor-clear *more* likely.
- The realised live book is tiny: **3 matured predictions, 3 closed paper
  trades**, spanning ~8 days. F-044's literal fix (train the learner on realised
  outcomes) is data-blocked for roughly a year.

So the model must **sit silent and learn**, and the weekly evaluation that asks
"is it usable yet?" must be answered by a statistic that cannot be faked by a
handful of lucky trades.

## Goal

A shadow ranker that:

1. Retrains weekly on all data it can see (already happens via `weekly_train`).
2. Is judged by an **honest pooled statistic**, not a mean of small-sample folds.
3. Records a **dated, auditable "usable yet?" verdict** every Sunday.
4. Stays **mute (zero live weight)** until it clears a credible bar — at which
   point F-043's existing floor + `active ⟹ clears floor` invariant let it speak.

**The usable bar:** `oos_sharpe_pooled > 0` **AND** `n_oos_total ≥ 50`.

## Non-goals (deferred)

- **Component D — realised-outcome training join (the literal F-044 deepening).**
  Reading `predictions.actual_return_at_horizon` / closed `paper_trades` into the
  training label, gated weight-0 until ~200 closed trades exist. It cannot be
  meaningfully integration-tested until the live book is deep enough (~a year
  out), so it gets its own follow-up spec. This pass makes the system honest
  *today*; D makes it *smarter* later.

## Design

### Component A — Honest pooled OOS statistic

`ranker_train.py` today computes per-fold `(n_trades, sharpe, hit)` then
`oos_sharpe_mean = mean(fold sharpes)` (`train_walkforward`, ~line 376–384).

Change:
- `_evaluate_fold_oos` currently returns `(n_trades, sharpe, hit_rate)` and
  discards the realised per-trade return vector it builds internally. It now also
  returns that vector (e.g. `(n_trades, sharpe, hit_rate, realised: list[float])`),
  or `train_walkforward` accumulates it via a small refactor.
- `train_walkforward` concatenates the realised returns across **all non-skipped
  folds** into one pooled vector and computes:
  - `oos_sharpe_pooled = sharpe(pooled, periods_per_year=12)`
  - `oos_hit_pooled = (pooled > 0).mean()`
  - `n_oos_total = len(pooled)`
- `TrainResult` gains `oos_sharpe_pooled: float`, `oos_hit_pooled: float`,
  `n_oos_total: int`. `oos_sharpe_mean` / `oos_hit_rate_mean` are **retained for
  reporting/continuity** but are no longer the promotion basis.

### Component B — Credible usable-gate

- New module constant `MIN_OOS_TRADES = 50`.
- `RegistryRow` gains `n_oos_trades: int` (new CSV column; `to_csv_row` /
  `from_csv_row` updated; old rows without the column read as 0 → never clears
  the floor, which is the safe default).
- `RegistryRow.oos_sharpe` now carries the **pooled** Sharpe.
- `_clears_floor` (`model_registry.py:190`) becomes
  `_clears_floor(oos_sharpe, n_oos_trades)` and requires **both**
  `oos_sharpe > SHARPE_PROMOTION_FLOOR` **and** `n_oos_trades >= MIN_OOS_TRADES`.
  All call sites updated: `register` (promotion path) and `_demote_subfloor`
  (the `active ⟹ clears floor` invariant). NaN still never clears.
- Net effect on today's data: pooled N=38 (< 50) and negative pooled Sharpe →
  model never promotes → planner keeps `p_win=prior`. Silence by measurement.

### Component C — Weekly verdict record

- New migration (next `schema_version`): table
  `ranker_eval_log(as_of TEXT, pooled_sharpe REAL, pooled_hit REAL,
  n_oos INTEGER, usable INTEGER, note TEXT)` with `as_of` indexed/unique.
- A `store/repo.py` insert helper (`insert_ranker_eval`) following the existing
  `insert_prediction` pattern.
- `weekly_train._step_retrain` writes one row each Sunday from the `TrainResult`
  pooled fields, and `render_weekly_review` (`weekly_train.py:308`) renders a
  human line, e.g.:
  > Ranker NOT usable: pooled OOS Sharpe −0.31 over N=38, hit 0.27 — staying
  > silent.
- `RetrainOutcome` plumbs the pooled fields so the report and the Slack body can
  show the verdict.

### Component E — Findings hygiene

- The uncommitted comments in `ranker_train.py` / `forward_return.py` mis-cite
  **F-045** (which is the `daily_budget → paper.ledger` layering debt) for the
  magnitude/threshold work. Re-cite them to **F-044** (model quality).
- Add **F-046** to `docs/architecture/FINDINGS.md`:
  *"Promotion gated on `oos_sharpe_mean` (mean of per-fold Sharpes); at the
  small per-fold trade counts the abstention/top-K cap produces, a single lucky
  fold spuriously clears the F-043 floor. Fixed by the pooled statistic +
  `MIN_OOS_TRADES` gate."* — marked Fixed by this pass.
- Re-scope **F-044** in FINDINGS.md: shadow learner + honest gate (A–C) done;
  realised-outcome training join (D) deferred to its own spec.

## Testing

TDD throughout; full suite must stay green (ruff + mypy clean).

- **A:** unit test that `train_walkforward` pools across folds — construct folds
  with known realised returns and assert `oos_sharpe_pooled` / `n_oos_total`
  match a hand-computed pool, and that one lucky 3-trade fold can **no longer**
  drag the pooled Sharpe positive when the rest are negative.
- **B:** `test_model_registry.py` — (1) a model with pooled Sharpe > 0 but
  `n_oos_trades < 50` does **not** clear the floor; (2) Sharpe > 0 and N ≥ 50
  promotes; (3) a pre-existing active row with N < 50 is demoted by the
  invariant; (4) CSV round-trip with the new `n_oos_trades` column; legacy rows
  (missing column) read as 0 and never clear.
- **C:** migration test (table exists, round-trips); `insert_ranker_eval` +
  read; `render_weekly_review` contains the verdict line for a not-usable result.
- **Regression guard:** an end-to-end-ish test asserting the current real data
  (or a fixture mirroring it) yields `usable = False`.

## Rollout / commit shape

One coherent commit set on `factor-tilt-path-a` (folds in the existing
uncommitted objective work), in plan order: A → B → C → E. Commit and push per
the standing workflow. No `weekly_train --promote` run is needed to land this;
the next scheduled Sunday retrain will record the first verdict.
