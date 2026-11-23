"""Kalshi client tests against documented request/response shapes (OpenAPI 3.37.0).

A recording transport stands in for the network: every request ATLAS would
send is captured and checked, and canned responses follow the documented
schemas. Nothing here touches a real account.
"""
import base64
import email.utils
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.exchange.errors import AuthError, DataError, DuplicateOrder, ExchangeError, OrderRejected
from atlas.exchange.kalshi import DEMO, KalshiExchange, Signer, to_v2

NOW = 1_795_400_000.0


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, payload = r
        return status, {"Date": email.utils.formatdate(NOW, usegmt=True)}, payload


def rsa_signer():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                          serialization.NoEncryption())
    return Signer("key-123", pem), k.public_key()


def ed_signer():
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return Signer("key-ed", pem), k.public_key()


def client(*responses, subaccount=None):
    rec = Recorder(*responses)
    signer, _ = ed_signer()
    return KalshiExchange(DEMO, signer, subaccount=subaccount, transport=rec, clock=lambda: NOW, min_interval=0), rec


def order_obj(coid, status="executed", fill="2.00", remaining="0.00", initial="2.00", yes="0.8800", no="0.1200",
              ticker="KXBTC15M-26NOV221845-45"):
    return {"order_id": "ord-1", "user_id": "u", "client_order_id": coid, "ticker": ticker,
            "outcome_side": "yes", "book_side": "bid", "type": "limit", "status": status,
            "yes_price_dollars": yes, "no_price_dollars": no, "fill_count_fp": fill, "remaining_count_fp": remaining,
            "initial_count_fp": initial, "taker_fees_dollars": "0.020000", "maker_fees_dollars": "0.000000",
            "taker_fill_cost_dollars": "1.760000", "maker_fill_cost_dollars": "0.000000"}


# ------------------------------------------------------------------ signing
@pytest.mark.parametrize("make", [rsa_signer, ed_signer])
def test_signature_covers_timestamp_method_and_path_without_query(make):
    signer, pub = make()
    h = signer.headers("GET", "/trade-api/v2/portfolio/orders?limit=5", 1703123456789)
    msg = b"1703123456789GET/trade-api/v2/portfolio/orders"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    if isinstance(pub, rsa.RSAPublicKey):
        pub.verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    else:
        pub.verify(sig, msg)
    assert h["KALSHI-ACCESS-KEY"] and h["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"


def test_signed_path_includes_api_root():
    c, rec = client((200, {"balance": 500, "balance_dollars": "5.0000", "portfolio_value": 500, "updated_ts": 1}))
    assert c.balance() == "5.0000"
    call = rec.calls[0]
    assert call["url"].startswith("https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance")


# ------------------------------------------------------------------ order direction
def test_v2_direction_translation():
    assert to_v2("yes", "buy", "0.88") == ("bid", "0.880000")
    assert to_v2("no", "buy", "0.13") == ("ask", "0.870000")     # buy NO at 13c = sell YES at 87c
    assert to_v2("yes", "sell", "0.50") == ("ask", "0.500000")
    assert to_v2("no", "sell", "0.40") == ("bid", "0.600000")


def test_place_order_body_matches_v2_spec():
    coid = "atlas-no-buy-0f8f1d2e-1111-4a4a-9b9b-123456789abc"
    c, rec = client((201, {"order_id": "o1", "client_order_id": coid, "fill_count": "2.00",
                           "remaining_count": "0.00", "ts_ms": 1}), subaccount=7)
    r = c.place_order(coid, "KXBTC15M-X", "no", "buy", 2, "0.1300")
    call = rec.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/trade-api/v2/portfolio/events/orders")
    b = call["body"]
    assert b == {"ticker": "KXBTC15M-X", "client_order_id": coid, "side": "ask", "count": "2", "price": "0.870000",
                 "self_trade_prevention_type": "taker_at_cross", "time_in_force": "immediate_or_cancel",
                 "cancel_order_on_pause": True, "subaccount": 7}
    assert "expiration_time" not in b                            # not allowed with IOC
    assert r["status"] == "filled" and r["filled"] == 2


def test_sell_orders_are_reduce_only():
    coid = "atlas-yes-sell-0f8f1d2e-1111-4a4a-9b9b-123456789abc"
    c, rec = client((201, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "0.00", "ts_ms": 1}))
    r = c.place_order(coid, "M", "yes", "sell", 1, "0.40")
    assert rec.calls[0]["body"]["reduce_only"] is True and r["status"] == "cancelled"


