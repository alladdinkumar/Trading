"""NSE F&O securities-ban list fetcher (F-010).

The daily ban CSV lists symbols barred from fresh F&O positions. We use it to
populate `fno_ban_list` so Layer A's `passes_not_fno_banned` gate (dead since
F-019 left the context empty) can veto a banned candidate.

Best-effort by contract: any network/parse failure yields `[]`, so a feed
outage degrades the gate to a pass with a warning rather than killing pre-open.
"""

from __future__ import annotations

import re

from requests_cache import CachedSession

from trading.data.cache import get_cached_session

FNO_SECBAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"

# A desktop UA — NSE rejects empty/unknown agents (matches the news fetchers).
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 10

# Tokens the CSV uses for its header/columns, never a real symbol.
_HEADER_TOKENS = frozenset({"DATE", "SYMBOL", "SYMBOLS", "SRNO", "SR", "SR.NO.", "SERIALNUMBER"})
# A ticker: leading letter, then letters/digits plus the `&`/`-` seen in
# BAJAJ-AUTO / M&M. Excludes serial numbers and the date value.
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9&-]*")


def _is_symbol(token: str) -> bool:
    return bool(token) and token not in _HEADER_TOKENS and bool(_SYMBOL_RE.fullmatch(token))


def parse_fno_ban_csv(text: str) -> list[str]:
    """Extract ban-list symbols from the NSE CSV body (pipe- or comma-delimited).

    Order-preserving and deduped. Tolerant of the header line, the leading
    serial column, and stray blank lines.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for field in re.split(r"[|,]", line):
            token = field.strip().upper()
            if _is_symbol(token) and token not in seen:
                seen.add(token)
                out.append(token)
    return out


def fetch_fno_ban_symbols(session: CachedSession | None = None) -> list[str]:
    """Fetch + parse the NSE F&O ban list. Best-effort: any failure → []."""
    try:
        sess = session if session is not None else get_cached_session()
        resp = sess.get(FNO_SECBAN_URL, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        resp.raise_for_status()
        return parse_fno_ban_csv(resp.text)
    except Exception:  # pragma: no cover — defensive; feed outage must not raise
        return []
