"""Phase 16 model registry — single CSV at `models/registry.csv`.

One row per training run. Exactly one row may have `active=true`. Promotion
is gated by a 0.05 walk-forward Sharpe deadband on the active row. Atomic
write via temp-file + os.replace; pickle includes `feature_names` so
inference can detect a stale model after FEATURE_NAMES evolves.
"""

from __future__ import annotations

import csv
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import joblib

if TYPE_CHECKING:
    import lightgbm as lgb

    from trading.config import Paths

REGISTRY_FILENAME = "registry.csv"
SHARPE_PROMOTION_DEADBAND = 0.05
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


@dataclass(frozen=True)
class ActiveModel:
    row: RegistryRow
    model: lgb.LGBMClassifier
    feature_names: tuple[str, ...]


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
    return ActiveModel(
        row=r,
        model=payload["model"],
        feature_names=tuple(payload["feature_names"]),
    )


def _with_active(row: RegistryRow, active_flag: bool) -> RegistryRow:
    return RegistryRow(
        version=row.version,
        trained_at=row.trained_at,
        train_start=row.train_start,
        train_end=row.train_end,
        oos_sharpe=row.oos_sharpe,
        oos_hit_rate=row.oos_hit_rate,
        n_train_examples=row.n_train_examples,
        n_features=row.n_features,
        path=row.path,
        active=active_flag,
        notes=row.notes,
    )


def register(paths: Paths, *, row: RegistryRow, promote: bool) -> bool:
    """Append `row` to registry.csv. Returns True iff this row became active.

    Promotion logic:
      - If `promote` is False → always write inactive.
      - If `promote` is True and there's no current active row → activate
        (unless `oos_sharpe` is NaN — never activate an unmeasured model).
      - If `promote` is True and there is one → activate iff
        `row.oos_sharpe > current.oos_sharpe + SHARPE_PROMOTION_DEADBAND`.
        NaN comparisons are False, so NaN sharpe never promotes.
      - When activating, the previous active row is flipped to inactive.
    """
    existing = all_rows(paths)
    if not promote:
        existing.append(_with_active(row, False))
        _write_all_rows(paths, existing)
        return False

    current_active = next((r for r in existing if r.active), None)
    if current_active is None:
        if math.isnan(row.oos_sharpe):
            existing.append(_with_active(row, False))
            _write_all_rows(paths, existing)
            return False
        existing.append(_with_active(row, True))
        _write_all_rows(paths, existing)
        return True

    improves = (
        not math.isnan(row.oos_sharpe)
        and row.oos_sharpe > current_active.oos_sharpe + SHARPE_PROMOTION_DEADBAND
    )
    if not improves:
        existing.append(_with_active(row, False))
        _write_all_rows(paths, existing)
        return False

    new_rows = [_with_active(r, False) if r.active else r for r in existing]
    new_rows.append(_with_active(row, True))
    _write_all_rows(paths, new_rows)
    return True


def save_model(
    path: Path,
    model: lgb.LGBMClassifier,
    feature_names: tuple[str, ...],
) -> None:
    """Persist model + feature_names via joblib. Atomic write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".pkl", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            joblib.dump({"model": model, "feature_names": list(feature_names)}, fh)
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
