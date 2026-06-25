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
   point the usable-gate + `active ⟹ usable` invariant let it speak (the bar
   replaces F-043's bare positive-Sharpe floor).

**The usable bar (market-knowledge refined — three gates, all required):**

1. **Statistical edge, not just sign.** A raw `Sharpe > 0` is noise at small N
   (Sharpe SE ≈ `√((1 + ½·SR²)/N)`). Gate on a **t-statistic on the pooled mean
   net return**: `t = oos_sharpe_pooled · √(n_oos_total / 12) ≥ T_MIN` with
   `T_MIN = 2.0`. Reuses the existing `sharpe(…, periods_per_year=12)` number
   and implicitly demands more trades when the edge is small. `n_oos_total ≥ 50`
   is retained as a **computability floor** (a t-stat on < 50 trades is itself
   unstable), not the real test.
2. **Breadth (regime robustness).** Positive realised mean in a **majority of
   non-skipped folds** (`≥ ⌈n_folds / 2⌉`). Pooling stops one lucky fold from
   inflating the *mean*, but a single high-trade-count fold can still carry the
   *pool*; breadth blocks regime-concentrated "edge".
3. **Persistence (multiple-testing deflation).** Weekly retraining runs ~52
   hypothesis tests/year, so a zero-edge model spuriously clears ~2–3×/year by
   chance (Deflated Sharpe, Bailey & López de Prado). Promote only after
   **2 consecutive weekly "usable" verdicts** — the `ranker_eval_log` (Component
   C) is the persistence memory, so C becomes load-bearing for the gate, not just
   a report.

**Deliberately excluded:** a hit-rate floor. Sharpe already captures payoff
asymmetry; a low-hit / high-payoff trend profile is genuine expectancy and a
hit-rate floor would wrongly reject convex payoffs.

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
  `n_oos_total: int`, and the breadth inputs `n_folds_positive: int` /
  `n_folds_total: int` (count of non-skipped folds with a positive realised mean,
  and total non-skipped folds). `oos_sharpe_mean` / `oos_hit_rate_mean` are
  **retained for reporting/continuity** but are no longer the promotion basis.

### Component B — Credible usable-gate (three gates)

Constants: `MIN_OOS_TRADES = 50` (computability floor), `T_MIN = 2.0`
(t-stat threshold), `MIN_USABLE_STREAK = 2` (consecutive weekly verdicts).

- `RegistryRow` gains `n_oos_trades: int` and `n_folds_positive: int` /
  `n_folds_total: int` (new CSV columns; `to_csv_row` / `from_csv_row` updated;
  legacy rows missing the columns read as 0 → never clear, the safe default).
- `RegistryRow.oos_sharpe` now carries the **pooled** Sharpe.
- A pure helper `_is_usable(oos_sharpe, n_oos_trades, n_folds_positive,
  n_folds_total) -> bool` encodes gates 1 + 2 (the per-train test):
  - **G1 (statistical edge):** `n_oos_trades ≥ MIN_OOS_TRADES` AND
    `oos_sharpe * sqrt(n_oos_trades / 12) ≥ T_MIN`. NaN never passes.
  - **G2 (breadth):** `n_folds_positive ≥ ceil(n_folds_total / 2)`.
- `_clears_floor` (`model_registry.py:190`) is replaced by `_is_usable` at both
  call sites: `register` (promotion path) and `_demote_subfloor` (the
  `active ⟹ usable` invariant). The old `SHARPE_PROMOTION_FLOOR > 0` check is
  subsumed by G1 (a positive t-stat implies a positive Sharpe).
- **G3 (persistence)** lives in `register`. Ordering is fixed to avoid
  ambiguity: `weekly_train` **first** inserts the current week's verdict row into
  `ranker_eval_log`, **then** calls `register`. Promotion requires the most
  recent `MIN_USABLE_STREAK` (=2) verdict rows — current included — to all be
  `usable = 1`. The first-ever usable verdict therefore records but does **not**
  promote (only one usable row exists). The `active ⟹ usable` demotion invariant
  is *not* subject to G3 — a model that stops being usable (G1/G2 fail on the
  current eval) is demoted immediately (asymmetric: slow to trust, fast to mute).
- Net effect on today's data: pooled N=38 (< 50) and negative pooled Sharpe →
  G1 fails → never promotes → planner keeps `p_win=prior`. Silence by
  measurement.

### Component C — Weekly verdict record

- New migration (next `schema_version`): table
  `ranker_eval_log(as_of TEXT, pooled_sharpe REAL, pooled_hit REAL,
  n_oos INTEGER, usable INTEGER, note TEXT)` with `as_of` indexed/unique.
- `store/repo.py` helpers following the existing `insert_prediction` pattern:
  `insert_ranker_eval(...)` and `latest_ranker_eval(conn) -> row | None` (the
  latter is what `register`'s G3 persistence check reads — C is load-bearing for
  the gate, not just a report).
- `weekly_train._step_retrain` writes one row each Sunday from the `TrainResult`
  pooled fields **before** calling `register` (see G3 ordering above), and
  `render_weekly_review` (`weekly_train.py:308`) renders a human line, e.g.:
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
- **B:** `test_model_registry.py` — (1) **G1**: positive Sharpe but
  `t < 2.0` (small N) does **not** pass `_is_usable`; t ≥ 2.0 with N ≥ 50 does;
  N < 50 never passes regardless of t. (2) **G2 breadth**: a pool carried by one
  high-count fold while a minority of folds are positive fails; majority-positive
  passes. (3) **G3 persistence**: a first usable verdict records but does **not**
  promote; promotion fires only when the prior `ranker_eval_log` row is also
  usable. (4) **Asymmetry**: an active model that turns unusable is demoted on
  the next eval *without* waiting for a streak. (5) CSV round-trip with the new
  `n_oos_trades` / `n_folds_positive` / `n_folds_total` columns; legacy rows
  (missing columns) read as 0 and never pass.
- **C:** migration test (table exists, round-trips); `insert_ranker_eval` +
  read; `render_weekly_review` contains the verdict line for a not-usable result.
- **Regression guard:** an end-to-end-ish test asserting the current real data
  (or a fixture mirroring it) yields `usable = False`.

## Rollout / commit shape

One coherent commit set on `factor-tilt-path-a` (folds in the existing
uncommitted objective work), in plan order: A → B → C → E. Commit and push per
the standing workflow. No `weekly_train --promote` run is needed to land this;
the next scheduled Sunday retrain will record the first verdict.
