"""Tests for trading.data.kite — typed wrapper over kiteconnect SDK."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trading.data.kite import (
    GttOrder,
    Holding,
    KiteAuthError,
    Margin,
    Position,
    Quote,
    generate_session,
    get_gtts,
    get_holdings,
    get_ltp,
    get_margins,
    get_positions,
    get_quotes,
    is_authenticated,
    login_url,
    make_client,
)

# ---------------------------------------------------------------------------
# Fixtures — raw dicts mimicking what kiteconnect returns
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_holding() -> dict:
    return {
        "tradingsymbol": "RVNL",
        "exchange": "NSE",
        "isin": "INE415G01027",
        "instrument_token": 12345,
        "product": "CNC",
        "quantity": 594,
        "average_price": 328.21,
        "last_price": 305.0,
        "close_price": 308.5,
        "pnl": -13784.0,
        "day_change": -3.5,
        "day_change_percentage": -1.13,
    }


@pytest.fixture
def raw_position() -> dict:
    return {
        "tradingsymbol": "NTPC",
        "exchange": "NSE",
        "product": "MIS",
        "quantity": 195,
        "average_price": 340.34,
        "last_price": 402.2,
        "pnl": 12063.0,
        "instrument_token": 23456,
    }


@pytest.fixture
def raw_gtt() -> dict:
    return {
        "id": 123456,
        "type": "single",
        "status": "active",
        "created_at": "2026-04-15 10:30:00",
        "condition": {
            "tradingsymbol": "TATAPOWER",
            "exchange": "NSE",
            "last_price": 436.0,
            "trigger_values": [500.0],
        },
        "orders": [
            {
                "transaction_type": "SELL",
                "quantity": 340,
                "order_type": "LIMIT",
                "price": 500.0,
            }
        ],
    }


@pytest.fixture
def raw_quote() -> dict:
    return {
        "instrument_token": 12345,
        "last_price": 305.0,
        "volume": 1500000,
        "ohlc": {"open": 308.0, "high": 310.5, "low": 302.0, "close": 308.5},
        "buy_quantity": 12000,
        "sell_quantity": 8000,
        "depth": {
            "buy": [{"price": 304.95, "quantity": 1000}],
            "sell": [{"price": 305.05, "quantity": 800}],
        },
        "oi": 0,
        "upper_circuit_limit": 339.35,
        "lower_circuit_limit": 277.65,
    }


@pytest.fixture
def raw_margin() -> dict:
    return {
        "available": {"cash": 25000.0, "live_balance": 25000.0},
        "utilised": {"debits": 5000.0, "live_balance": 5000.0},
        "net": 20000.0,
    }


# ---------------------------------------------------------------------------
# make_client / auth
# ---------------------------------------------------------------------------


def test_make_client_without_token() -> None:
    with patch("trading.data.kite.KiteConnect") as mock_cls:
        client = make_client("api-key")
    mock_cls.assert_called_once_with(api_key="api-key")
    client.set_access_token.assert_not_called()


def test_make_client_with_token() -> None:
    with patch("trading.data.kite.KiteConnect") as mock_cls:
        inst = mock_cls.return_value
        make_client("api-key", access_token="tok-123")
    inst.set_access_token.assert_called_once_with("tok-123")


def test_login_url_proxies() -> None:
    fake = MagicMock()
    fake.login_url.return_value = "https://kite.trade/connect/login?api_key=xyz"
    assert login_url(fake) == "https://kite.trade/connect/login?api_key=xyz"


def test_generate_session_returns_access_token() -> None:
    fake = MagicMock()
    fake.generate_session.return_value = {"access_token": "fresh-token", "public_token": "pub"}
    token = generate_session(fake, "request-tok", "api-secret")
    assert token == "fresh-token"
    fake.generate_session.assert_called_once_with("request-tok", api_secret="api-secret")
    fake.set_access_token.assert_called_once_with("fresh-token")


def test_is_authenticated_true_when_profile_ok() -> None:
    fake = MagicMock()
    fake.profile.return_value = {"user_name": "Sandeep"}
    assert is_authenticated(fake) is True


def test_is_authenticated_false_when_profile_raises() -> None:
    fake = MagicMock()
    fake.profile.side_effect = RuntimeError("boom")
    assert is_authenticated(fake) is False


# ---------------------------------------------------------------------------
# get_holdings
# ---------------------------------------------------------------------------


def test_get_holdings_maps_shape(raw_holding: dict) -> None:
    fake = MagicMock()
    fake.holdings.return_value = [raw_holding]
    holdings = get_holdings(fake)
    assert len(holdings) == 1
    h = holdings[0]
    assert isinstance(h, Holding)
    assert h.tradingsymbol == "RVNL"
    assert h.exchange == "NSE"
    assert h.isin == "INE415G01027"
    assert h.quantity == 594
    assert h.average_price == 328.21
    assert h.last_price == 305.0
    assert h.close_price == 308.5
    assert h.pnl == -13784.0
    assert h.day_change == -3.5
    assert h.day_change_percentage == -1.13


def test_get_holdings_empty() -> None:
    fake = MagicMock()
    fake.holdings.return_value = []
    assert get_holdings(fake) == []


def test_get_holdings_raises_kite_auth_error_on_token_exception() -> None:
    from kiteconnect.exceptions import TokenException

    fake = MagicMock()
    fake.holdings.side_effect = TokenException("Invalid token")
    with pytest.raises(KiteAuthError):
        get_holdings(fake)


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------


def test_get_positions_combines_net_and_day(raw_position: dict) -> None:
    fake = MagicMock()
    fake.positions.return_value = {"net": [raw_position], "day": []}
    positions = get_positions(fake)
    assert len(positions) == 1
    p = positions[0]
    assert isinstance(p, Position)
    assert p.tradingsymbol == "NTPC"
    assert p.product == "MIS"
    assert p.quantity == 195


# ---------------------------------------------------------------------------
# get_gtts
# ---------------------------------------------------------------------------


def test_get_gtts_maps_nested_condition(raw_gtt: dict) -> None:
    fake = MagicMock()
    fake.get_gtts.return_value = [raw_gtt]
    gtts = get_gtts(fake)
    assert len(gtts) == 1
    g = gtts[0]
    assert isinstance(g, GttOrder)
    assert g.id == 123456
    assert g.tradingsymbol == "TATAPOWER"
    assert g.trigger_values == [500.0]
    assert g.last_price == 436.0
    assert g.status == "active"
    assert len(g.orders) == 1


# ---------------------------------------------------------------------------
# get_quotes
# ---------------------------------------------------------------------------


def test_get_quotes_passes_through_key(raw_quote: dict) -> None:
    fake = MagicMock()
    fake.quote.return_value = {"NSE:RVNL": raw_quote}
    quotes = get_quotes(fake, ["NSE:RVNL"])
    assert "NSE:RVNL" in quotes
    q = quotes["NSE:RVNL"]
    assert isinstance(q, Quote)
    assert q.last_price == 305.0
    assert q.volume == 1500000
    assert q.open == 308.0
    assert q.high == 310.5
    assert q.low == 302.0
    assert q.close == 308.5
    assert q.bid == 304.95
    assert q.ask == 305.05
    assert q.upper_circuit_limit == 339.35
    assert q.lower_circuit_limit == 277.65


def test_get_quotes_handles_missing_depth() -> None:
    fake = MagicMock()
    fake.quote.return_value = {
        "NSE:RVNL": {
            "instrument_token": 1,
            "last_price": 100.0,
            "volume": 0,
            "ohlc": {"open": 100, "high": 100, "low": 100, "close": 100},
            "buy_quantity": 0,
            "sell_quantity": 0,
        }
    }
    q = get_quotes(fake, ["NSE:RVNL"])["NSE:RVNL"]
    assert q.bid is None
    assert q.ask is None


# ---------------------------------------------------------------------------
# get_ltp
# ---------------------------------------------------------------------------


def test_get_ltp_flattens_to_float_dict() -> None:
    fake = MagicMock()
    fake.ltp.return_value = {
        "NSE:RVNL": {"instrument_token": 1, "last_price": 305.0},
        "NSE:NTPC": {"instrument_token": 2, "last_price": 402.2},
    }
    out = get_ltp(fake, ["NSE:RVNL", "NSE:NTPC"])
    assert out == {"NSE:RVNL": 305.0, "NSE:NTPC": 402.2}


# ---------------------------------------------------------------------------
# get_margins
# ---------------------------------------------------------------------------


def test_get_margins_equity(raw_margin: dict) -> None:
    fake = MagicMock()
    fake.margins.return_value = raw_margin
    m = get_margins(fake, segment="equity")
    assert isinstance(m, Margin)
    assert m.segment == "equity"
    assert m.available_cash == 25000.0
    assert m.utilised_total == 5000.0
    assert m.net == 20000.0


# ---------------------------------------------------------------------------
# Live integration — only runs with `pytest -m live` and a valid access token
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_get_holdings_against_real_kite() -> None:
    """Live: real-Kite holdings fetch. Requires valid KITE_ACCESS_TOKEN in .env."""
    from trading.config import get_settings

    settings = get_settings()
    if not settings.kite_api_key or not settings.kite_access_token:
        pytest.skip("KITE_API_KEY / KITE_ACCESS_TOKEN not set")
    client = make_client(settings.kite_api_key, settings.kite_access_token)
    holdings = get_holdings(client)
    # Don't assert specific holdings (portfolio changes). Just shape.
    assert isinstance(holdings, list)
    for h in holdings:
        assert isinstance(h, Holding)
        assert h.tradingsymbol
        assert h.exchange in {"NSE", "BSE"}
        assert h.quantity >= 0
