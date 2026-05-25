"""Phase 16 model registry — single CSV at `models/registry.csv`.

One row per training run. Exactly one row may have `active=true`. Promotion
is gated by a 0.05 walk-forward Sharpe deadband on the active row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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
    model: "lgb.LGBMClassifier"
    feature_names: tuple[str, ...]


class RegistryFeatureMismatch(RuntimeError):
    """Raised when a loaded model's feature names diverge from current FEATURE_NAMES."""


def all_rows(paths: "Paths") -> list[RegistryRow]:
    raise NotImplementedError


def active(paths: "Paths") -> ActiveModel | None:
    raise NotImplementedError


def register(paths: "Paths", *, row: RegistryRow, promote: bool) -> bool:
    """Write `row` to registry.csv. Returns True iff this row became active."""
    raise NotImplementedError


def save_model(path: Path, model: "lgb.LGBMClassifier", feature_names: tuple[str, ...]) -> None:
    raise NotImplementedError
