# Phase 3 — Kite Connect Wrapper Implementation Plan

**Goal:** A typed Python wrapper around the `kiteconnect` SDK exposing the operations Phase 10+ needs (holdings, positions, GTTs, quotes, LTP, margins), plus an interactive `trading kite-login` CLI command that handles the daily access-token rotation and writes the new token to `.env`.

**Architecture:** Module-level functions over `KiteConnect`. Frozen dataclasses for return types. A dedicated `KiteAuthError` so callers know when re-login is required. Token persistence via a small `.env` line-rewriter. Tests mock `KiteConnect` at the import site so no live calls are made.

**Tech Stack:** `kiteconnect` (already in deps), `typer` (CLI).

**Reference:** Spec Section 6 (Kite-sourced indicators), Section 11 (`src/trading/data/kite.py`).

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/trading/data/kite.py` | Typed dataclasses + module functions |
| Modify | `src/trading/cli.py` | Add `kite-login` command |
| Modify | `src/trading/config.py` | Add `update_env_var(path, key, value)` helper |
| Create | `tests/test_kite.py` | Mocked SDK tests + shape mapping |
| Create | `tests/test_config_env.py` | Tests for `update_env_var` |
| Modify | `tests/test_cli.py` | Add `kite-login` CLI tests |
| Modify | `PROGRESS.md` | Tick 3.1-3.6 |

---

## Public API of `data/kite.py`

```python
class KiteAuthError(Exception): ...

# Construction / auth
def make_client(api_key: str, access_token: str | None = None) -> KiteConnect: ...
def login_url(client: KiteConnect) -> str: ...
def generate_session(client: KiteConnect, request_token: str, api_secret: str) -> str: ...
def is_authenticated(client: KiteConnect) -> bool: ...

# Account data (all raise KiteAuthError on stale token)
def get_holdings(client: KiteConnect) -> list[Holding]: ...
def get_positions(client: KiteConnect) -> list[Position]: ...
def get_gtts(client: KiteConnect) -> list[GttOrder]: ...
def get_quotes(client: KiteConnect, instruments: list[str]) -> dict[str, Quote]: ...
def get_ltp(client: KiteConnect, instruments: list[str]) -> dict[str, float]: ...
def get_margins(client: KiteConnect, segment: str = "equity") -> Margin: ...
```

**Dataclasses** (all frozen):

- `Holding` — tradingsymbol, exchange, isin, quantity, average_price, last_price, close_price, pnl, day_change, day_change_percentage
- `Position` — tradingsymbol, exchange, product, quantity, average_price, last_price, pnl
- `GttOrder` — id, type, status, tradingsymbol, exchange, trigger_values (list[float]), last_price, created_at, orders (list[dict] — leaves)
- `Quote` — instrument_token, last_price, volume, ohlc(open/high/low/close), bid, ask, oi, upper/lower circuit
- `Margin` — segment, available_cash, utilised_total, net

`KiteAuthError` is raised whenever `kiteconnect.exceptions.TokenException` (or any auth-shaped error) bubbles up from a wrapped call.

## `config.py` addition

```python
def update_env_var(path: Path, key: str, value: str) -> None:
    """Idempotently set KEY=VALUE in a dotenv file.

    Creates the file if missing. Updates the existing line in place when KEY
    exists; otherwise appends. Preserves all other lines verbatim.
    """
```

## CLI: `trading kite-login`

Interactive flow:
1. Read `KITE_API_KEY` / `KITE_API_SECRET` from Settings; error if missing.
2. Print `login_url(client)`.
3. Prompt user to paste back the `request_token` from the redirect URL.
4. Call `generate_session()` → get fresh access token.
5. Write `KITE_ACCESS_TOKEN=<token>` to `.env` via `update_env_var`.
6. Confirm with the user's profile name (from `client.profile()`).

## Tests

- `tests/test_kite.py` — patch `trading.data.kite.KiteConnect` with `MagicMock`. Test:
  - Each public function's shape mapping from raw dict → dataclass
  - `KiteAuthError` raised when underlying SDK raises `TokenException`
  - `make_client` sets the access token only when provided
  - `login_url`, `generate_session` round-trip
  - `get_quotes` handles the `"NSE:RVNL"` key format
- `tests/test_config_env.py` — `update_env_var` covering insert / update / no-trailing-newline / file-not-exists
- `tests/test_cli.py` — `kite-login` CLI flow with everything mocked

`@pytest.mark.live` — one test for `get_holdings` against the real account. Skipped by default; run with `pytest -m live` when the user has a valid token.

## Tasks (TDD order)

1. Write all tests (will fail — modules don't exist yet).
2. Implement `config.update_env_var`.
3. Implement `data/kite.py` (dataclasses + functions).
4. Add `kite-login` to `cli.py`.
5. Run `ruff check` / `ruff format --check` / `mypy` / `pytest`.
6. Update PROGRESS.md → commit.
