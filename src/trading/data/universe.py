"""Load the trading universe (NSE tickers) from `data/static/universe.txt`."""

from __future__ import annotations

from pathlib import Path

from trading.config import Paths, get_paths


def _default_universe_path(paths: Paths | None = None) -> Path:
    p = paths if paths is not None else get_paths()
    return p.project_root / "data" / "static" / "universe.txt"


def load_universe(path: Path | None = None) -> list[str]:
    """Read tickers from a one-per-line file.

    Strips whitespace, skips blank lines and `#` comments, deduplicates while
    preserving first-seen order.
    """
    file_path = path if path is not None else _default_universe_path()
    seen: set[str] = set()
    out: list[str] = []
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out
