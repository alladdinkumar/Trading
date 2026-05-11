"""Typed wrapper over the `kiteconnect` SDK.

Exposes the subset of operations the trading system actually uses, each
returning a frozen dataclass instead of a raw dict. Auth-shaped errors from
the SDK are translated into `KiteAuthError` so callers can branch on
"re-login required" without importing from `kiteconnect.exceptions`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException


class KiteAuthError(Exception):
    """Raised when Kite rejects auth — the access token is stale or invalid."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    tradingsymbol: str
    exchange: str
    isin: str | None
    quantity: int
    average_price: float
    last_price: float
    close_price: float
    pnl: float
    day_change: float
    day_change_percentage: float


@dataclass(frozen=True)
class Position:
    tradingsymbol: str
    exchange: str
    product: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float


@dataclass(frozen=True)
class GttOrder:
    id: int
    type: str
    status: str
    tradingsymbol: str
    exchange: str
    trigger_values: list[float]
    last_price: float | None
    created_at: str
    orders: list[dict[str, Any]]


@dataclass(frozen=True)
class Quote:
    instrument_token: int
    last_price: float
    volume: int
    open: float
    high: float
    low: float
    close: float
    bid: float | None
    ask: float | None
    oi: int | None
    upper_circuit_limit: float | None
    lower_circuit_limit: float | None


@dataclass(frozen=True)
class Margin:
    segment: str
    available_cash: float
    utilised_total: float
    net: float


# ---------------------------------------------------------------------------
# Construction / auth
# ---------------------------------------------------------------------------


def make_client(api_key: str, access_token: str | None = None) -> KiteConnect:
    """Construct a KiteConnect client. If `access_token` is provided, sets it."""
    client = KiteConnect(api_key=api_key)
    if access_token:
        client.set_access_token(access_token)
    return client


def login_url(client: KiteConnect) -> str:
    """Return the Kite browser-login URL for the configured API key."""
    return str(client.login_url())


def generate_session(client: KiteConnect, request_token: str, api_secret: str) -> str:
    """Exchange a `request_token` for a fresh `access_token`.

    Also sets the token on the client so subsequent calls work without re-login.
    """
    data = client.generate_session(request_token, api_secret=api_secret)
    token = str(data["access_token"])
    client.set_access_token(token)
    return token


def is_authenticated(client: KiteConnect) -> bool:
    """Cheap health check — calls `profile()` and returns whether it succeeded."""
    try:
        client.profile()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Internal: wrap SDK calls so TokenException → KiteAuthError
# ---------------------------------------------------------------------------


def _wrap_auth(call: Any) -> Any:
    """Call a zero-arg lambda, re-raising TokenException as KiteAuthError."""
    try:
        return call()
    except TokenException as exc:
        raise KiteAuthError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Adapters: raw dict → dataclass
# ---------------------------------------------------------------------------


def _to_holding(d: dict[str, Any]) -> Holding:
    return Holding(
        tradingsymbol=d["tradingsymbol"],
        exchange=d["exchange"],
        isin=d.get("isin"),
        quantity=int(d["quantity"]),
        average_price=float(d["average_price"]),
        last_price=float(d["last_price"]),
        close_price=float(d["close_price"]),
        pnl=float(d["pnl"]),
        day_change=float(d["day_change"]),
        day_change_percentage=float(d["day_change_percentage"]),
    )


def _to_position(d: dict[str, Any]) -> Position:
    return Position(
        tradingsymbol=d["tradingsymbol"],
        exchange=d["exchange"],
        product=d["product"],
        quantity=int(d["quantity"]),
        average_price=float(d["average_price"]),
        last_price=float(d["last_price"]),
        pnl=float(d["pnl"]),
    )


def _to_gtt(d: dict[str, Any]) -> GttOrder:
    cond = d.get("condition", {})
    return GttOrder(
        id=int(d["id"]),
        type=d.get("type", "single"),
        status=d.get("status", ""),
        tradingsymbol=cond.get("tradingsymbol", ""),
        exchange=cond.get("exchange", ""),
        trigger_values=[float(x) for x in cond.get("trigger_values", [])],
        last_price=float(cond["last_price"]) if "last_price" in cond else None,
        created_at=str(d.get("created_at", "")),
        orders=list(d.get("orders", [])),
    )


def _to_quote(d: dict[str, Any]) -> Quote:
    ohlc = d.get("ohlc", {})
    depth = d.get("depth", {})
    buy = depth.get("buy") or []
    sell = depth.get("sell") or []
    bid = float(buy[0]["price"]) if buy else None
    ask = float(sell[0]["price"]) if sell else None
    return Quote(
        instrument_token=int(d["instrument_token"]),
        last_price=float(d["last_price"]),
        volume=int(d.get("volume", 0)),
        open=float(ohlc.get("open", 0.0)),
        high=float(ohlc.get("high", 0.0)),
        low=float(ohlc.get("low", 0.0)),
        close=float(ohlc.get("close", 0.0)),
        bid=bid,
        ask=ask,
        oi=int(d["oi"]) if d.get("oi") is not None else None,
        upper_circuit_limit=(
            float(d["upper_circuit_limit"]) if d.get("upper_circuit_limit") is not None else None
        ),
        lower_circuit_limit=(
            float(d["lower_circuit_limit"]) if d.get("lower_circuit_limit") is not None else None
        ),
    )


def _to_margin(d: dict[str, Any], segment: str) -> Margin:
    available = d.get("available", {})
    utilised = d.get("utilised", {})
    return Margin(
        segment=segment,
        available_cash=float(available.get("cash", 0.0)),
        utilised_total=float(utilised.get("debits", 0.0)),
        net=float(d.get("net", 0.0)),
    )


# ---------------------------------------------------------------------------
# Public account data functions
# ---------------------------------------------------------------------------


def get_holdings(client: KiteConnect) -> list[Holding]:
    raw = _wrap_auth(client.holdings)
    return [_to_holding(d) for d in raw]


def get_positions(client: KiteConnect) -> list[Position]:
    raw = _wrap_auth(client.positions)
    # Kite returns {'net': [...], 'day': [...]}. We collapse to 'net' for analysis.
    return [_to_position(d) for d in raw.get("net", [])]


def get_gtts(client: KiteConnect) -> list[GttOrder]:
    raw = _wrap_auth(client.get_gtts)
    return [_to_gtt(d) for d in raw]


def get_quotes(client: KiteConnect, instruments: list[str]) -> dict[str, Quote]:
    raw = _wrap_auth(lambda: client.quote(instruments))
    return {key: _to_quote(val) for key, val in raw.items()}


def get_ltp(client: KiteConnect, instruments: list[str]) -> dict[str, float]:
    raw = _wrap_auth(lambda: client.ltp(instruments))
    return {key: float(val["last_price"]) for key, val in raw.items()}


def get_margins(client: KiteConnect, segment: str = "equity") -> Margin:
    raw = _wrap_auth(lambda: client.margins(segment=segment))
    return _to_margin(raw, segment)
