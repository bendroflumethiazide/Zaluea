"""A fake exchange for tests and drills. It never touches real money.

It behaves like a Kalshi-style venue at its boundary (money as decimal
strings, unique client order ids, order states) and can inject the faults
that cause real-world accounting bugs:

    "timeout_after_accept"  the order IS placed, but the caller gets a timeout
    "timeout_before_accept" nothing is placed, and the caller gets a timeout
    "error_after_accept"    the order IS placed, but the caller gets a 5xx error
    "reject"                the exchange rejects the order
    "down"                  every call fails with ConnectionError
    "slow_cancel"           a cancel is acknowledged but confirmed one poll later
    "partial"               only one contract fills; an IOC order cancels the rest

Each side of a market (yes/no) has its own simple book: best bid, best ask
and the size available at the ask.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field


class ExchangeError(Exception):
    pass


class DuplicateOrder(ExchangeError):
    pass


class OrderRejected(ExchangeError):
    pass


U = 1_000_000          # units per dollar ($0.000001), like the real API's 6 decimals


def _u(s: str) -> int:
    """'0.8800' -> 880000 units of $0.000001 (strings only, never floats)."""
    whole, _, frac = s.partition(".")
    return int(whole) * U + int((frac + "000000")[:6])


def _s(u: int) -> str:
    return f"{'-' if u < 0 else ''}{abs(u) // U}.{abs(u) % U:06d}"


def _fee(qty: int, price: int) -> int:
    num = 7 * qty * price * (U - price) * 100
    cents = -(-num // (100 * U * U))
    return cents * (U // 100)


@dataclass
class Book:
    bid: int
    ask: int
    ask_size: int
    bid_size: int = 1000


@dataclass
class FakeOrder:
    client_order_id: str
    order_id: str
    market: str
    side: str
    action: str
    qty: int
    limit: int
    tif: str
    status: str = "open"           # open | filled | cancelled | rejected | expired
    filled: int = 0
    cancel_pending: bool = False
    expiration_ts: float | None = None


@dataclass
class FakeExchange:
    balance_units: int = 50 * U
    clock: callable = time.time
    markets: dict = field(default_factory=dict)        # market -> {"event", "module", "books": {side: Book}}
    orders: dict = field(default_factory=dict)         # client_order_id -> FakeOrder
    fills_log: list = field(default_factory=list)
    settlements_log: list = field(default_factory=list)
    positions_map: dict = field(default_factory=dict)  # (market, side) -> [qty, cost_units]
    faults: list = field(default_factory=list)         # consumed in order by place_order/cancel
    down: bool = False
    calls: int = 0

    def __post_init__(self):
        self._ids = itertools.count(1)

    # ---------------- setup helpers (tests) ----------------
    def add_market(self, market: str, event: str, module: str = "kalshi", yes=("0.86", "0.88", 50),
                   no=("0.11", "0.13", 50)):
        self.markets[market] = {"event": event, "module": module, "books": {
            "yes": Book(_u(yes[0]), _u(yes[1]), yes[2]), "no": Book(_u(no[0]), _u(no[1]), no[2])},
            "ts": self.clock()}

    def set_book(self, market: str, side: str, bid: str, ask: str, ask_size: int):
        self.markets[market]["books"][side] = Book(_u(bid), _u(ask), ask_size)
        self.markets[market]["ts"] = self.clock()

    def _check_up(self):
        self.calls += 1
        if self.down or "down" in self.faults:
            raise ConnectionError("exchange unreachable")

    # ---------------- read API ----------------
    def server_time(self) -> float:
        self._check_up()
        return self.clock()

    def orderbook(self, market: str, side: str) -> dict:
        self._check_up()
        m = self.markets[market]
        b = m["books"][side]
        return {"market": market, "side": side, "bid": _s(b.bid), "ask": _s(b.ask),
                "ask_size": b.ask_size, "ts": m["ts"]}

    def market_info(self, market: str) -> dict:
        self._check_up()
        m = self.markets[market]
        return {"market": market, "event": m["event"], "module": m["module"]}

    def balance(self) -> str:
        self._check_up()
        return _s(self.balance_units)

    def positions(self) -> list[dict]:
        self._check_up()
        return [{"market": mk, "side": sd, "qty": q, "cost": _s(c)}
                for (mk, sd), (q, c) in self.positions_map.items() if q > 0]

    def resting_orders(self) -> list[dict]:
        self._check_up()
        self._expire()
        return [self._view(o) for o in self.orders.values() if o.status == "open"]

    def fills(self, since_ts: float = 0.0) -> list[dict]:
        self._check_up()
        return [f for f in self.fills_log if f["ts"] >= since_ts]

    def settlements(self, since_ts: float = 0.0) -> list[dict]:
        self._check_up()
        return [s for s in self.settlements_log if s["ts"] >= since_ts]

    def get_order(self, client_order_id: str) -> dict | None:
        self._check_up()
        self._expire()
        o = self.orders.get(client_order_id)
        if o is None:
            return None
        if o.cancel_pending:                       # a slow cancel confirms on the next poll
            o.cancel_pending = False
            o.status = "cancelled"
        return self._view(o)

    def _view(self, o: FakeOrder) -> dict:
        return {"client_order_id": o.client_order_id, "order_id": o.order_id, "market": o.market,
                "side": o.side, "action": o.action, "qty": o.qty, "filled": o.filled,
                "remaining": o.qty - o.filled if o.status == "open" else 0,
                "limit": _s(o.limit), "status": o.status}

    def _expire(self):
        now = self.clock()
        for o in self.orders.values():
            if o.status == "open" and o.expiration_ts and now >= o.expiration_ts:
                o.status = "expired"

    # ---------------- trading API ----------------
    def place_order(self, client_order_id: str, market: str, side: str, action: str, qty: int,
                    limit_price: str, tif: str = "ioc", expiration_ts: float | None = None) -> dict:
        self._check_up()
        fault = self.faults.pop(0) if self.faults else None
        if fault == "timeout_before_accept":
            raise TimeoutError("request timed out")
        if client_order_id in self.orders:
            raise DuplicateOrder(f"client_order_id {client_order_id} already used")
        if fault == "reject":
            raise OrderRejected("insufficient balance or market closed")
        limit = _u(limit_price)
        o = FakeOrder(client_order_id, f"ex-{next(self._ids)}", market, side, action, qty, limit, tif,
                      expiration_ts=expiration_ts)
        self.orders[client_order_id] = o
        self._match(o, max_fill=1 if fault == "partial" else None)
        if fault == "timeout_after_accept":
            raise TimeoutError("request timed out")
        if fault == "error_after_accept":
            raise ExchangeError("HTTP 502 Bad Gateway")
        return self._view(o)

    def _match(self, o: FakeOrder, max_fill: int | None = None):
        book = self.markets[o.market]["books"][o.side]
        if o.action == "buy" and o.limit >= book.ask and book.ask_size > 0:
            q = min(o.qty, book.ask_size, max_fill or o.qty)
            self._fill(o, q, book.ask)
            book.ask_size -= q
        elif o.action == "sell" and o.limit <= book.bid:
            held = self.positions_map.get((o.market, o.side), [0, 0])[0]
            q = min(o.qty, held, book.bid_size)
            if q:
                self._fill(o, q, book.bid)
        if o.filled == o.qty:
            o.status = "filled"
        elif o.tif == "ioc":
            o.status = "cancelled"

    def _fill(self, o: FakeOrder, q: int, px: int):
        fee = _fee(q, px)
        o.filled += q
        pos = self.positions_map.setdefault((o.market, o.side), [0, 0])
        if o.action == "buy":
            self.balance_units -= q * px + fee
            pos[0] += q
            pos[1] += q * px + fee
        else:
            self.balance_units += q * px - fee
            removed = pos[1] if q == pos[0] else pos[1] * q // pos[0]
            pos[0] -= q
            pos[1] -= removed
        self.fills_log.append({"fill_id": f"fill-{next(self._ids)}", "client_order_id": o.client_order_id,
                               "market": o.market, "side": o.side, "action": o.action, "qty": q,
                               "price": _s(px), "fee": _s(fee), "ts": self.clock()})

    def cancel(self, client_order_id: str) -> dict:
        self._check_up()
        fault = self.faults.pop(0) if self.faults else None
        o = self.orders.get(client_order_id)
        if o is None:
            raise ExchangeError("unknown order")
        if o.status == "open":
            if fault == "slow_cancel":
                o.cancel_pending = True
                return self._view(o) | {"status": "cancel_requested"}
            o.status = "cancelled"
        return self._view(o)

    def settle(self, market: str, winning_side: str):
        """Settle a prediction market: winners get $1 per contract."""
        for (mk, sd), pos in list(self.positions_map.items()):
            if mk == market and pos[0] > 0:
                payout = pos[0] * U if sd == winning_side else 0
                self.balance_units += payout
                self.settlements_log.append({"settle_id": f"settle-{next(self._ids)}", "market": mk, "side": sd,
                                             "qty": pos[0], "payout": _s(payout), "ts": self.clock()})
                pos[0], pos[1] = 0, 0
