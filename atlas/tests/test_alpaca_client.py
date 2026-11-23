"""Alpaca client tests against documented shapes. No network, no account."""
import json

import pytest

from atlas.exchange.alpaca import SAFE_SETTINGS, AlpacaClient, _loads
from atlas.exchange.errors import AuthError, ExchangeError, OrderRejected

GOOD_ACCOUNT = {"status": "ACTIVE", "trading_blocked": False, "account_blocked": False,
                "trade_suspended_by_user": False, "multiplier": "1", "shorting_enabled": False, "cash": "100.00"}
GOOD_CONFIG = {"no_shorting": True, "max_margin_multiplier": "1", "max_options_trading_level": 0,
               "suspend_trade": False}


class Rec:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r[0], {}, r[1]


def client(*responses):
    rec = Rec(*responses)
    return AlpacaClient("kid", "secret", paper=True, transport=rec, clock=lambda: 1.0), rec


def test_numbers_are_never_floats():
    d = _loads(b'{"ap": 387.7, "bp": 387.67, "as": 1}')
    assert d == {"ap": "387.7", "bp": "387.67", "as": 1}


def test_auth_headers_and_paper_host():
    c, rec = client((200, GOOD_ACCOUNT))
    c.account()
    method, url, headers, _ = rec.calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/account"
    assert headers["APCA-API-KEY-ID"] == "kid" and headers["APCA-API-SECRET-KEY"] == "secret"


def test_latest_quote_parsing_and_source():
    c, rec = client((200, {"symbol": "SPY", "quote": {"ap": "591.43", "as": 3, "bp": "591.41", "bs": 2,
                                                      "t": "2026-11-23T15:00:01.123456789Z"}}))
    q = c.latest_quote("SPY")
    assert (q["bid"], q["ask"], q["ask_size"], q["source"]) == ("591.41", "591.43", 3, "alpaca:iex")
    assert "data.alpaca.markets/v2/stocks/SPY/quotes/latest?feed=iex" in rec.calls[0][1]


def test_zero_price_means_no_quote():
    c, _ = client((200, {"quote": {"ap": "0", "as": 0, "bp": "10.00", "bs": 1, "t": "2026-11-23T15:00:01Z"}}))
    assert c.latest_quote("X")["ask"] is None


def test_safe_account_passes():
    c, _ = client((200, GOOD_ACCOUNT), (200, GOOD_CONFIG))
    assert c.safety_problems() == []


def test_margin_shorting_and_options_are_flagged():
    acct = GOOD_ACCOUNT | {"multiplier": "4", "shorting_enabled": True}
    conf = GOOD_CONFIG | {"no_shorting": False, "max_margin_multiplier": "4", "max_options_trading_level": 2}
    c, _ = client((200, acct), (200, conf))
    p = " ".join(c.safety_problems())
    for word in ("margin multiplier", "Short selling", "no_shorting", "max_margin_multiplier", "Options"):
        assert word in p


def test_blocked_account_is_flagged():
    c, _ = client((200, GOOD_ACCOUNT | {"trading_blocked": True, "status": "ACCOUNT_UPDATED"}), (200, GOOD_CONFIG))
    p = c.safety_problems()
    assert any("trading_blocked" in x for x in p) and any("status" in x for x in p)


def test_order_body_is_limit_day_no_extended_hours():
    b = AlpacaClient.order_body("SPY", "buy", "0.0034", "591.45", "atlas-long-buy-1")
    assert b == {"symbol": "SPY", "qty": "0.0034", "side": "buy", "type": "limit", "time_in_force": "day",
                 "limit_price": "591.45", "client_order_id": "atlas-long-buy-1", "extended_hours": False,
                 "position_intent": "buy_to_open"}
    with pytest.raises(ValueError):
        AlpacaClient.order_body("SPY", "short", "1", "1", "x")


def test_order_post_never_retried_and_errors_mapped():
    c, rec = client((503, {"message": "unavailable"}))
    with pytest.raises(ExchangeError) as e:
        c.place_order({"symbol": "SPY"})
    assert not isinstance(e.value, OrderRejected) and len(rec.calls) == 1
    c, _ = client((403, {"code": 40310000, "message": "insufficient buying power"}))
    with pytest.raises(OrderRejected):
        c.place_order({"symbol": "SPY"})
    c, _ = client((401, {"message": "unauthorized"}))
    with pytest.raises(AuthError):
        c.place_order({"symbol": "SPY"})


def test_order_lookup_by_client_id():
    c, rec = client((404, {"message": "order not found"}))
    assert c.get_order_by_client_id("atlas-x") is None
    assert "orders:by_client_order_id?client_order_id=atlas-x" in rec.calls[0][1]


def test_safe_settings_are_long_only_no_margin_no_options():
    assert SAFE_SETTINGS == {"no_shorting": True, "max_margin_multiplier": "1", "max_options_trading_level": 0}


def test_gateway_refuses_stock_execution_for_now(tmp_path):
    from .harness import build
    st = build(tmp_path)
    st.ex.add_market("SPY", "SPY", module="stocks", yes=("591.41", "591.43", 10))
    st.gw.control.set_module("stocks", True)
    st.gw.control.authorize_today(st.day, confirm="AUTHORIZE")
    r = st.buy(st.proposal(market="SPY", module="stocks", qty=1, limit="591.43"))
    assert r.status == "rejected" and "Phase 6" in r.reasons[0]
    st.gw.stop()
