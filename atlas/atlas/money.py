"""Exact money for ATLAS.

All amounts are integers of UNITS, where 1 unit = $0.000001 (one millionth
of a dollar). Kalshi's API returns fill prices and costs with up to six
decimals, and exact reconciliation needs every digit, so a float (or any
rounding) is never acceptable.

`Money` refuses floats at every entry point. Authorization decisions in
ATLAS must only ever compare `Money` (or plain ints) - never floats.
"""
from __future__ import annotations

import re
from functools import total_ordering

UNITS_PER_DOLLAR = 1_000_000
UNITS_PER_CENT = 10_000
DECIMALS = 6
_DEC = re.compile(r"^\s*(-)?\$?(\d+)(?:\.(\d{1,6}))?\s*$")


class MoneyError(ValueError):
    pass


@total_ordering
class Money:
    __slots__ = ("units",)

    def __init__(self, units: int):
        if isinstance(units, bool) or not isinstance(units, int):
            raise MoneyError(f"Money needs integer units, got {type(units).__name__}")
        object.__setattr__(self, "units", units)

    def __setattr__(self, *_):
        raise AttributeError("Money is immutable")

    # ---- construction ----
    @classmethod
    def parse(cls, text: str) -> "Money":
        """'1.25', '$0.9870', '-3', '0.123456' -> Money. At most 6 decimals; no floats."""
        if not isinstance(text, str):
            raise MoneyError(f"Money.parse needs a string, got {type(text).__name__}")
        m = _DEC.match(text)
        if not m:
            raise MoneyError(f"Not a money amount: {text!r}")
        sign, whole, frac = m.groups()
        units = int(whole) * UNITS_PER_DOLLAR + int((frac or "").ljust(DECIMALS, "0") or 0)
        return cls(-units if sign else units)

    @classmethod
    def cents(cls, c: int) -> "Money":
        if isinstance(c, bool) or not isinstance(c, int):
            raise MoneyError("cents must be an int")
        return cls(c * UNITS_PER_CENT)

    @classmethod
    def dollars(cls, d: int) -> "Money":
        if isinstance(d, bool) or not isinstance(d, int):
            raise MoneyError("dollars must be an int")
        return cls(d * UNITS_PER_DOLLAR)

    ZERO: "Money"

    # ---- arithmetic (Money with Money, or Money scaled by an int) ----
    def _other(self, o) -> int:
        if not isinstance(o, Money):
            raise MoneyError(f"Cannot combine Money with {type(o).__name__}")
        return o.units

    def __add__(self, o): return Money(self.units + self._other(o))
    def __sub__(self, o): return Money(self.units - self._other(o))
    def __neg__(self): return Money(-self.units)
    def __abs__(self): return Money(abs(self.units))

    def __mul__(self, k):
        if isinstance(k, bool) or not isinstance(k, int):
            raise MoneyError("Money can only be multiplied by an int")
        return Money(self.units * k)
    __rmul__ = __mul__

    def __eq__(self, o):
        return isinstance(o, Money) and self.units == o.units

    def __lt__(self, o): return self.units < self._other(o)
    def __hash__(self): return hash(("Money", self.units))
    def __bool__(self): return self.units != 0

    # ---- output ----
    def ceil_to_cent(self) -> "Money":
        return Money(-((-self.units) // UNITS_PER_CENT) * UNITS_PER_CENT)

    def to_str(self) -> str:
        """Exact decimal string with 4-6 decimals, e.g. '0.9870', '-1.5000', '0.123456'."""
        s = "-" if self.units < 0 else ""
        u = abs(self.units)
        frac = f"{u % UNITS_PER_DOLLAR:06d}"
        frac = frac[:4] + frac[4:].rstrip("0")
        return f"{s}{u // UNITS_PER_DOLLAR}.{frac}"

    def __str__(self):
        s = "-" if self.units < 0 else ""
        u = abs(self.units)
        cents_part = u % UNITS_PER_DOLLAR
        if cents_part % UNITS_PER_CENT == 0:
            return f"{s}${u // UNITS_PER_DOLLAR:,}.{cents_part // UNITS_PER_CENT:02d}"
        return f"{s}${u // UNITS_PER_DOLLAR:,}.{f'{cents_part:06d}'.rstrip('0')}"

    def __repr__(self):
        return f"Money({self.to_str()})"


Money.ZERO = Money(0)


def min_money(*xs: Money) -> Money:
    return min(xs, key=lambda m: m.units)


# ---------------------------------------------------------------------------
# Kalshi fees
# ---------------------------------------------------------------------------
# Kalshi fee rounding (docs: getting_started/fee_rounding). For EACH FILL Kalshi charges
#   trade fee    = model fee rounded up to $0.000001
#   rounding fee = whatever brings the balance back to its grid ($0.0001 direct, $0.01 non-direct)
#   rebate       = accumulated rounding returned in whole grid steps, capped so a fill's net fee >= 0
# The per-order accumulator makes the total converge toward one fill's cost, but because the rebate
# is capped, an order filled in many small pieces can pay up to just under $0.01 of rounding PER FILL.
KALSHI_ROUNDING_ALLOWANCE = Money(10_000)          # $0.01: the most rounding one fill can add


def kalshi_worst_case_fee(contracts: int, price: Money, multiplier_pct: int = 100) -> Money:
    """PROVABLE upper bound of everything Kalshi can charge for one order of `contracts`, however
    it is split into fills: cent-rounded model fee + $0.01 per possible fill (at most one per contract).

    Proof: fill i pays trade_i + rounding_i - rebate_i <= (m_i + $0.000001) + $0.009999 = m_i + $0.01,
    where m_i is that fill's exact model fee. Summing over n <= C fills: total <= sum(m_i) + C x $0.01
    <= ceil_cent(model fee for C) + C x $0.01, because the model fee is linear in the contract count.
    """
    return kalshi_taker_fee(contracts, price, multiplier_pct) + KALSHI_ROUNDING_ALLOWANCE * contracts


class KalshiFeeAccumulator:
    """Exact per-fill fee mechanics from Kalshi's fee_rounding docs, for ONE order (used by the
    simulator). `grid` is the account's balance precision: $0.01 (non-direct members, the more
    expensive case and the default here) or $0.0001 (direct members)."""

    def __init__(self, grid: Money = Money(10_000)):
        self.grid = grid.units
        self.carry = 0                     # accumulated rounding overpayment, in units

    def fill(self, contracts: int, price: Money, rate_bp: int, multiplier_pct: int = 100) -> Money:
        """Net fee for a BUY fill. rate_bp: 700 for the 0.07 taker rate, 175 for an assumed 0.0175 maker rate."""
        p = price.units
        if contracts <= 0 or not 0 <= p <= UNITS_PER_DOLLAR:
            raise MoneyError("bad fill")
        # model fee in units = rate * C * P * (1 - P) * mult; P = p / U
        num = rate_bp * contracts * p * (UNITS_PER_DOLLAR - p) * multiplier_pct
        trade = -(-num // (10_000 * 100 * UNITS_PER_DOLLAR))           # ceil to $0.000001
        change = -(contracts * p) - trade                                # buyer: revenue is negative
        aligned = (change // self.grid) * self.grid                      # floor to the balance grid
        rounding = change - aligned
        self.carry += rounding
        rebate = min(self.carry // self.grid, (trade + rounding) // self.grid) * self.grid
        self.carry -= rebate
        return Money(trade + rounding - rebate)


def kalshi_taker_fee(contracts: int, price: Money, multiplier_pct: int = 100) -> Money:
    """Kalshi 'quadratic' taker fee for ONE order, exactly as charged:

        fee = ceil_to_cent( 0.07 * C * P * (1 - P) * multiplier )

    P is the price in dollars (0..1). Computed with integers only:
    with p = price in units (P = p / U, U = units per dollar), the fee in
    cents is 7 * C * p * (U - p) * mult / (100 * U^2).
    """
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 0:
        raise MoneyError("contracts must be a non-negative int")
    p = price.units
    if not 0 <= p <= UNITS_PER_DOLLAR:
        raise MoneyError("Kalshi price must be between $0 and $1")
    # fee in cents = 7/100 * C * (p/1e4) * (1 - p/1e4) * (mult/100) * 100
    numerator = 7 * contracts * p * (UNITS_PER_DOLLAR - p) * multiplier_pct
    denominator = 100 * UNITS_PER_DOLLAR * UNITS_PER_DOLLAR
    cents = -(-numerator // denominator)  # ceiling division
    return Money.cents(cents)
