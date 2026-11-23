"""Crypto 15-minute "flip-aware favorite" strategy (rebuild of the original bot's idea).

Late in each 15-minute window, buy the favorite only when BTC is far enough
from the strike that a flip is unlikely, the price is fair after fees, and no
flip appears to be under way.

Probability model (an ESTIMATE, labelled unvalidated until scored):
    P(up) = Phi( ln(S/K) / (k * sigma * sqrt(T_eff)) )
  S      latest multi-exchange BTC reference price
  K      the market's official strike (Kalshi floor_strike)
  sigma  std. dev. of 1-minute log returns of the reference over the lookback
  T_eff  minutes left minus 2/3 (settlement averages the final 60 seconds)
  k      volatility scale, fitted ONLY on past data in walk-forward tests

Floats are used for the probability model; every money figure (price, fee,
size, expected value) is exact Money.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..analytics.probability import BP, expected_value
from ..money import Money, kalshi_worst_case_fee
from .base import MarketView, NoTrade, Signal, Strategy, StrategyConfig

ONE = Money.dollars(1)


@dataclass(frozen=True)
class FlipConfig(StrategyConfig):
    entry_start_min: int = 10
    entry_end_min: int = 14
    min_price: str = "0.80"
    max_price: str = "0.97"
    z_min: str = "2.0"
    vol_scale: str = "1.05"
    vol_lookback_s: int = 3600
    min_returns: int = 30
    max_ref_age_s: int = 90
    momentum_window_s: int = 120
    momentum_drop: str = "0.05"
    jump_window_s: int = 60
    jump_sigmas: str = "1.0"
    max_spread: str = "0.04"
    min_ev: str = "0.01"
    slippage_per_contract: str = "0.00"


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sigma_per_minute(points: list[tuple[float, Money]]) -> float | None:
    rets = []
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        dt = t1 - t0
        if dt <= 0 or p0.units <= 0 or p1.units <= 0:
            continue
        rets.append(math.log(p1.units / p0.units) / math.sqrt(dt / 60.0))   # scale each return to 1 minute
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)), len(rets)


class CryptoFlipFavorite(Strategy):
    name = "crypto_flip_favorite"
    module = "kalshi"

    def __init__(self, config: FlipConfig = FlipConfig()):
        super().__init__(config)
        c = config
        self.min_price, self.max_price = Money.parse(c.min_price), Money.parse(c.max_price)
        self.momentum_drop, self.max_spread = Money.parse(c.momentum_drop), Money.parse(c.max_spread)
        self.min_ev, self.slip = Money.parse(c.min_ev), Money.parse(c.slippage_per_contract)
        self.z_min, self.k, self.jump_sigmas = float(c.z_min), float(c.vol_scale), float(c.jump_sigmas)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def side_quote(view: MarketView, side: str, t: float):
        q = view.quotes[side].at(t)
        return q[1] if q else None

    def evaluate(self, v: MarketView) -> Signal | NoTrade:
        c, why = self.config, []
        no = lambda reasons, **d: NoTrade(self.name, v.market, reasons, d)
        minute = (v.now - v.open_ts) / 60.0
        if not c.entry_start_min <= minute < c.entry_end_min:
            return no([f"Minute {minute:.1f}: this strategy only acts in minutes {c.entry_start_min}-{c.entry_end_min}"])
        if v.strike is None:
            return no(["Market has no strike price"])

        # ---- the favorite and its executable price
        yes = self.side_quote(v, "yes", v.now)
        if not yes or yes[0] is None or yes[1] is None:
            return no(["No two-sided quote"])
        yes_mid2 = yes[0] + yes[1]                         # 2 x mid, exact
        side = "yes" if yes_mid2 >= ONE else "no"
        q = self.side_quote(v, side, v.now)
        if not q or q[0] is None or q[1] is None:
            return no(["No two-sided quote on the favorite"])
        bid, ask = q[0], q[1]
        if ask - bid > self.max_spread:
            why.append(f"Spread {ask - bid} is wider than {self.max_spread}")
        if not self.min_price <= ask <= self.max_price:
            why.append(f"Favorite costs {ask}, outside {self.min_price}-{self.max_price}")

        # ---- reference price and volatility
        ref = v.reference.at(v.now)
        if ref is None or v.now - ref[0] > c.max_ref_age_s:
            return no(why + ["BTC reference price is missing or stale"])
        s_ref = ref[1]
        hist = v.reference.window(v.now - c.vol_lookback_s, v.now)
        sv = sigma_per_minute(hist)
        if sv is None or sv[1] < c.min_returns:
            return no(why + ["Not enough recent BTC prices to measure volatility"])
        sigma = sv[0]
        if sigma <= 0:
            return no(why + ["Volatility measured as zero: data unreliable"])

        t_eff = max((v.close_ts - v.now) / 60.0 - 2.0 / 3.0, 1.0 / 3.0)
        z_yes = math.log(s_ref.units / v.strike.units) / (self.k * sigma * math.sqrt(t_eff))
        z = z_yes if side == "yes" else -z_yes
        prob_bp = min(max(int(phi(z) * BP), 1), BP - 1)
        if z < self.z_min:
            why.append(f"BTC is only {z:.2f} standard deviations from the strike (needs {self.z_min})")

        # ---- flip guards
        before = self.side_quote(v, side, v.now - c.momentum_window_s)
        if before and before[0] is not None and before[1] is not None:
            drop = (before[0] + before[1]) - (bid + ask)         # 2 x mid change
            if drop > self.momentum_drop * 2:
                why.append(f"Favorite's price fell {Money(drop.units // 2)} in {c.momentum_window_s} s: flip under way")
        earlier = v.reference.at(v.now - c.jump_window_s)
        if earlier:
            move = s_ref.units - earlier[1].units
            toward = -move if side == "yes" else move
            if toward > self.jump_sigmas * sigma * math.sqrt(c.jump_window_s / 60.0) * s_ref.units:
                why.append(f"BTC jumped {Money(toward)} toward the strike in {c.jump_window_s} s")

        # ---- size and value (exact Money)
        qty, fee = 0, Money.ZERO
        while True:
            f = kalshi_worst_case_fee(qty + 1, ask, v.fee_multiplier_pct)
            if ask * (qty + 1) + f + self.slip * (qty + 1) > v.max_order:
                break
            qty, fee = qty + 1, f
        if qty == 0:
            why.append(f"Even one contract at {ask} plus fee exceeds the {v.max_order} purchase limit")
            return no(why, prob_bp=prob_bp, z=z)
        ev = expected_value(prob_bp, ask, qty, fee, self.slip * qty)
        if ev < self.min_ev:
            why.append(f"Expected value {ev} after fees is below the {self.min_ev} minimum")
        if why:
            return no(why, prob_bp=prob_bp, z=z, ev=ev)
        mid_bp = (bid + ask).units * BP // (2 * ONE.units)
        return Signal(self.name, self.version, v.market, side, qty, ask, prob_bp, mid_bp, ev, fee,
                      [f"BTC {z:.1f} sd from strike {v.strike}", f"model {prob_bp / 100:.1f}% vs price {ask}",
                       f"EV {ev} after {fee} fee"],
                      ["kalshi:orderbook", "btc:multi-exchange-median"],
                      "Hold to settlement; exit if the favorite's bid falls to $0.50")