def test_client_order_id_must_encode_intent():
    c, rec = client()
    with pytest.raises(OrderRejected):
        c.place_order("random-id", "M", "yes", "buy", 1, "0.5")
    with pytest.raises(OrderRejected):
        c.place_order("atlas-yes-buy-x-y", "M", "yes", "buy", 0, "0.5")
    assert rec.calls == []


# ------------------------------------------------------------------ error handling
COID = "atlas-yes-buy-0f8f1d2e-1111-4a4a-9b9b-123456789abc"


def test_post_is_never_retried_and_5xx_is_unknown():
    c, rec = client((503, {"error": {"code": "service_unavailable"}}), (201, {}))
    with pytest.raises(ExchangeError) as e:
        c.place_order(COID, "M", "yes", "buy", 1, "0.5")
    assert not isinstance(e.value, OrderRejected) and len(rec.calls) == 1


def test_timeout_propagates_as_unknown():
    c, rec = client(TimeoutError("timed out"))
    with pytest.raises(TimeoutError):
        c.place_order(COID, "M", "yes", "buy", 1, "0.5")
    assert len(rec.calls) == 1


@pytest.mark.parametrize("status,code,exc", [
    (409, "order_already_exists", DuplicateOrder),
    (400, "insufficient_balance", OrderRejected),
    (400, "invalid_price", OrderRejected),
    (409, "market_inactive", OrderRejected),
    (410, "deprecated_v1_order_endpoint", OrderRejected),
    (429, "too_many_requests", OrderRejected),
    (401, "INCORRECT_API_KEY_SIGNATURE", AuthError),
    (403, "insufficient_scope:_write::trade_required", AuthError),
])
def test_order_error_codes(status, code, exc):
    c, _ = client((status, {"error": {"code": code, "message": "x"}}))
    with pytest.raises(exc):
        c.place_order(COID, "M", "yes", "buy", 1, "0.5")


def test_get_retries_on_429_then_succeeds():
    c, rec = client((429, {"error": {"code": "too_many_requests"}}),
                    (200, {"balance": 1, "balance_dollars": "0.0100", "portfolio_value": 1, "updated_ts": 1}))
    assert c.balance() == "0.0100" and len(rec.calls) == 2


# ------------------------------------------------------------------ reads
def test_get_order_finds_by_client_order_id_and_maps_status():
    c, rec = client((200, {"orders": [order_obj("someone-else"), order_obj(COID, status="resting", fill="1.00",
                                                                           remaining="1.00")], "cursor": ""}))
    o = c.get_order(COID, market="KXBTC15M-26NOV221845-45")
    assert o["status"] == "open" and o["filled"] == 1 and o["remaining"] == 1 and o["side"] == "yes"
    assert "ticker=KXBTC15M-26NOV221845-45" in rec.calls[0]["url"]


def test_get_order_not_visible_returns_none():
    c, _ = client((200, {"orders": [], "cursor": ""}))
    assert c.get_order(COID) is None


def test_fractional_fill_is_refused():
    c, _ = client((200, {"orders": [order_obj(COID, fill="1.50", remaining="0.00", initial="2.00",
                                              status="canceled")], "cursor": ""}))
    with pytest.raises(DataError):
        c.get_order(COID)


def test_fills_map_to_atlas_side_action_price_and_fee():
    fill = {"fill_id": "f1", "exchange_index": 2, "trade_id": "f1", "order_id": "ord-1", "ticker": "KXBTC15M-X",
            "market_ticker": "KXBTC15M-X", "outcome_side": "no", "book_side": "ask", "count_fp": "2.00",
            "yes_price_dollars": "0.870000", "no_price_dollars": "0.130000", "is_taker": True,
            "fee_cost": "0.020000", "ts": 1795399000}
    no_coid = "atlas-no-buy-0f8f1d2e-1111-4a4a-9b9b-123456789abc"
    c, _ = client((200, {"fills": [fill], "cursor": ""}),
                  (200, {"orders": [order_obj(no_coid, ticker="KXBTC15M-X")], "cursor": ""}))
    f = c.fills(NOW - 3600)[0]
    assert (f["side"], f["action"], f["qty"], f["price"], f["fee"]) == ("no", "buy", 2, "0.130000", "0.020000")
    assert f["client_order_id"] == no_coid


