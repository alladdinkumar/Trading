"""Phase 16 model registry — single CSV at `models/registry.csv`.

One row per training run. Exactly one row may have `active=true`. Promotion
is gated by an absolute OOS-Sharpe floor (F-043 — a model must show a positive
out-of-sample edge to ever be served) *and* a 0.05 walk-forward Sharpe deadband
over the active row. Atomic write via temp-file + os.replace; pickle includes
`feature_names` so inference can detect a stale model after FEATURE_NAMES evolves.
"""

from __future__ import annotations

import csv
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import joblib

if TYPE_CHECKING:
    import lightgbm as lgb

    from trading.config import Paths

REGISTRY_FILENAME = "registry.csv"
SHARPE_PROMOTION_DEADBAND = 0.05
# F-043: absolute floor a model must clear to be promoted *at all* — distinct from
# the deadband (which only governs beating an incumbent). A model with a
# non-positive OOS Sharpe has no demonstrated out-of-sample edge, so the live EV
# planner must keep its `p_win=prior` rather than consume an unproven score.
SHARPE_PROMOTION_FLOOR = 0.0
# F-046: the honest usable-gate that replaces the bare positive-Sharpe floor. A
# positive pooled Sharpe is noise at small N (Sharpe SE ≈ √((1+½·SR²)/N)), so we
# gate on a t-statistic over enough pooled OOS trades, plus fold breadth.
MIN_OOS_TRADES = 50  # computability floor: a t-stat on fewer trades is unstable
T_MIN = 2.0  # min t-statistic on the pooled mean net return (statistical edge)
MIN_USABLE_STREAK = 2  # consecutive weekly usable verdicts before promotion (G3)
REGISTRY_COLUMNS: tuple[str, ...] = (
    "version",
    "trained_at",
    "train_start",
    "train_end",
    "oos_sharpe",
    "oos_hit_rate",
    "n_train_examples",
    "n_features",
    "path",
    "active",
    "notes",
    "n_oos_trades",
    "n_folds_positive",
    "n_folds_total",
)


@dataclass(frozen=True)
class RegistryRow:
    version: str
    trained_at: str
    train_start: str
    train_end: str
    oos_sharpe: float
    oos_hit_rate: float
    n_train_examples: int
    n_features: int
    path: str
    active: bool
    notes: str
    # F-046 usable-gate inputs. Defaulted so legacy construction sites and CSV
    # rows stay valid; 0 is the safe "never usable" default.
    n_oos_trades: int = 0
    n_folds_positive: int = 0
    n_folds_total: int = 0


@dataclass(frozen=True)
class ActiveModel:
    row: RegistryRow
    model: lgb.LGBMClassifier
    feature_names: tuple[str, ...]
    # Calibrated act/pass probability threshold (F-045). None for models saved
    # before thresholds existed → inference falls back to top-K-only selection.
    threshold: float | None = None


class RegistryFeatureMismatch(RuntimeError):  # noqa: N818 — domain term, not an "Error"
    """Raised when a loaded model's feature names diverge from current FEATURE_NAMES."""


def _registry_path(paths: Paths) -> Path:
    return paths.models_dir / REGISTRY_FILENAME


def _row_to_csv(r: RegistryRow) -> dict[str, str]:
    return {
        "version": r.version,
        "trained_at": r.trained_at,
        "train_start": r.train_start,
        "train_end": r.train_end,
        "oos_sharpe": "" if math.isnan(r.oos_sharpe) else f"{r.oos_sharpe:.6f}",
        "oos_hit_rate": "" if math.isnan(r.oos_hit_rate) else f"{r.oos_hit_rate:.6f}",
        "n_train_examples": str(r.n_train_examples),
        "n_features": str(r.n_features),
        "path": r.path,
        "active": "true" if r.active else "false",
        "notes": r.notes,
        "n_oos_trades": str(r.n_oos_trades),
        "n_folds_positive": str(r.n_folds_positive),
        "n_folds_total": str(r.n_folds_total),
    }


def _csv_to_row(d: dict[str, str]) -> RegistryRow:
    def _f(s: str) -> float:
        return math.nan if s == "" else float(s)

    return RegistryRow(
        version=d["version"],
        trained_at=d["trained_at"],
        train_start=d["train_start"],
        train_end=d["train_end"],
        oos_sharpe=_f(d["oos_sharpe"]),
        oos_hit_rate=_f(d["oos_hit_rate"]),
        n_train_examples=int(d["n_train_examples"]),
        n_features=int(d["n_features"]),
        path=d["path"],
        active=d["active"].lower() == "true",
        notes=d["notes"],
        n_oos_trades=int(d.get("n_oos_trades", "0") or "0"),
        n_folds_positive=int(d.get("n_folds_positive", "0") or "0"),
        n_folds_total=int(d.get("n_folds_total", "0") or "0"),
    )


def all_rows(paths: Paths) -> list[RegistryRow]:
    p = _registry_path(paths)
    if not p.is_file():
        return []
    with p.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return [_csv_to_row(r) for r in reader]


