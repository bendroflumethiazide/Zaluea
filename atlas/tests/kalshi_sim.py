"""A Kalshi API simulator: speaks the real V2 HTTP shapes, backed by FakeExchange.

Used to run the real KalshiExchange client (and therefore the whole gateway
stack) end to end without a network or an account. Faults can be injected
at the HTTP layer, e.g. a timeout AFTER the order was processed.
"""
from __future__ import annotations

import email.utils
import json
import urllib.parse
from datetime import datetime, timezone

from atlas.exchange.fake import FakeExchange, _s, _u

U = 1_000_000


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class KalshiSim:
    def __init__(self, ex: FakeExchange, series: dict | None = None):
        self.ex = ex
        self.series = series or {}
        self.http_faults: list[str] = []          # "timeout_after", "timeout_before", "502_after"
        self.requests: list[tuple[str, str, dict | None]] = []

    # ------------------------------------------------------------------ transport
    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlparse(url)
        path = u.path.removeprefix("/trade-api/v2")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.requests.append((method, path, body))
        if path.startswith("/portfolio") or path.startswith("/exchange/user_data"):
            assert "KALSHI-ACCESS-SIGNATURE" in headers, "portfolio calls must be signed"
        fault = self.http_faults.pop(0) if (self.http_faults and method != "GET") else None
        if fault == "timeout_before":
            raise TimeoutError("simulated timeout before the exchange saw the request")
        status, payload = self.route(method, path, q, body)
        if fault == "timeout_after":
            raise TimeoutError("simulated timeout after the exchange processed the request")
        if fault == "502_after":
            return 502, {}, {"error": {"code": "bad_gateway"}}
        return status, {"Date": email.utils.formatdate(self.ex.clock(), usegmt=True)}, payload

    # ------------------------------------------------------------------ routes
    def route(self, method, path, q, body):
        ex = self.ex
        try:
            ex._check_up()
        except ConnectionError:
            raise
        if method == "GET" and path == "/exchange/status":
            return 200, {"exchange_active": True, "trading_active": True}
        if method == "GET" and path == "/exchange/user_data_timestamp":
            return 200, {"as_of_time": _iso(ex.clock())}
        if method == "GET" and path.startswith("/series/"):
            s = path.split("/")[2]
            meta = self.series.get(s, {"tags": [], "category": "Crypto", "fee_type": "quadratic", "fee_multiplier": 1})
            return 200, {"series": {"ticker": s, **meta}}
        if method == "GET" and path.startswith("/markets/") and path.endswith("/orderbook"):
            t = urllib.parse.unquote(path.split("/")[2])
            yes = ex.markets[t]["books"]["yes"]
            return 200, {"orderbook_fp": {"yes_dollars": [[_s(yes.bid), f"{yes.bid_size}.00"]],
                                          "no_dollars": [[_s(U - yes.ask), f"{yes.ask_size}.00"]]}}
        if method == "GET" and path.startswith("/markets/"):
            t = urllib.parse.unquote(path.split("/")[2])
            m = ex.markets[t]
            return 200, {"market": {"ticker": t, "event_ticker": m["event"], "status": "active",
                                    "close_time": _iso(ex.clock() + 600), "exchange_index": 0,
                                    "market_type": "binary"}}
        if method == "GET" and path == "/portfolio/balance":
            return 200, {"balance": ex.balance_units // 10_000, "balance_dollars": _s(ex.balance_units),
                         "portfolio_value": 0, "updated_ts": int(ex.clock())}
        if method == "GET" and path == "/portfolio/positions":
            out = []
            for (mk, sd), (qty, cost) in ex.positions_map.items():
                if qty:
                    out.append({"ticker": mk, "position_fp": f"{qty if sd == 'yes' else -qty}.00",
                                "market_exposure_dollars": _s(cost)})
            return 200, {"market_positions": out, "event_positions": [], "cursor": ""}
        if method == "GET" and path == "/portfolio/orders":
            ex._expire()
            orders = [self._order(o) for o in ex.orders.values()
                      if (not q.get("ticker") or o.market == q["ticker"])
                      and (not q.get("status") or self._status(o) == q["status"])]
            return 200, {"orders": orders, "cursor": ""}
        if method == "GET" and path == "/portfolio/fills":
            out = []
            for f in ex.fills_log:
                o = ex.orders[f["client_order_id"]]
                outcome = f["side"] if f["action"] == "buy" else ("no" if f["side"] == "yes" else "yes")
                px = _u(f["price"])
                yes_px = px if f["side"] == "yes" else U - px
                out.append({"fill_id": f["fill_id"], "trade_id": f["fill_id"], "order_id": o.order_id,
                            "ticker": f["market"], "market_ticker": f["market"], "outcome_side": outcome,
                            "book_side": "bid" if outcome == "yes" else "ask", "count_fp": f"{f['qty']}.00",
                            "yes_price_dollars": _s(yes_px), "no_price_dollars": _s(U - yes_px),
                            "is_taker": True, "fee_cost": f["fee"], "ts": int(f["ts"]), "exchange_index": 0})
            return 200, {"fills": out, "cursor": ""}
        if method == "GET" and path == "/portfolio/settlements":
            by_market = {}
            for s in ex.settlements_log:
                d = by_market.setdefault(s["market"], {"ticker": s["market"], "event_ticker": s["market"],
                                                       "yes_count_fp": "0.00", "no_count_fp": "0.00",
                                                       "settled_time": _iso(s["ts"]), "market_result": None})
                d[f"{s['side']}_count_fp"] = f"{s['qty']}.00"
                won = _u(s["payout"]) > 0
                d["market_result"] = s["side"] if won else ("no" if s["side"] == "yes" else "yes")
            return 200, {"settlements": [d | {"revenue": 0, "fee_cost": "0", "yes_total_cost_dollars": "0",
                                              "no_total_cost_dollars": "0"} for d in by_market.values()],
                         "cursor": ""}
        if method == "POST" and path == "/portfolio/events/orders":
            return self._create(body)
        if method == "DELETE" and path.startswith("/portfolio/events/orders/"):
            oid = path.split("/")[-1].split("?")[0]
            o = next(o for o in ex.orders.values() if o.order_id == oid)
            ex.cancel(o.client_order_id)
            return 200, {"order_id": oid, "client_order_id": o.client_order_id, "reduced_by": "1.00", "ts_ms": 1}
        return 404, {"error": {"code": "not_found"}}

    def _create(self, b):
        ex = self.ex
        coid = b["client_order_id"]
        if coid in ex.orders:
            return 409, {"error": {"code": "order_already_exists"}}
        _, side, action, _ = coid.split("-", 3)           # the sim reads ATLAS's intent like the real
        price = _u(b["price"])                             # exchange's netting would economically
        own_price = price if side == "yes" else U - price
        tif = {"immediate_or_cancel": "ioc", "fill_or_kill": "ioc", "good_till_canceled": "gtc"}[b["time_in_force"]]
        try:
            v = ex.place_order(coid, b["ticker"], side, action, int(b["count"]), _s(own_price), tif=tif)
        except Exception as e:
            return 400, {"error": {"code": "invalid_order", "details": str(e)}}
        return 201, {"order_id": v["order_id"], "client_order_id": coid, "fill_count": f"{v['filled']}.00",
                     "remaining_count": f"{v['remaining']}.00", "ts_ms": int(ex.clock() * 1000)}

    def _status(self, o):
        return {"open": "resting", "filled": "executed"}.get(o.status, "canceled")

    def _order(self, o):
        own = o.limit
        yes_px = own if o.side == "yes" else U - own
        outcome = o.side if o.action == "buy" else ("no" if o.side == "yes" else "yes")
        return {"order_id": o.order_id, "user_id": "sim", "client_order_id": o.client_order_id, "ticker": o.market,
                "outcome_side": outcome, "book_side": "bid" if outcome == "yes" else "ask", "type": "limit",
                "status": self._status(o), "yes_price_dollars": _s(yes_px), "no_price_dollars": _s(U - yes_px),
                "fill_count_fp": f"{o.filled}.00", "remaining_count_fp": f"{o.qty - o.filled if o.status == 'open' else 0}.00",
                "initial_count_fp": f"{o.qty}.00", "taker_fees_dollars": "0", "maker_fees_dollars": "0",
                "taker_fill_cost_dollars": "0", "maker_fill_cost_dollars": "0"}