def test_fill_direction_mismatch_is_data_error():
    fill = {"fill_id": "f1", "order_id": "ord-1", "ticker": "M", "outcome_side": "yes", "book_side": "bid",
            "count_fp": "1.00", "yes_price_dollars": "0.5", "no_price_dollars": "0.5", "fee_cost": "0.01", "ts": 1}
    c, _ = client((200, {"fills": [fill], "cursor": ""}),
                  (200, {"orders": [order_obj("atlas-no-buy-0f8f1d2e-1111-4a4a-9b9b-123456789abc", ticker="M")],
                         "cursor": ""}))
    with pytest.raises(DataError):
        c.fills(NOW - 3600)


def test_positions_signed_count_becomes_side():
    c, _ = client((200, {"market_positions": [
        {"ticker": "A", "position_fp": "3.00", "market_exposure_dollars": "2.640000"},
        {"ticker": "B", "position_fp": "-2.00", "market_exposure_dollars": "0.260000"},
        {"ticker": "C", "position_fp": "0.00", "market_exposure_dollars": "0"}], "event_positions": [], "cursor": ""}))
    assert c.positions() == [{"market": "A", "side": "yes", "qty": 3, "cost": "2.640000"},
                             {"market": "B", "side": "no", "qty": 2, "cost": "0.260000"}]


def test_orderbook_derives_asks_from_opposite_bids():
    c, _ = client((200, {"orderbook_fp": {"yes_dollars": [["0.8600", "40.00"], ["0.8500", "10.00"]],
                                          "no_dollars": [["0.1200", "25.00"], ["0.1100", "5.00"]]}}))
    b = c.orderbook("M", "yes")
    assert (b["bid"], b["ask"], b["ask_size"]) == ("0.860000", "0.880000", 25)
    assert b["ts"] == NOW and b["source"] == "kalshi:orderbook"


def test_settlements_binary_only():
    s = {"ticker": "M", "event_ticker": "E", "market_result": "yes", "yes_count_fp": "2.00", "no_count_fp": "0.00",
         "yes_total_cost_dollars": "1.76", "no_total_cost_dollars": "0", "revenue": 200,
         "settled_time": "2026-11-22T23:05:00Z", "fee_cost": "0.02"}
    c, _ = client((200, {"settlements": [s], "cursor": ""}))
    out = c.settlements(NOW - 3600)
    assert out == [{"settle_id": "M:2026-11-22T23:05:00Z:yes", "market": "M", "side": "yes", "qty": 2,
                    "payout": "2.000000", "ts": out[0]["ts"]}]
    c, _ = client((200, {"settlements": [s | {"market_result": "scalar"}], "cursor": ""}))
    with pytest.raises(DataError):
        c.settlements(NOW - 3600)


def test_server_time_comes_from_date_header():
    c, _ = client((200, {"exchange_active": True, "trading_active": True}))
    assert abs(c.server_time() - NOW) < 1


def test_inactive_exchange_is_an_error():
    c, _ = client((200, {"exchange_active": False, "trading_active": False}))
    with pytest.raises(ExchangeError):
        c.server_time()


# ------------------------------------------------------------------ safety by construction
def test_client_has_no_money_moving_endpoints():
    src = (Path(__file__).resolve().parents[1] / "atlas" / "exchange" / "kalshi.py").read_text()
    for forbidden in ("/transfer", "withdraw", "/deposits", "/api_keys", "intra_exchange", "subaccounts/transfer",
                      "target_balance_allocation"):
        assert forbidden not in src.lower().replace("no money-moving endpoints (transfers, withdrawals", ""), forbidden


def test_legacy_v1_write_endpoint_is_not_used():
    src = (Path(__file__).resolve().parents[1] / "atlas" / "exchange" / "kalshi.py").read_text()
    assert '"POST", "/portfolio/orders"' not in src
