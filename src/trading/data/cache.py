"""SQLite-backed `requests` cache for HTTP fetchers (news, NSE files, etc.)."""

from __future__ import annotations

from requests_cache import CachedSession

from trading.config import Paths, get_paths


def get_cached_session(
    paths: Paths | None = None,
    *,
    expire_after: int = 3600,
) -> CachedSession:
    """Return a CachedSession backed by `data/cache/http.sqlite` (default TTL 1h)."""
    p = paths if paths is not None else get_paths()
    p.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = p.cache_dir / "http"
    return CachedSession(
        cache_name=str(cache_path),
        backend="sqlite",
        expire_after=expire_after,
    )
