"""Architecture guard — the import-linter layering contract runs in the test
gate (F-009).

This repo has no separate CI lint step, so the L0–L6 dependency DAG documented
in docs/architecture/01-architecture.md is enforced here: any new back-edge
(a lower layer importing a higher one) flips the contract to BROKEN and fails
this test. Keeping it in pytest means a regression can't merge unnoticed.
"""

from __future__ import annotations

from pathlib import Path

from importlinter.cli import EXIT_STATUS_SUCCESS, lint_imports

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_layering_contract_holds() -> None:
    """The `Layered architecture (L0–L6)` contract must be KEPT."""
    config = PROJECT_ROOT / "pyproject.toml"
    exit_status = lint_imports(config_filename=str(config))
    assert exit_status == EXIT_STATUS_SUCCESS, (
        "import-linter layering contract is BROKEN - a back-edge violates the "
        "L0-L6 DAG. Run `uv run lint-imports` to see the offending import."
    )
