"""Pure cross-source reconciliation for macro figures (F-036).

`reconcile_macro` compares the primary macro snapshot (yfinance + nsepython)
against the Kite MCP second source (`MacroCrossSource`) and returns one
`ReconRow` per reconcilable field. It is deliberately pure — no DB, no I/O — so
the tolerance logic is unit-testable in isolation. `trading macro verify`
persists the rows and reacts to a `mismatch`; `context._render_macro` reads them
back to annotate the bundle.

Per-field handling:
- **VIX** — absolute tolerance (default 0.5 vol points).
- **USDINR** — relative tolerance (default 0.5% of the primary).
- **FII / DII** — Kite has no flow feed, so these are always `unreconciled`
  (flagged, never silently `ok`); the second-source columns stay null.

Status precedence when a value is absent: a missing primary figure is
`missing_primary` (the figure itself is gone); otherwise a missing secondary is
`missing_secondary`.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.domain import MacroSnapshot
from trading.data.macro_cross import MacroCrossSource
from trading.store.reconciliation_store import ReconRow

# Default tolerances, keyed by field. VIX is absolute (vol points); USDINR is
# relative (fraction of the primary value).
DEFAULT_VIX_ABS_TOL = 0.5
DEFAULT_USDINR_REL_TOL = 0.005


@dataclass(frozen=True)
class _FieldSpec:
    field: str
    primary_source: str
    secondary_source: str | None  # None → no second source exists (FII/DII)
    kind: str  # "abs" | "rel" | "none"
    tolerance: float


_FIELD_SPECS: tuple[_FieldSpec, ...] = (
    _FieldSpec("vix", "yfinance", "kite_mcp", "abs", DEFAULT_VIX_ABS_TOL),
    _FieldSpec("usdinr", "yfinance", "kite_mcp", "rel", DEFAULT_USDINR_REL_TOL),
    _FieldSpec("fii_flow_cr", "nsepython", None, "none", 0.0),
    _FieldSpec("dii_flow_cr", "nsepython", None, "none", 0.0),
)


def reconcile_macro(
    primary: MacroSnapshot,
    secondary: MacroCrossSource,
    *,
    checked_at: str,
    tolerances: dict[str, float] | None = None,
) -> list[ReconRow]:
    """Reconcile `primary` against `secondary` → one ReconRow per field.

    `tolerances` optionally overrides the per-field defaults (keyed by field name).
    """
    tol = tolerances or {}
    rows: list[ReconRow] = []
    for spec in _FIELD_SPECS:
        primary_val = getattr(primary, spec.field)

        if spec.kind == "none":
            # No second source on Kite (FII/DII) — flag, never compare.
            rows.append(
                ReconRow(
                    date=primary.date,
                    field=spec.field,
                    primary_value=primary_val,
                    primary_source=spec.primary_source,
                    secondary_value=None,
                    secondary_source=None,
                    abs_delta=None,
                    status="unreconciled",
                    checked_at=checked_at,
                )
            )
            continue

        secondary_val = getattr(secondary, spec.field)
        status, abs_delta = _classify(
            primary_val, secondary_val, spec.kind, tol.get(spec.field, spec.tolerance)
        )
        rows.append(
            ReconRow(
                date=primary.date,
                field=spec.field,
                primary_value=primary_val,
                primary_source=spec.primary_source,
                secondary_value=secondary_val,
                secondary_source=spec.secondary_source,
                abs_delta=abs_delta,
                status=status,
                checked_at=checked_at,
            )
        )
    return rows


def _classify(
    primary_val: float | None,
    secondary_val: float | None,
    kind: str,
    tolerance: float,
) -> tuple[str, float | None]:
    """Return (status, abs_delta) for one comparable field."""
    if primary_val is None:
        return "missing_primary", None
    if secondary_val is None:
        return "missing_secondary", None

    abs_delta = abs(primary_val - secondary_val)
    if kind == "rel":
        # Relative to the primary; guard the zero-denominator edge.
        within = primary_val != 0 and abs_delta / abs(primary_val) <= tolerance
    else:  # "abs"
        within = abs_delta <= tolerance
    return ("ok" if within else "mismatch"), abs_delta
