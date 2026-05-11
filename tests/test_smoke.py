"""Smoke test — confirms the package imports cleanly and tooling is wired up."""

from pathlib import Path

import trading


def test_package_version() -> None:
    assert trading.__version__ == "0.1.0"


def test_fixtures_dir_resolves(fixtures_dir: Path) -> None:
    assert fixtures_dir.name == "fixtures"
    assert fixtures_dir.is_dir()
