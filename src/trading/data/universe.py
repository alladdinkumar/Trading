"""Load the trading universe (NSE tickers) from `data/static/universe.txt`."""

from __future__ import annotations

from pathlib import Path

from trading.config import Paths, get_paths


def _default_universe_path(paths: Paths | None = None) -> Path:
    p = paths if paths is not None else get_paths()
    return p.project_root / "data" / "static" / "universe.txt"


def _default_candidate_path(paths: Paths | None = None) -> Path:
    p = paths if paths is not None else get_paths()
    return p.project_root / "data" / "static" / "nifty50.txt"


def load_universe(path: Path | None = None) -> list[str]:
    """Read tickers from a one-per-line file.

    Strips whitespace, skips blank lines and `#` comments, deduplicates while
    preserving first-seen order.
    """
    file_path = path if path is not None else _default_universe_path()
    return _read_symbol_file(file_path)


def load_candidate_universe(paths: Paths | None = None) -> list[str]:
    """Read the Nifty-50 candidate universe from `data/static/nifty50.txt`.

    This is the set the daily scan / ranker / auto-open operate over. Falls
    back to the full `universe.txt` if the candidate file is absent, so the
    scanner still works on a fresh checkout that hasn't pinned the list yet.
    """
    candidate_path = _default_candidate_path(paths)
    if candidate_path.is_file():
        return _read_symbol_file(candidate_path)
    return load_universe(_default_universe_path(paths))


def _read_symbol_file(file_path: Path) -> list[str]:
    """One ticker per line; strip whitespace, skip blanks/`#` comments, dedup."""
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
