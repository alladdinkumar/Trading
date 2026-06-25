# Shadow Ranker — Honest OOS Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ranker a silent shadow learner that is judged by a statistically honest pooled OOS gate (t-stat + breadth + persistence), records a dated weekly "usable?" verdict, and stays mute until it clears a credible bar.

**Architecture:** Replace the noise-prone "mean of per-fold Sharpes" promotion statistic with a pooled per-trade statistic computed in `ranker_train.train_walkforward`. Gate promotion in `model_registry` on a pure `_is_usable` helper (t-stat ≥ 2.0 over ≥50 pooled trades AND positive in a majority of folds). Add a `ranker_eval_log` table; `weekly_train` writes one verdict row each Sunday and only authorizes promotion when this week *and* last week were both usable (2-consecutive persistence, deflating the ~52 tests/yr multiple-testing bias).

**Tech Stack:** Python 3.9+, LightGBM, pandas/numpy, sqlite3, pytest, ruff, mypy. Package manager: `uv` (run tests via `uv run pytest`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-25-shadow-ranker-honest-gate-design.md`.
- Findings: closes part of **F-044**; opens **F-046**. Re-cite mis-numbered **F-045** comments to **F-044**.
- TDD throughout. Full suite (`uv run pytest`) must stay green; `ruff check .` and `mypy src/` clean.
- New `RegistryRow` / `TrainResult` fields MUST have defaults (0 / NaN) so existing construction sites and CSV legacy rows stay valid; 0 is the safe "never usable" default.
- Gate constants (exact): `MIN_OOS_TRADES = 50`, `T_MIN = 2.0`, `MIN_USABLE_STREAK = 2`.
- t-statistic formula (exact): `t = oos_sharpe_pooled * sqrt(n_oos_trades / 12.0)`; the pooled Sharpe uses `sharpe(pd.Series(pooled), periods_per_year=12)`, matching the existing fold convention.
- No hit-rate floor (would reject convex payoffs).
- Commit and push after the final task per the standing workflow.

---

### Task 1: Pooled OOS statistic in `train_walkforward` (Component A)

**Files:**
- Modify: `src/trading/ranking/ranker_train.py` (`_evaluate_fold_oos` return type ~222–286; `TrainResult` ~93–101; `train_walkforward` fold loop + aggregation ~330–396)
- Test: `tests/test_ranker_train.py`

**Interfaces:**
- Produces: `_evaluate_fold_oos(...) -> tuple[int, float, float, list[float]]` (now also returns the realised per-trade return list).
- Produces: `TrainResult` gains `oos_sharpe_pooled: float = float("nan")`, `oos_hit_pooled: float = float("nan")`, `n_oos_total: int = 0`, `n_folds_positive: int = 0`, `n_folds_total: int = 0`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ranker_train.py`:

```python
def test_pooled_oos_not_inflated_by_one_lucky_fold() -> None:
    """One lucky small fold must not drag the pooled stat positive when the
    rest of the realised trades are losers. Pooling is trade-weighted, so a
    3-trade winner can't outvote 30 losers the way a mean-of-folds can."""
    import numpy as np
    from trading.ranking.ranker_train import _pool_oos

    # fold A: 3 big winners; fold B: 30 small losers
    fold_returns = [[0.20, 0.25, 0.30], [-0.01] * 30]
    pooled_sharpe, pooled_hit, n_total, n_pos, n_folds = _pool_oos(fold_returns)
    assert n_total == 33
    assert n_folds == 2
    assert n_pos == 1            # only fold A had a positive mean
    assert pooled_hit < 0.5
    assert pooled_sharpe < 0.0   # the 30 losers dominate the pool
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ranker_train.py::test_pooled_oos_not_inflated_by_one_lucky_fold -v`
Expected: FAIL with `ImportError: cannot import name '_pool_oos'`.

- [ ] **Step 3: Add the `_pool_oos` helper**

Add to `src/trading/ranking/ranker_train.py` (above `train_walkforward`):

```python
def _pool_oos(
    fold_returns: list[list[float]],
) -> tuple[float, float, int, int, int]:
    """Pool realised per-trade returns across folds into one honest statistic.

    Returns (pooled_sharpe, pooled_hit, n_oos_total, n_folds_positive,
    n_folds_total). `n_folds_total` counts non-empty folds; `n_folds_positive`
    counts folds whose realised mean is > 0 (the breadth check). Trade-weighted
    pooling stops a lucky small fold from inflating a mean-of-fold-Sharpes
    (F-046)."""
    non_empty = [f for f in fold_returns if f]
    pooled = [r for f in non_empty for r in f]
    if not pooled:
        return float("nan"), float("nan"), 0, 0, len(non_empty)
    s = pd.Series(pooled, dtype=float)
    n_folds_positive = sum(1 for f in non_empty if float(np.mean(f)) > 0.0)
    return (
        float(sharpe(s, periods_per_year=12)),
        float((s > 0).mean()),
        len(pooled),
        n_folds_positive,
        len(non_empty),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ranker_train.py::test_pooled_oos_not_inflated_by_one_lucky_fold -v`
Expected: PASS

- [ ] **Step 5: Thread realised returns out of `_evaluate_fold_oos`**

In `_evaluate_fold_oos`, change the signature return annotation to
`-> tuple[int, float, float, list[float]]` and update both `return` paths:

```python
    if not feat_rows:
        return 0, float("nan"), float("nan"), []
```

```python
    if not realised:
        return 0, float("nan"), float("nan"), []
    returns = pd.Series(realised, dtype=float)
    return (
        len(realised),
        float(sharpe(returns, periods_per_year=12)),
        float((returns > 0).mean()),
        realised,
    )
```

- [ ] **Step 6: Add fields to `TrainResult`**

In the `TrainResult` dataclass, after `threshold: float | None = None` add:

```python
    oos_sharpe_pooled: float = float("nan")
    oos_hit_pooled: float = float("nan")
    n_oos_total: int = 0
    n_folds_positive: int = 0
    n_folds_total: int = 0
```

- [ ] **Step 7: Aggregate the pool in `train_walkforward`**

In the fold loop, change the `_evaluate_fold_oos` unpack to capture the realised
list and accumulate it. Where the loop currently reads
`n_trades, sh, hr = _evaluate_fold_oos(...)`, replace with:

```python
        n_trades, sh, hr, realised = _evaluate_fold_oos(
            enriched,
            macro_history,
            sentiment_lookup,
            negative_news_lookup,
            model,
            win.test_start,
            win.test_end,
            threshold=tau,
        )
        fold_returns.append(realised)
```

Initialise `fold_returns: list[list[float]] = []` just before the
`for win in windows(...)` loop. After the existing `oos_sharpe_mean` /
`oos_hit_rate_mean` block, add:

```python
    pooled_sharpe, pooled_hit, n_oos_total, n_folds_pos, n_folds_tot = _pool_oos(
        fold_returns
    )
```

Then add these to the `TrainResult(...)` constructor:

```python
        oos_sharpe_pooled=pooled_sharpe,
        oos_hit_pooled=pooled_hit,
        n_oos_total=n_oos_total,
        n_folds_positive=n_folds_pos,
        n_folds_total=n_folds_tot,
```

- [ ] **Step 8: Run the ranker-train suite**

Run: `uv run pytest tests/test_ranker_train.py -v`
Expected: PASS (existing `test_oos_sharpe_mean_is_nan_safe` still green — `oos_sharpe_mean` is retained).

- [ ] **Step 9: Lint + type check**

Run: `ruff check src/trading/ranking/ranker_train.py && mypy src/trading/ranking/ranker_train.py`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add src/trading/ranking/ranker_train.py tests/test_ranker_train.py
git commit -m "feat(ranker): pooled OOS statistic across folds (F-046)

Trade-weighted pool replaces the noise-prone mean-of-fold-Sharpes as the
honest basis for the promotion gate. Adds oos_sharpe_pooled / oos_hit_pooled /
n_oos_total / n_folds_positive / n_folds_total to TrainResult (defaulted)."
```

---

### Task 2: `_is_usable` gate + `RegistryRow` fields (Component B, registry side)

**Files:**
- Modify: `src/trading/store/model_registry.py` (constants ~33; `REGISTRY_COLUMNS` ~36–46; `RegistryRow` ~49–61; `_row_to_csv` ~82; `_csv_to_row` ~98; `_clears_floor`→`_is_usable` ~190; `_demote_subfloor` ~199; `register` ~212)
- Test: `tests/test_model_registry.py`

**Interfaces:**
- Consumes: `RegistryRow` fields `oos_sharpe` (now the pooled Sharpe), `n_oos_trades`, `n_folds_positive`, `n_folds_total`.
- Produces: `MIN_OOS_TRADES = 50`, `T_MIN = 2.0`, `MIN_USABLE_STREAK = 2`; `_is_usable(oos_sharpe: float, n_oos_trades: int, n_folds_positive: int, n_folds_total: int) -> bool`.
- Produces: `RegistryRow` gains `n_oos_trades: int = 0`, `n_folds_positive: int = 0`, `n_folds_total: int = 0` (defaults keep all other construction sites valid).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_model_registry.py` (import `_is_usable`, `MIN_OOS_TRADES`, `T_MIN` at top):

```python
def test_is_usable_requires_tstat_not_just_positive_sharpe() -> None:
    from trading.store.model_registry import _is_usable
    # positive Sharpe but tiny N -> t-stat below 2.0 -> not usable
    # t = 0.5 * sqrt(20/12) = 0.645 < 2.0
    assert not _is_usable(0.5, 20, 2, 2)
    # below the computability floor regardless of t
    assert not _is_usable(5.0, 49, 2, 2)
    # strong, broad, enough trades: t = 1.2 * sqrt(60/12) = 2.68 >= 2.0
    assert _is_usable(1.2, 60, 2, 2)


def test_is_usable_requires_fold_breadth() -> None:
    from trading.store.model_registry import _is_usable
    # t passes (t = 1.2 * sqrt(60/12) = 2.68) but only 1 of 4 folds positive
    assert not _is_usable(1.2, 60, 1, 4)
    # majority of folds positive
    assert _is_usable(1.2, 60, 2, 4)


def test_is_usable_nan_never_passes() -> None:
    from trading.store.model_registry import _is_usable
    assert not _is_usable(float("nan"), 100, 4, 4)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model_registry.py::test_is_usable_requires_tstat_not_just_positive_sharpe -v`
Expected: FAIL with `ImportError: cannot import name '_is_usable'`.

- [ ] **Step 3: Add constants and `_is_usable`**

In `src/trading/store/model_registry.py`, near `SHARPE_PROMOTION_FLOOR = 0.0` add:

```python
MIN_OOS_TRADES = 50  # computability floor: a t-stat on fewer trades is unstable
T_MIN = 2.0          # min t-statistic on pooled mean net return (statistical edge)
MIN_USABLE_STREAK = 2  # consecutive weekly usable verdicts before promotion (F-046)
```

Replace `_clears_floor` with:

```python
def _is_usable(
    oos_sharpe: float,
    n_oos_trades: int,
    n_folds_positive: int,
    n_folds_total: int,
) -> bool:
    """True iff the model clears the per-train usable gate (F-044/F-046).

    G1 statistical edge: at least `MIN_OOS_TRADES` pooled OOS trades and a
    t-statistic `oos_sharpe * sqrt(N/12) >= T_MIN` (a positive Sharpe alone is
    noise at small N). G2 breadth: positive in a majority of non-skipped folds.
    NaN never passes. Persistence (G3) is enforced by the caller via
    `ranker_eval_log`, not here.
    """
    if math.isnan(oos_sharpe) or n_oos_trades < MIN_OOS_TRADES:
        return False
    t_stat = oos_sharpe * math.sqrt(n_oos_trades / 12.0)
    if t_stat < T_MIN:
        return False
    if n_folds_total <= 0:
        return False
    return n_folds_positive >= math.ceil(n_folds_total / 2)
```

- [ ] **Step 4: Add `RegistryRow` fields + CSV plumbing**

Add to `REGISTRY_COLUMNS` (after `"n_features"`):
`"n_oos_trades", "n_folds_positive", "n_folds_total",`

Add to `RegistryRow` (after `n_features: int`, but note defaults must trail
non-default fields — place these at the end of the dataclass, after `notes`):

```python
    n_oos_trades: int = 0
    n_folds_positive: int = 0
    n_folds_total: int = 0
```

In `_row_to_csv` add:

```python
        "n_oos_trades": str(r.n_oos_trades),
        "n_folds_positive": str(r.n_folds_positive),
        "n_folds_total": str(r.n_folds_total),
```

In `_csv_to_row` add (legacy rows lack the columns → default 0, the safe
"never usable" value):

```python
        n_oos_trades=int(d.get("n_oos_trades", "0") or "0"),
        n_folds_positive=int(d.get("n_folds_positive", "0") or "0"),
        n_folds_total=int(d.get("n_folds_total", "0") or "0"),
```

- [ ] **Step 5: Swap `_demote_subfloor` and `register` onto `_is_usable`**

In `_demote_subfloor`, replace the `_clears_floor(r.oos_sharpe)` call with:

```python
        _with_active(r, False)
        if (r.active and not _is_usable(
            r.oos_sharpe, r.n_oos_trades, r.n_folds_positive, r.n_folds_total
        ))
        else r
```

In `register`, replace `if not promote or not _clears_floor(row.oos_sharpe):`
with:

```python
    eligible = _is_usable(
        row.oos_sharpe, row.n_oos_trades, row.n_folds_positive, row.n_folds_total
    )
    if not promote or not eligible:
```

(The challenger `improves` deadband comparison on `oos_sharpe` is unchanged.)

- [ ] **Step 6: Update the legacy floor tests**

In `tests/test_model_registry.py`, the two tests at ~200/209 asserting
`SHARPE_PROMOTION_FLOOR` behaviour now need usable rows. Update `_row` helper to
accept the new fields and update those two tests to construct rows that pass/fail
`_is_usable`:

```python
def _row(
    version: str,
    sharpe: float = 1.5,
    *,
    active_flag: bool = False,
    n_oos_trades: int = 60,
    n_folds_positive: int = 3,
    n_folds_total: int = 4,
) -> RegistryRow:
    return RegistryRow(
        version=version,
        trained_at=datetime.now(UTC).isoformat(),
        train_start="2023-05-01",
        train_end="2026-05-01",
        oos_sharpe=sharpe,
        oos_hit_rate=0.50,
        n_train_examples=100,
        n_features=20,
        path=f"models/ranker_{version}.pkl",
        active=active_flag,
        notes="test",
        n_oos_trades=n_oos_trades,
        n_folds_positive=n_folds_positive,
        n_folds_total=n_folds_total,
    )
```

Replace the two floor tests with usable-gate equivalents:

```python
def test_unusable_first_model_blocked(paths) -> None:
    # t = 0.5 * sqrt(20/12) = 0.645 < 2.0 -> not usable
    promoted = register(paths, row=_row("a", sharpe=0.5, n_oos_trades=20), promote=True)
    assert promoted is False
    assert active(paths) is None


def test_usable_first_model_promotes(paths) -> None:
    promoted = register(paths, row=_row("a", sharpe=1.5), promote=True)
    assert promoted is True
```

(If a `paths` fixture doesn't exist, use `_paths(tmp_path)` as the surrounding
tests do.)

- [ ] **Step 7: Run the registry suite**

Run: `uv run pytest tests/test_model_registry.py -v`
Expected: PASS (all, including CSV round-trip — add one if missing, below).

- [ ] **Step 8: Add a CSV round-trip + legacy test**

```python
def test_registry_csv_roundtrip_new_columns(tmp_path) -> None:
    paths = _paths(tmp_path)
    register(paths, row=_row("a", sharpe=1.5), promote=True)
    (got,) = all_rows(paths)
    assert got.n_oos_trades == 60
    assert got.n_folds_positive == 3
    assert got.n_folds_total == 4
```

Run: `uv run pytest tests/test_model_registry.py::test_registry_csv_roundtrip_new_columns -v`
Expected: PASS

- [ ] **Step 9: Lint + type check**

Run: `ruff check src/trading/store/model_registry.py && mypy src/trading/store/model_registry.py`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add src/trading/store/model_registry.py tests/test_model_registry.py
git commit -m "feat(registry): _is_usable gate (t-stat + breadth) replaces bare Sharpe floor

Promotion now needs a t-stat >= 2.0 over >= 50 pooled OOS trades AND a majority
of folds positive, not just oos_sharpe > 0. RegistryRow carries n_oos_trades /
n_folds_positive / n_folds_total (defaulted; legacy CSV rows read as 0)."
```

---

### Task 3: `ranker_eval_log` table + repo helpers (Component C, storage)

**Files:**
- Modify: `src/trading/store/migrations.py` (`CURRENT_VERSION` line 13; add `SCHEMA_V7` after `SCHEMA_V6`; add a `current < 7` branch in `run_migrations`)
- Modify: `src/trading/store/repo.py` (add `RankerEval` dataclass + `insert_ranker_eval` + `latest_ranker_eval`)
- Test: `tests/test_repo.py` (or `tests/test_migrations.py` if that is where schema tests live — match the existing file)

**Interfaces:**
- Produces: table `ranker_eval_log(as_of TEXT PK, pooled_sharpe REAL, pooled_hit REAL, n_oos INTEGER, n_folds_pos INTEGER, n_folds_total INTEGER, usable INTEGER, note TEXT, created_at TEXT)`.
- Produces: `RankerEval` dataclass; `insert_ranker_eval(conn, ev: RankerEval) -> None` (INSERT OR REPLACE on `as_of`); `latest_ranker_eval(conn) -> RankerEval | None`.

- [ ] **Step 1: Write the failing test**

Add to the repo/migrations test module:

```python
def test_ranker_eval_log_insert_and_latest(tmp_path) -> None:
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations
    from trading.store.repo import RankerEval, insert_ranker_eval, latest_ranker_eval

    with get_conn(tmp_path / "t.db") as conn:
        run_migrations(conn)
        assert latest_ranker_eval(conn) is None
        insert_ranker_eval(conn, RankerEval(
            as_of="2026-06-21", pooled_sharpe=-0.3, pooled_hit=0.27,
            n_oos=38, n_folds_pos=2, n_folds_total=7, usable=False,
            note="not usable", created_at="2026-06-21T00:00:00Z",
        ))
        insert_ranker_eval(conn, RankerEval(
            as_of="2026-06-28", pooled_sharpe=1.2, pooled_hit=0.55,
            n_oos=60, n_folds_pos=4, n_folds_total=6, usable=True,
            note="usable", created_at="2026-06-28T00:00:00Z",
        ))
        latest = latest_ranker_eval(conn)
        assert latest is not None
        assert latest.as_of == "2026-06-28"
        assert latest.usable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -k ranker_eval_log -v`
Expected: FAIL (`ImportError` / `no such table: ranker_eval_log`).

- [ ] **Step 3: Add the migration**

In `src/trading/store/migrations.py` bump `CURRENT_VERSION = 7` and after
`SCHEMA_V6` add:

```python
# v7 (F-044/F-046): dated weekly "is the ranker usable yet?" verdict. One row per
# Sunday eval. `usable` is the pooled-gate result; persistence (2 consecutive
# usable verdicts before promotion) reads this table. PK as_of makes the Sunday
# re-run idempotent.
SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS ranker_eval_log (
  as_of         TEXT NOT NULL,
  pooled_sharpe REAL,
  pooled_hit    REAL,
  n_oos         INTEGER NOT NULL,
  n_folds_pos   INTEGER NOT NULL,
  n_folds_total INTEGER NOT NULL,
  usable        INTEGER NOT NULL,
  note          TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (as_of)
);
"""
```

In `run_migrations`, after the `current < 6` block add:

```python
    if current < 7:
        conn.executescript(SCHEMA_V7)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (7, datetime.now(UTC).isoformat()),
        )
```

- [ ] **Step 4: Add the repo helpers**

In `src/trading/store/repo.py` add (near the other dataclasses / CRUD):

```python
@dataclass(frozen=True)
class RankerEval:
    """A dated weekly verdict on whether the shadow ranker is usable yet."""

    as_of: str
    pooled_sharpe: float | None
    pooled_hit: float | None
    n_oos: int
    n_folds_pos: int
    n_folds_total: int
    usable: bool
    note: str | None
    created_at: str


def insert_ranker_eval(conn: sqlite3.Connection, ev: RankerEval) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ranker_eval_log (
          as_of, pooled_sharpe, pooled_hit, n_oos, n_folds_pos,
          n_folds_total, usable, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ev.as_of, ev.pooled_sharpe, ev.pooled_hit, ev.n_oos, ev.n_folds_pos,
            ev.n_folds_total, 1 if ev.usable else 0, ev.note, ev.created_at,
        ),
    )


def latest_ranker_eval(conn: sqlite3.Connection) -> RankerEval | None:
    row = conn.execute(
        "SELECT * FROM ranker_eval_log ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return RankerEval(
        as_of=row["as_of"],
        pooled_sharpe=row["pooled_sharpe"],
        pooled_hit=row["pooled_hit"],
        n_oos=int(row["n_oos"]),
        n_folds_pos=int(row["n_folds_pos"]),
        n_folds_total=int(row["n_folds_total"]),
        usable=bool(row["usable"]),
        note=row["note"],
        created_at=row["created_at"],
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest -k ranker_eval_log -v`
Expected: PASS

- [ ] **Step 6: Lint + type check**

Run: `ruff check src/trading/store/migrations.py src/trading/store/repo.py && mypy src/trading/store/repo.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/trading/store/migrations.py src/trading/store/repo.py tests/
git commit -m "feat(store): ranker_eval_log table + RankerEval repo helpers (v7)

Dated weekly usable-verdict log; latest_ranker_eval feeds the 2-consecutive
persistence gate. PK as_of keeps Sunday re-runs idempotent."
```

---

### Task 4: weekly_train verdict + streak-gated promotion + cli plumbing (Components B-G3, C wiring)

**Files:**
- Modify: `src/trading/jobs/weekly_train.py` (`RetrainOutcome` ~132–139; `_step_retrain` ~411–470; `render_weekly_review` ~308; `_slack_body` ~478)
- Modify: `src/trading/cli.py` (`train_ranker` printout ~2044, `RegistryRow` construction ~2053)
- Test: `tests/test_jobs_weekly_train.py`

**Interfaces:**
- Consumes: `TrainResult.oos_sharpe_pooled/oos_hit_pooled/n_oos_total/n_folds_positive/n_folds_total` (Task 1); `_is_usable`, `MIN_USABLE_STREAK` (Task 2); `RankerEval`, `insert_ranker_eval`, `latest_ranker_eval` (Task 3).
- Produces: `RetrainOutcome` gains `pooled_sharpe: float | None`, `n_oos: int | None`, `usable: bool`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_jobs_weekly_train.py`:

Follow the existing conventions in this module: the `paths` fixture, a
`TrainResult` stub built like the one at line ~450 in
`test_retrain_success_registers_and_saves_model`, and a monkeypatched
`load_training_inputs`. `_step_retrain` takes a real `conn`.

```python
def test_first_usable_verdict_records_but_does_not_promote(paths, monkeypatch) -> None:
    """G3 persistence: the first usable week records a verdict but must NOT
    promote (needs 2 consecutive). A second usable week promotes."""
    import lightgbm as lgb

    from trading.jobs import weekly_train as wt
    from trading.ranking.ranker_features import FEATURE_NAMES
    from trading.ranking.ranker_io import TrainingInputs
    from trading.ranking.ranker_train import TrainResult
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations
    from trading.store.repo import latest_ranker_eval

    monkeypatch.setattr(
        wt, "load_training_inputs",
        lambda p, c: TrainingInputs(
            enriched={"RVNL": pd.DataFrame({"close": [1.0]})},
            macro_history=pd.DataFrame(), sentiment_lookup={},
            negative_news_lookup={},
        ),
    )
    usable_stub = TrainResult(
        folds=(), final_model=lgb.LGBMClassifier(),
        final_train_start=pd.Timestamp("2023-06-14"),
        final_train_end=pd.Timestamp("2026-06-14"),
        n_final_examples=42,
        oos_sharpe_mean=float("nan"), oos_hit_rate_mean=float("nan"),
        feature_names=FEATURE_NAMES,
        oos_sharpe_pooled=1.5, oos_hit_pooled=0.55,
        n_oos_total=60, n_folds_positive=4, n_folds_total=6,
    )
    monkeypatch.setattr(wt, "train_walkforward", lambda **_kw: usable_stub)

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        out = wt._step_retrain(paths, conn, date(2026, 6, 21), skip_train=False)
        assert out.usable is True
        assert out.promoted is False          # first usable -> records, no promote
        assert latest_ranker_eval(conn).as_of == "2026-06-21"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jobs_weekly_train.py::test_first_usable_verdict_records_but_does_not_promote -v`
Expected: FAIL (`RetrainOutcome` has no `usable`; no verdict written).

- [ ] **Step 3: Extend `RetrainOutcome`**

```python
@dataclass(frozen=True)
class RetrainOutcome:
    ran: bool
    skip_reason: str | None
    examples: int | None
    oos_sharpe: float | None
    promoted: bool
    model_path: str | None
    pooled_sharpe: float | None = None
    n_oos: int | None = None
    usable: bool = False
```

- [ ] **Step 4: Rewrite the tail of `_step_retrain`**

Replace from the `row = RegistryRow(...)` construction through the `return`
with:

```python
    usable = _is_usable(
        result.oos_sharpe_pooled,
        result.n_oos_total,
        result.n_folds_positive,
        result.n_folds_total,
    )
    # Persistence (G3): read the PREVIOUS verdict before writing this week's.
    prev = latest_ranker_eval(conn)
    streak_ok = usable and prev is not None and prev.usable  # 2 consecutive
    note = (
        f"pooled Sharpe {result.oos_sharpe_pooled:.2f} over N={result.n_oos_total}, "
        f"hit {result.oos_hit_pooled:.2f}, folds {result.n_folds_positive}/"
        f"{result.n_folds_total} -> {'USABLE' if usable else 'not usable'}"
    )
    insert_ranker_eval(conn, RankerEval(
        as_of=end_iso,
        pooled_sharpe=result.oos_sharpe_pooled,
        pooled_hit=result.oos_hit_pooled,
        n_oos=result.n_oos_total,
        n_folds_pos=result.n_folds_positive,
        n_folds_total=result.n_folds_total,
        usable=usable,
        note=note,
        created_at=datetime.now(UTC).isoformat(),
    ))

    row = RegistryRow(
        version=end_iso,
        trained_at=datetime.now(UTC).isoformat(),
        train_start=str(result.final_train_start.date()),
        train_end=end_iso,
        oos_sharpe=result.oos_sharpe_pooled,
        oos_hit_rate=result.oos_hit_pooled,
        n_train_examples=result.n_final_examples,
        n_features=len(FEATURE_NAMES),
        path=pkl_rel,
        active=False,
        notes="weekly_train",
        n_oos_trades=result.n_oos_total,
        n_folds_positive=result.n_folds_positive,
        n_folds_total=result.n_folds_total,
    )
    promoted = register(paths, row=row, promote=streak_ok)
    return RetrainOutcome(
        True, None, result.n_final_examples, result.oos_sharpe_pooled, promoted,
        pkl_rel, pooled_sharpe=result.oos_sharpe_pooled, n_oos=result.n_oos_total,
        usable=usable,
    )
```

Add imports at the top of `weekly_train.py`: `_is_usable` from
`trading.store.model_registry`; `RankerEval, insert_ranker_eval,
latest_ranker_eval` from `trading.store.repo`.

- [ ] **Step 5: Add the verdict line to the review**

In `render_weekly_review`, where the retrain block renders, add a line built
from `retrain`:

```python
    verdict = (
        f"Ranker {'USABLE' if retrain.usable else 'NOT usable'}: pooled OOS Sharpe "
        f"{retrain.pooled_sharpe:.2f} over N={retrain.n_oos} — "
        f"{'promoting' if retrain.promoted else 'staying silent'}."
        if retrain.ran and retrain.pooled_sharpe is not None
        else "Ranker: no retrain this week."
    )
```

and include `verdict` in the rendered markdown (append to the retrain section).
Mirror a one-line version into `_slack_body`.

- [ ] **Step 6: Update `cli.train_ranker`**

In `src/trading/cli.py`, change the summary print to use pooled fields and the
`RegistryRow` construction to pass the new fields:

```python
    console.print(
        f"OOS Sharpe (pooled): {result.oos_sharpe_pooled:.3f} | "
        f"hit (pooled): {result.oos_hit_pooled:.3f} | "
        f"N={result.n_oos_total} folds {result.n_folds_positive}/{result.n_folds_total} | "
        f"final examples: {result.n_final_examples}"
    )
```

and in the `RegistryRow(...)` there set `oos_sharpe=result.oos_sharpe_pooled`,
`oos_hit_rate=result.oos_hit_pooled`, and add
`n_oos_trades=result.n_oos_total, n_folds_positive=result.n_folds_positive,
n_folds_total=result.n_folds_total`. (Leave the markdown report strings at
~2076 consistent — switch them to the pooled fields too.)

- [ ] **Step 7: Run test to verify it passes + the weekly suite**

Run: `uv run pytest tests/test_jobs_weekly_train.py tests/test_cli_ranker.py -v`
Expected: PASS. Fix any `TrainResult(...)` construction in
`tests/test_jobs_weekly_train.py:456` to include the new pooled fields if it
asserts on them (defaults cover the rest).

- [ ] **Step 8: Full suite + lint + types**

Run: `uv run pytest && ruff check . && mypy src/`
Expected: all green/clean.

- [ ] **Step 9: Commit**

```bash
git add src/trading/jobs/weekly_train.py src/trading/cli.py tests/
git commit -m "feat(weekly_train): dated usable-verdict + 2-consecutive promotion gate

Each Sunday writes a ranker_eval_log verdict from the pooled stat, then promotes
only when this week AND last week are both usable (G3 persistence). Registry row
and cli now carry the pooled Sharpe and fold breadth."
```

---

### Task 5: Findings hygiene — re-cite F-045→F-044, open F-046 (Component E)

**Files:**
- Modify: `src/trading/ranking/ranker_train.py` (the `(F-045)` comments at the `OOS_TOP_K` / `MIN_THRESHOLD_TRADES` / `_FoldXY` / `_evaluate_fold_oos` docstrings)
- Modify: `src/trading/ranking/ranker_labels.py`, `src/trading/backtest/forward_return.py` (any `(F-045)` magnitude/threshold comments)
- Modify: `docs/architecture/FINDINGS.md` (re-scope F-044 ~1139; add F-046; bump the open-count footer)

**Interfaces:** none (comments + docs only).

- [ ] **Step 1: Re-cite the mis-numbered comments**

Search and correct only the comments where F-045 is used for the
magnitude-aware / abstention / top-K work (NOT the genuine F-045 layering
references in `docs/architecture/`):

Run: `git grep -n "F-045" src/trading/ranking/ src/trading/backtest/forward_return.py`
For each magnitude/threshold/top-K comment, change `(F-045)` → `(F-044)`.
Leave `docs/architecture/01-architecture.md` and the FINDINGS layering entry
untouched (those are the real F-045).

- [ ] **Step 2: Add F-046 and re-scope F-044 in FINDINGS.md**

Under the F-044 entry (~line 1139), append a status note:

```markdown
- **Update 2026-06-25 (partial fix — shadow-ranker honest gate):** the learner
  still trains on the synthetic exit-replay (live book has only 3 closed trades,
  ~a year from a trainable realised set), so the *realised-outcome training join*
  remains open (its own follow-up spec). What landed: the ranker is now an
  honest **silent** shadow learner — pooled OOS statistic, a usable-gate
  (t-stat ≥ 2.0 over ≥ 50 pooled trades AND majority-of-folds breadth), a dated
  weekly `ranker_eval_log` verdict, and a 2-consecutive-week persistence gate.
  See `docs/superpowers/specs/2026-06-25-shadow-ranker-honest-gate-design.md`.
```

Add a new finding F-046 in the open-findings table and detail section:

```markdown
### F-046 — Promotion gated on mean-of-fold Sharpes (`VULN`, Med) — ✅ Fixed 2026-06-25
The F-043 floor gated promotion on `oos_sharpe_mean`, the arithmetic mean of
per-fold Sharpes. With the abstention + top-K cap, folds shrink to 1–15 trades,
so a single lucky small fold (e.g. +83.5 on 3 trades) inflates the mean above
the floor and **spuriously promotes a no-edge model** (verified: pooled hit-rate
0.27 yet reported mean +2.57). Fixed by a trade-weighted **pooled** statistic and
`_is_usable` (t-stat ≥ 2.0 over ≥ 50 trades + majority-fold breadth + 2-week
persistence) replacing the bare floor.
```

Update the counts footer (open/fixed) accordingly.

- [ ] **Step 3: Verify nothing else cites the old number wrongly**

Run: `git grep -n "F-045" src/ | grep -i "magnitude\|threshold\|top-k\|abstain\|expectancy"`
Expected: no matches remain.

- [ ] **Step 4: Full suite (docs/comment-only, sanity)**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit and push**

```bash
git add src/trading/ranking/ranker_train.py src/trading/ranking/ranker_labels.py \
        src/trading/backtest/forward_return.py docs/architecture/FINDINGS.md
git commit -m "docs(findings): re-cite F-045->F-044 comments; open+fix F-046

The magnitude-aware/abstention comments referenced F-045 (a layering debt) by
mistake; they belong to F-044 (model quality). F-046 records the spurious
mean-of-fold-Sharpes promotion risk this work fixed."
git push
```

---

## Self-Review

**Spec coverage:**
- Component A (pooled statistic) → Task 1. ✓
- Component B (usable gate: G1 t-stat, G2 breadth, G3 persistence) → Task 2 (G1/G2 + registry), Task 4 (G3 in weekly_train). ✓
- Component C (ranker_eval_log + weekly verdict) → Task 3 (storage), Task 4 (write + render). ✓
- Component E (findings hygiene) → Task 5. ✓
- Component D (realised-outcome join) → explicitly deferred per spec; no task. ✓

**Type consistency:** `_is_usable(oos_sharpe, n_oos_trades, n_folds_positive, n_folds_total)` signature identical across Task 2 (def), Task 4 (call). `TrainResult` pooled field names (`oos_sharpe_pooled`, `oos_hit_pooled`, `n_oos_total`, `n_folds_positive`, `n_folds_total`) consistent Task 1↔2↔4. `RankerEval` field names consistent Task 3↔4. `RegistryRow` new fields (`n_oos_trades`, `n_folds_positive`, `n_folds_total`) consistent Task 2↔4.

**Placeholder scan:** no TBD/TODO; every code step shows full code. Test helper stubs in Task 4 (`_make_train_result`, `_fake_inputs`) are described with exact required behaviour, since they depend on this module's existing fixtures which the implementer can see.

**Build-green invariant:** Tasks 1–3 add only defaulted fields / new symbols, so the build and existing call sites stay valid after each; Task 4 switches the wiring onto the pooled fields; Task 5 is comments/docs. Order A→B→C→D-wiring→E is safe.