def has_row_for_train_end(paths: Paths, train_end: str) -> bool:
    """True iff any registry row was trained on a window ending `train_end`.

    Weekly idempotency guard: a Sunday re-run of weekly_train must not
    append a duplicate training row for the same window.
    """
    return any(r.train_end == train_end for r in all_rows(paths))


def _write_all_rows(paths: Paths, rows: list[RegistryRow]) -> None:
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="registry-", suffix=".csv", dir=str(paths.models_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(REGISTRY_COLUMNS))
            writer.writeheader()
            for r in rows:
                writer.writerow(_row_to_csv(r))
        os.replace(tmp_name, _registry_path(paths))
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def active(paths: Paths) -> ActiveModel | None:
    rows = all_rows(paths)
    active_rows = [r for r in rows if r.active]
    if not active_rows:
        return None
    if len(active_rows) > 1:
        raise RuntimeError(
            f"registry.csv invariant violated: {len(active_rows)} rows with active=true"
        )
    r = active_rows[0]
    pkl_path = paths.project_root / r.path
    if not pkl_path.is_file():
        return None
    payload = joblib.load(pkl_path)
    threshold = payload.get("threshold")
    return ActiveModel(
        row=r,
        model=payload["model"],
        feature_names=tuple(payload["feature_names"]),
        threshold=None if threshold is None else float(threshold),
    )


def _with_active(row: RegistryRow, active_flag: bool) -> RegistryRow:
    # `replace` preserves all fields (incl. the F-046 gate inputs) — rebuilding
    # field-by-field silently dropped the new columns.
    return replace(row, active=active_flag)


def _is_usable(
    oos_sharpe: float,
    n_oos_trades: int,
    n_folds_positive: int,
    n_folds_total: int,
) -> bool:
    """True iff the model clears the per-train usable gate (F-044/F-046).

    G1 statistical edge: at least `MIN_OOS_TRADES` pooled OOS trades and a
    t-statistic `oos_sharpe * sqrt(N/12) >= T_MIN` — a positive Sharpe alone is
    noise at small N. G2 breadth: positive in a majority of non-skipped folds.
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


def _row_is_usable(r: RegistryRow) -> bool:
    return _is_usable(r.oos_sharpe, r.n_oos_trades, r.n_folds_positive, r.n_folds_total)


def _demote_subfloor(rows: list[RegistryRow]) -> list[RegistryRow]:
    """Flip any active row that no longer clears the usable gate back to inactive.

    Guards against a model promoted *before* the gate existed (e.g. the live
    −1.49 one) staying active and being served — the invariant is
    ``active ⟹ usable``. Falls back to cold-start when none qualifies.
    """
    return [
        _with_active(r, False) if (r.active and not _row_is_usable(r)) else r
        for r in rows
    ]


def register(paths: Paths, *, row: RegistryRow, promote: bool) -> bool:
    """Append `row` to registry.csv. Returns True iff this row became active.

    Promotion logic:
      - If `promote` is False → always write inactive.
      - A row must clear the `_is_usable` gate (F-046: t-stat ≥ T_MIN over ≥
        MIN_OOS_TRADES pooled trades AND majority-fold breadth) before any
        promotion is considered; NaN / sub-threshold rows never promote.
      - If `promote` is True, the row is usable, and there's no current active
        row → activate.
      - If `promote` is True, the row is usable, and there is one → activate iff
        `row.oos_sharpe > current.oos_sharpe + SHARPE_PROMOTION_DEADBAND`.
      - Whenever this call does not promote `row`, any active row that itself no
        longer clears the gate is demoted (so a pre-gate unusable model can't
        keep being served). When activating, the previous active row is flipped to
        inactive.
    """
    existing = all_rows(paths)
    if not promote or not _row_is_usable(row):
        rows_out = _demote_subfloor(existing)
        rows_out.append(_with_active(row, False))
        _write_all_rows(paths, rows_out)
        return False

    current_active = next((r for r in existing if r.active), None)
    if current_active is None:
        existing.append(_with_active(row, True))
        _write_all_rows(paths, existing)
        return True

    improves = row.oos_sharpe > current_active.oos_sharpe + SHARPE_PROMOTION_DEADBAND
    if not improves:
        rows_out = _demote_subfloor(existing)
        rows_out.append(_with_active(row, False))
        _write_all_rows(paths, rows_out)
        return False

    new_rows = [_with_active(r, False) if r.active else r for r in existing]
    new_rows.append(_with_active(row, True))
    _write_all_rows(paths, new_rows)
    return True


def save_model(
    path: Path,
    model: lgb.LGBMClassifier,
    feature_names: tuple[str, ...],
    *,
    threshold: float | None = None,
) -> None:
    """Persist model + feature_names (+ calibrated threshold) via joblib. Atomic write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".pkl", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            joblib.dump(
                {
                    "model": model,
                    "feature_names": list(feature_names),
                    "threshold": threshold,
                },
                fh,
            )
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
