"""Read-boundary validation for broker/quote snapshot JSON (F-002).

`/kite-snapshot` and `/kite-quotes-snapshot` write JSON that the readers in
`kite_snapshot.py` / `quotes_snapshot.py` splat straight into the frozen
dataclasses in `trading.data.kite`. Frozen dataclasses don't type-check, so a
malformed write — wrong `exchange`, a string where a float belongs, a missing
field, an extra key from a drifted contract — would either crash with a cryptic
`TypeError`/`KeyError` or pass silently and corrupt downstream logic (e.g. a
wrong-exchange quote driving an MTM exit).

`validate_rows`/`validate_row` validate a raw row against its target dataclass
(the single source of truth for the contract) and raise `SnapshotSchemaError`
with the offending file, row index, field, and a remediation hint. We drive the
check off `dataclasses.fields` + resolved type hints so the validator never
drifts from the dataclass definition.
"""

from __future__ import annotations

import types
from dataclasses import MISSING, fields
from typing import Any, Union, get_args, get_origin, get_type_hints

# Kite trading exchanges (equity + derivative + currency/commodity segments).
# A row whose `exchange` falls outside this set is almost always a bad write,
# and silently trusting it can drive a wrong-instrument MTM/exit.
_ALLOWED_EXCHANGES: frozenset[str] = frozenset(
    {"NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX", "NCO"}
)


class SnapshotSchemaError(ValueError):
    """A snapshot JSON row violated its dataclass contract."""


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Strip `X | None` / `Optional[X]` → `(X, is_optional)`."""
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        optional = len(non_none) != len(args)
        # The snapshot dataclasses only ever union a single concrete type w/ None.
        return non_none[0], optional
    return annotation, False


def _kind(annotation: Any) -> tuple[str, Any]:
    """Classify an annotation → ("list", elem_type) or ("scalar", base_type)."""
    if get_origin(annotation) is list:
        args = get_args(annotation)
        return "list", (args[0] if args else Any)
    return "scalar", annotation


def _check_scalar(base: Any, value: Any, *, where: str) -> Any:
    """Validate/coerce a single scalar against `base`, or raise."""
    target = get_origin(base) or base
    if target is float:
        # bool is a subtype of int — reject so True/False can't masquerade as 1/0.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SnapshotSchemaError(
                f"{where} expected a number, got {type(value).__name__} {value!r}."
            )
        return float(value)
    if target is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SnapshotSchemaError(
                f"{where} expected an integer, got {type(value).__name__} {value!r}."
            )
        return value
    if target is str:
        if not isinstance(value, str):
            raise SnapshotSchemaError(
                f"{where} expected a string, got {type(value).__name__} {value!r}."
            )
        return value
    if target is dict:
        if not isinstance(value, dict):
            raise SnapshotSchemaError(
                f"{where} expected an object, got {type(value).__name__}."
            )
        return value
    # `Any` (e.g. list[dict[str, Any]] element) or an unmodelled type — accept.
    return value


def validate_row(cls: type, row: Any, *, source: str, index: int = 0) -> Any:
    """Validate one raw dict against dataclass `cls`; return an instance or raise.

    `source` is the file the row came from and `index` its position — both are
    woven into every error so a bad write points straight at what to re-run.
    """
    label = f"{source}[{index}]"
    if not isinstance(row, dict):
        raise SnapshotSchemaError(
            f"{label}: expected a JSON object, got {type(row).__name__}. "
            f"Re-run the skill that writes {source}."
        )

    hints = get_type_hints(cls)
    flds = fields(cls)
    names = {f.name for f in flds}
    extra = sorted(set(row) - names)
    if extra:
        raise SnapshotSchemaError(
            f"{label}: unexpected field(s) {extra} for {cls.__name__}; "
            f"expected {sorted(names)}. The payload at {source} doesn't match the "
            f"broker contract — re-run the skill that wrote it."
        )

    kwargs: dict[str, Any] = {}
    for f in flds:
        inner, optional = _unwrap_optional(hints[f.name])
        kind, base = _kind(inner)
        has_default = f.default is not MISSING or f.default_factory is not MISSING
        where = f"{label}.{f.name}"

        if f.name not in row:
            if has_default:
                continue  # let the dataclass supply its default
            raise SnapshotSchemaError(
                f"{label}: missing required field {f.name!r} for {cls.__name__}. "
                f"Re-run the skill that writes {source}."
            )

        value = row[f.name]
        if value is None:
            if optional or has_default:
                kwargs[f.name] = None
                continue
            raise SnapshotSchemaError(f"{where} must not be null for {cls.__name__}.")

        if kind == "list":
            if not isinstance(value, list):
                raise SnapshotSchemaError(
                    f"{where} expected a list, got {type(value).__name__}."
                )
            kwargs[f.name] = [
                _check_scalar(base, v, where=f"{where}[{i}]") for i, v in enumerate(value)
            ]
        else:
            coerced = _check_scalar(base, value, where=where)
            if f.name == "exchange" and coerced not in _ALLOWED_EXCHANGES:
                raise SnapshotSchemaError(
                    f"{where}: invalid exchange {coerced!r}; allowed "
                    f"{sorted(_ALLOWED_EXCHANGES)}. A wrong-exchange row at {source} "
                    f"could drive a bad MTM/exit — re-run the skill that wrote it."
                )
            kwargs[f.name] = coerced

    return cls(**kwargs)


def validate_rows(cls: type, rows: Any, *, source: str) -> list[Any]:
    """Validate a JSON array of rows against `cls`; return instances or raise."""
    if not isinstance(rows, list):
        raise SnapshotSchemaError(
            f"{source}: expected a JSON array of {cls.__name__} rows, got "
            f"{type(rows).__name__}. Re-run the skill that writes {source}."
        )
    return [validate_row(cls, row, source=source, index=i) for i, row in enumerate(rows)]
