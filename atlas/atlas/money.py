"""Exact money for ATLAS.

All amounts are integers of UNITS, where 1 unit = $0.0001. Kalshi quotes
prices to four decimals and US stocks under $1 trade in sub-pennies, so a
tenth of a cent is not fine enough and a float is never acceptable.

`Money` refuses floats at every entry point. Authorization decisions in
ATLAS must only ever compare `Money` (or plain ints) - never floats.
"""
from __future__ import annotations

import re
from functools import total_ordering

UNITS_PER_DOLLAR = 10_000
UNITS_PER_CENT = 100
_DEC = re.compile(r"^\s*(-)?\$?(\d+)(?:\.(\d{1,4}))?\s*$")


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
        """'1.25', '$0.9870', '-3' -> Money. At most 4 decimals; no floats."""
        if not isinstance(text, str):
            raise MoneyError(f"Money.parse needs a string, got {type(text).__name__}")
        m = _DEC.match(text)
        if not m:
            raise MoneyError(f"Not a money amount: {text!r}")
        sign, whole, frac = m.groups()
        units = int(whole) * UNITS_PER_DOLLAR + int((frac or "").ljust(4, "0") or 0)
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
        """Exact decimal string, e.g. '0.9870' or '-1.5000'."""
        s = "-" if self.units < 0 else ""
        u = abs(self.units)
        return f"{s}{u // UNITS_PER_DOLLAR}.{u % UNITS_PER_DOLLAR:04d}"

    def __str__(self):
        s = "-" if self.units < 0 else ""
        u = abs(self.units)
        cents_part = u % UNITS_PER_DOLLAR
        if cents_part % UNITS_PER_CENT == 0:
            return f"{s}${u // UNITS_PER_DOLLAR:,}.{cents_part // UNITS_PER_CENT:02d}"
        return f"{s}${u // UNITS_PER_DOLLAR:,}.{cents_part:04d}"

    def __repr__(self):
        return f"Money({self.to_str()})"


Money.ZERO = Money(0)


def min_money(*xs: Money) -> Money:
    return min(xs, key=lambda m: m.units)


# ---------------------------------------------------------------------------
# Kalshi fees
# ---------------------------------------------------------------------------
def kalshi_taker_fee(contracts: int, price: Money, multiplier_pct: int = 100) -> Money:
    """Kalshi 'quadratic' taker fee for ONE order, exactly as charged:

        fee = ceil_to_cent( 0.07 * C * P * (1 - P) * multiplier )

    P is the price in dollars (0..1). Computed with integers only:
    with p = price in units (P = p / 10_000), the fee in dollars is
    7 * C * p * (10_000 - p) * mult / (100 * 10_000^2 * 100).
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
