"""Crypto 15-minute RESTING-ORDER (maker) strategy. Shadow testing only.

Why: the interim backtest found the favorite-buying taker strategy pays the spread and the
taker fee and loses. A resting bid earns the spread instead and pays a smaller fee (none at
all on series whose fee type is plain `quadratic`). The risk is adverse selection: resting
bids tend to get filled exactly when the price is about to fall. Only shadow testing on live
public trades can measure that, which is what this strategy is for.

Rule: during minutes `entry_start_min`-`entry_end_min`, for each side compute the model
probability (same model as the flip strategy). Quote at the best bid, or one cent better
when the spread is at least `improve_spread`, but never at or above the ask (post-only). Rest only if
  model value - price - assumed maker fee  >=  min_edge per contract,
the model does not disagree with the market mid by more than `max_disagreement_bp`
(a large disagreement usually means the model is missing information), and no flip guard fires.
The runner cancels the order when the strategy stops recommending it, when the recommended price
falls below the resting price, or at minute `cancel_min`; filled contracts are held to settlement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..analytics.probability import BP, expected_value
from ..money import KALSHI_ROUNDING_ALLOWANCE, Money, kalshi_taker_fee, kalshi_worst_case_fee
from .base import MarketView, NoTrade, Signal, Strategy, StrategyConfig
from .crypto_flip import phi, sigma_per_minute

ONE = Money.dollars(1)
CENT = Money.cents(1)


@dataclass(frozen=True)
class MakerConfig(StrategyConfig):
    entry_start_min: int = 2
    entry_end_min: int = 13
    cancel_min: int = 14
    min_price: str = "0.10"            # cent ticks only (this series uses 0.1-cent ticks near 0 and 1)
    max_price: str = "0.90"
    improve_spread: str = "0.02"
    min_edge: str = "0.03"             # per contract, after the assumed maker fee
    max_disagreement_bp: int = 1500
    maker_fee_pct: int = 25            # assumed maker fee as % of the taker formula (pessimistic)
    vol_scale: str = "1.05"
    vol_lookback_s: int = 3600
    min_returns: int = 30
    max_ref_age_s: int = 30
    momentum_window_s: int = 120
    momentum_drop: str = "0.05"
    jump_window_s: int = 60
    jump_sigmas: str = "1.0"


def maker_fee(qty: int, price: Money, multiplier_pct: int, maker_fee_pct: int) -> Money:
    if not maker_fee_pct:
        return Money.ZERO
    return kalshi_taker_fee(qty, price, multiplier_pct * maker_fee_pct // 100) + KALSHI_ROUNDING_ALLOWANCE


class CryptoMaker(Strategy):
    name = "crypto_maker"
    module = "kalshi"
    kind = "maker"

    def __init__(self, config: MakerConfig = MakerConfig()):
        super().__init__(config)
        c = config
        self.min_price, self.max_price = Money.parse(c.min_price), Money.parse(c.max_price)
        self.improve, self.min_edge = Money.parse(c.improve_spread), Money.parse(c.min_edge)
        self.momentum_drop = Money.parse(c.momentum_drop)
        self.k, self.jump_sigmas = float(c.vol_scale), float(c.jump_sigmas)

    def _model(self, v: MarketView):
        """(z_yes, jump_move, jump_limit) or a reason string."""
        c = self.config
        if v.strike is None:
            return "Market has no strike price"
        ref = v.reference.at(v.now)
        if ref is None or v.now - ref[0] > c.max_ref_age_s:
            return "BTC reference price is missing or stale"
        s_ref = ref[1]
        sv = sigma_per_minute(v.reference.window(v.now - c.vol_lookback_s, v.now))
        if sv is None or sv[1] < c.min_returns or sv[0] <= 0:
            return "Not enough recent BTC prices to measure volatility"
        sigma = sv[0]
        t_eff = max((v.close_ts - v.now) / 60.0 - 2.0 / 3.0, 1.0 / 3.0)
        z_yes = math.log(s_ref.units / v.strike.units) / (self.k * sigma * math.sqrt(t_eff))
        earlier = v.reference.at(v.now - c.jump_window_s)
        jump_move = s_ref.units - earlier[1].units if earlier else None
        return z_yes, jump_move, self.jump_sigmas * sigma * math.sqrt(c.jump_window_s / 60.0) * s_ref.units

    def keep(self, v: MarketView, side: str, price: Money) -> str | None:
        """Should a bid ALREADY RESTING at `price` stay? None = keep; otherwise the reason to cancel.
        Value-based, not tick-chasing: the bid stays while the model still values it above its own price
        after fees and no flip guard fires. (Cancelling on every one-cent wiggle would mean the order
        almost never fills, so the test would never collect evidence.)"""
        if (v.now - v.open_ts) / 60.0 >= self.config.cancel_min:
            return f"minute {self.config.cancel_min} reached"
        m = self._model(v)
        if isinstance(m, str):
            return m
        r = self._side(v, side, *m, price=price)
        return r if isinstance(r, str) else None

    def evaluate(self, v: MarketView) -> Signal | NoTrade:
        c = self.config
        no = lambda reasons, **d: NoTrade(self.name, v.market, reasons, d)
        minute = (v.now - v.open_ts) / 60.0
        if not c.entry_start_min <= minute < c.entry_end_min:
            return no([f"Minute {minute:.1f}: resting orders are placed only in minutes "
                       f"{c.entry_start_min}-{c.entry_end_min}"])
        m = self._model(v)
        if isinstance(m, str):
            return no([m])
        z_yes, jump_move, jump_limit = m

        best, why = None, []
        for side in ("yes", "no"):
            r = self._side(v, side, z_yes, jump_move, jump_limit)
            if isinstance(r, str):
                why.append(f"{side.upper()}: {r}")
            elif best is None or r.expected_value > best.expected_value:
                best = r
        return best if best is not None else no(why, z_yes=z_yes)

    def _side(self, v: MarketView, side: str, z_yes: float, jump_move, jump_limit,
              price: Money | None = None) -> Signal | str:
        c = self.config
        q = v.quotes[side].at(v.now)
        if not q or q[1][0] is None or q[1][1] is None:
            return "no two-sided quote"
        bid, ask = q[1][0], q[1][1]
        if price is None:                              # a new order: choose the price, never cross
            price = bid + CENT if ask - bid >= self.improve else bid
            if price >= ask:
                return f"price {price} would cross the ask {ask}"
        if not self.min_price <= price <= self.max_price:
            return f"price {price} is outside {self.min_price}-{self.max_price}"
        z = z_yes if side == "yes" else -z_yes
        prob_bp = min(max(int(phi(z) * BP), 1), BP - 1)
        mid_bp = (bid + ask).units * BP // (2 * ONE.units)
        if abs(prob_bp - mid_bp) > c.max_disagreement_bp:
            return (f"model {prob_bp / 100:.1f}% disagrees with the market {mid_bp / 100:.1f}% "
                    f"by more than {c.max_disagreement_bp / 100:.0f} points")
        before = v.quotes[side].at(v.now - c.momentum_window_s)
        if not before or before[1][0] is None or before[1][1] is None:     # fail closed: no history, no guard
            return f"no price from {c.momentum_window_s} s ago to check for a flip"
        if (before[1][0] + before[1][1]) - (bid + ask) > self.momentum_drop * 2:
            return f"price fell more than {self.momentum_drop} in {c.momentum_window_s} s"
        if jump_move is not None:
            toward = -jump_move if side == "yes" else jump_move      # BTC moving against this side
            if toward > jump_limit:
                return f"BTC moved {Money(int(toward))} against this side in {c.jump_window_s} s"
        qty = 0
        while price * (qty + 1) + kalshi_worst_case_fee(qty + 1, price, v.fee_multiplier_pct) <= v.max_order:
            qty += 1                                   # sized on the TAKER worst case: never under-reserved
        if qty == 0:
            return f"one contract at {price} exceeds the {v.max_order} purchase limit"
        fee = maker_fee(qty, price, v.fee_multiplier_pct, c.maker_fee_pct)
        ev = expected_value(prob_bp, price, qty, fee)
        if ev < self.min_edge * qty:
            return f"value {ev} for {qty} at {price} is below {self.min_edge} per contract"
        return Signal(self.name, self.version, v.market, side, qty, price, prob_bp, mid_bp, ev, fee,
                      [f"model {prob_bp / 100:.1f}% vs bid {price}", f"EV {ev} after assumed maker fee {fee}"],
                      ["kalshi:orderbook", "btc:multi-exchange-median"],
                      f"Rest until filled or minute {c.cancel_min}; hold fills to settlement")
