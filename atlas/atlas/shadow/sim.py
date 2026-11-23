"""Simulated exchange for SHADOW TRADING. It has no connection to any real order system.

Fill rules are deliberately conservative (a shadow test that flatters a strategy is worse
than none):

  * Latency: an order becomes active `latency_s` after submission, against the book as it
    is THEN, not when the decision was made.
  * Taker (immediate-or-cancel) orders fill at the best ask only up to the size displayed at
    that price; the rest is cancelled (a partial fill). If the ask moved above the limit, nothing fills.
  * Resting (maker) orders fill ONLY when a real public trade, printed after the order became
    active, sold into it: a trade strictly through the price fills us; a trade AT the price
    first consumes the size that was queued ahead of us when we joined.
  * Fees: charged per fill exactly as Kalshi's fee_rounding docs describe (trade fee rounded to
    $0.000001, a rounding fee back to a $0.01 balance grid, rebates through a per-order accumulator).
    Taker fills use the 0.07 rate. Maker fills use `maker_fee_pct` percent of it (default 25% =
    0.0175, a pessimistic assumption: the maker rule for this series could not be verified).
  * Orders expire at their expiry time; markets settle from the exchange's published result.

Accounting uses the real ATLAS Ledger class on a SEPARATE file, so simulated money gets the
same exact arithmetic and the same hard limits, without ever touching the real ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..money import KalshiFeeAccumulator, Money

ONE = Money.dollars(1)
TAKER_RATE_BP = 700            # Kalshi's 0.07 quadratic rate


@dataclass
class Book:
    bid: Money | None
    ask: Money | None
    bid_size: int
    ask_size: int
    ts: float


@dataclass
class PublicTrade:
    trade_id: str
    yes_price: Money
    count: int
    taker_side: str            # outcome side the TAKER bought ('yes' or 'no')
    ts: float


@dataclass
class SimOrder:
    coid: str
    market: str
    side: str
    qty: int
    limit: Money
    kind: str                  # "taker" | "maker"
    submitted: float
    active_at: float
    expires_at: float
    fee_multiplier_pct: int
    filled: int = 0
    status: str = "pending"    # pending | open | filled | cancelled | expired
    queue_ahead: int = 0
    fills: list = field(default_factory=list)   # (fill_id, qty, price, fee, ts)
    cancel_at: float | None = None
    end_reason: str = ""
    fees: KalshiFeeAccumulator = field(default_factory=KalshiFeeAccumulator)   # per-order, as Kalshi does


class SimExchange:
    def __init__(self, latency_s: float = 1.0, maker_fee_pct: int = 25):
        self.latency_s = latency_s
        self.maker_fee_pct = maker_fee_pct
        self.books: dict[tuple[str, str], Book] = {}
        self.orders: dict[str, SimOrder] = {}
        self.seen_trades: set[str] = set()

    # ------------------------------------------------------------------ market data in
    def update_book(self, market: str, side: str, book: Book) -> None:
        self.books[(market, side)] = book

    def submit(self, coid: str, market: str, side: str, qty: int, limit: Money, kind: str, now: float,
               expires_at: float, fee_multiplier_pct: int = 100) -> SimOrder:
        if coid in self.orders:
            raise ValueError("duplicate client order id")
        o = SimOrder(coid, market, side, qty, limit, kind, now, now + self.latency_s, expires_at, fee_multiplier_pct)
        self.orders[coid] = o
        return o

    def cancel(self, coid: str, now: float) -> None:
        o = self.orders[coid]
        if o.status in ("pending", "open") and o.cancel_at is None:
            o.cancel_at = now + self.latency_s          # a cancel also takes time to land

    # ------------------------------------------------------------------ the clock moves
    def step(self, now: float, trades_by_market: dict[str, list[PublicTrade]] | None = None) -> list[tuple]:
        """Advance simulated time to `now`, feeding public trades seen since the last step.
        Returns new fills as (coid, fill_id, qty, price, fee, ts). A trade printed after `now`
        is never used (it would be future data) and is kept for a later step."""
        trades_by_market = {m: [t for t in ts if t.ts <= now and t.trade_id not in self.seen_trades]
                            for m, ts in (trades_by_market or {}).items()}
        left = {t.trade_id: t.count for ts in trades_by_market.values() for t in ts}   # shared liquidity
        new_fills = []
        for o in self.orders.values():
            if o.status in ("filled", "cancelled", "expired"):
                continue
            if o.status == "pending" and now >= o.active_at:
                self._activate(o, new_fills)
            if o.status == "open" and o.kind == "maker":
                for t in sorted(trades_by_market.get(o.market, []), key=lambda t: t.ts):
                    if t.ts < o.active_at or (o.cancel_at is not None and t.ts >= o.cancel_at):
                        continue
                    left[t.trade_id] = self._maker_match(o, t, left[t.trade_id], new_fills)
                    if o.status != "open":
                        break
            if o.status == "open" and o.cancel_at is not None and now >= o.cancel_at:
                o.status, o.end_reason = "cancelled", "withdrawn by the strategy"
            if o.status == "open" and now >= o.expires_at:
                o.status, o.end_reason = "expired", "reached its time limit"
        for ts in trades_by_market.values():
            self.seen_trades.update(t.trade_id for t in ts)
        return new_fills

    def open_orders(self) -> list[SimOrder]:
        return [o for o in self.orders.values() if o.status in ("pending", "open")]

    def _activate(self, o: SimOrder, out: list) -> None:
        b = self.books.get((o.market, o.side))
        if o.kind == "taker":
            if b and b.ask is not None and b.ask <= o.limit and b.ask_size > 0:
                q = min(o.qty, b.ask_size)
                fee = o.fees.fill(q, b.ask, TAKER_RATE_BP, o.fee_multiplier_pct)
                self._fill(o, q, b.ask, fee, o.active_at, out)
                b.ask_size -= q                          # we consumed that liquidity
            o.status = "filled" if o.filled == o.qty else "cancelled"   # IOC: the rest is cancelled
            if o.status == "cancelled":
                o.end_reason = ("not enough shown at the ask" if o.filled else
                                "the ask moved above the limit or nothing was offered")
            return
        # maker: never cross the book (post-only); join the queue behind displayed size at our price
        if b and b.ask is not None and o.limit >= b.ask:
            o.status = "cancelled"                       # would have crossed: rejected as post-only
            o.end_reason = "post-only: the ask fell to our price before the order went live"
            return
        o.queue_ahead = b.bid_size if (b and b.bid is not None and b.bid == o.limit) else 0
        o.status = "open"

    def _maker_match(self, o: SimOrder, t: PublicTrade, available: int, out: list) -> int:
        """Match one public trade against a resting order. Returns the trade size still unused."""
        # Someone SOLD our side into the book: for a YES bid the taker bought NO, and vice versa.
        if t.taker_side == o.side or available <= 0:
            return available
        price_for_us = t.yes_price if o.side == "yes" else ONE - t.yes_price
        if price_for_us > o.limit:
            return available
        if price_for_us == o.limit:                      # at our price: the queue ahead goes first
            used = min(o.queue_ahead, available)
            o.queue_ahead -= used
            available -= used
        q = min(available, o.qty - o.filled)
        if q <= 0:
            return available
        fee = o.fees.fill(q, o.limit, TAKER_RATE_BP * self.maker_fee_pct // 100, o.fee_multiplier_pct)
        self._fill(o, q, o.limit, fee, t.ts, out)
        if o.filled == o.qty:
            o.status = "filled"
        return available - q

    def maker_fee(self, qty: int, price: Money, fee_multiplier_pct: int) -> Money:
        """Fee of ONE resting fill of `qty` at the assumed maker rate (used for hypothetical results)."""
        return KalshiFeeAccumulator().fill(qty, price, TAKER_RATE_BP * self.maker_fee_pct // 100, fee_multiplier_pct)

    @staticmethod
    def taker_fee(qty: int, price: Money, fee_multiplier_pct: int) -> Money:
        """Fee of ONE taker fill of `qty` (used for hypothetical results)."""
        return KalshiFeeAccumulator().fill(qty, price, TAKER_RATE_BP, fee_multiplier_pct)

    def _fill(self, o: SimOrder, q: int, price: Money, fee: Money, ts: float, out: list) -> None:
        o.filled += q
        fill_id = f"{o.coid}-fill-{len(o.fills) + 1}"        # unique across restarts (coids are UUIDs)
        o.fills.append((fill_id, q, price, fee, ts))
        out.append((o.coid, fill_id, q, price, fee, ts))
