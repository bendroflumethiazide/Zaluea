"""Portfolio correlation and concentration.

Different tickers are not necessarily different risks. This module puts
every position into a RISK GROUP, and the gateway limits the combined
exposure of each group (Limits.max_correlated_exposure).

How groups are formed, most specific evidence first:
  * Prediction markets on crypto prices: all crypto underlyings (BTC, ETH,
    SOL, ...) form ONE group. They usually move together, and treating
    them as separate would understate risk.
  * Other prediction markets and esports: grouped by underlying event
    (all markets on one match or one event are one group).
  * Stocks and ETFs: grouped by measured rolling correlation of daily
    returns (|rho| >= threshold) and by known ETF holdings overlap.
    With NO adequate data for a symbol, it joins the broad "us_equity"
    group, which is the conservative assumption the spec requires.

Correlation is not causation, and past correlations can break down in
unusual markets: the threshold is deliberately low (0.6) and unknowns are
always grouped, never assumed independent.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..money import Money

CORR_THRESHOLD = 0.6
MIN_OBSERVATIONS = 40               # daily returns needed before a correlation is trusted
CRYPTO = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BCH", "LTC", "ADA", "AVAX", "HYPE")
_CRYPTO_SERIES = re.compile(r"^KX(?:" + "|".join(CRYPTO) + r")")

# GICS sectors of widely held large caps (stable public classifications). Anything not
# listed is "Unknown" and is grouped conservatively, never assumed independent.
SECTORS = {
    "AAPL": ("Information Technology", "Technology Hardware"), "MSFT": ("Information Technology", "Software"),
    "NVDA": ("Information Technology", "Semiconductors"), "AVGO": ("Information Technology", "Semiconductors"),
    "AMD": ("Information Technology", "Semiconductors"), "ORCL": ("Information Technology", "Software"),
    "GOOGL": ("Communication Services", "Interactive Media"), "META": ("Communication Services", "Interactive Media"),
    "NFLX": ("Communication Services", "Entertainment"), "AMZN": ("Consumer Discretionary", "Broadline Retail"),
    "TSLA": ("Consumer Discretionary", "Automobiles"), "JPM": ("Financials", "Banks"),
    "BAC": ("Financials", "Banks"), "V": ("Financials", "Payments"), "XOM": ("Energy", "Oil & Gas"),
    "CVX": ("Energy", "Oil & Gas"), "JNJ": ("Health Care", "Pharmaceuticals"), "LLY": ("Health Care", "Pharmaceuticals"),
    "UNH": ("Health Care", "Managed Health Care"), "WMT": ("Consumer Staples", "Consumer Staples Retail"),
    "PG": ("Consumer Staples", "Household Products"), "KO": ("Consumer Staples", "Beverages"),
}
BROAD_ETFS = {"SPY", "VOO", "IVV", "VTI", "QQQ", "DIA", "IWM"}


@dataclass
class Holding:
    module: str
    market: str            # ticker or symbol
    event_group: str
    exposure: Money


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def rolling_correlation(a_closes: list[float], b_closes: list[float], window: int = 60) -> float | None:
    """Correlation of the last `window` daily returns. None if there isn't enough data."""
    ra, rb = returns(a_closes)[-window:], returns(b_closes)[-window:]
    n = min(len(ra), len(rb))
    if n < MIN_OBSERVATIONS:
        return None
    return pearson(ra[-n:], rb[-n:])


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if rb == "us_equity" or (ra != "us_equity" and rb < ra):   # the broad-market group keeps its name
            ra, rb = rb, ra
        self.p[rb] = ra


@dataclass
class CorrelationModel:
    """Equity price histories and ETF holdings are optional inputs; absent data => conservative grouping."""
    closes: dict[str, list[float]] = field(default_factory=dict)        # symbol -> daily closes, oldest first
    etf_holdings: dict[str, dict[str, float]] = field(default_factory=dict)   # etf -> {symbol: weight %}
    holdings_as_of: str | None = None
    window: int = 60

    def _equity_groups(self, symbols: set[str]) -> dict[str, str]:
        uf = _UF()
        known = []
        for s in symbols:
            if s in self.closes and len(returns(self.closes[s])) >= MIN_OBSERVATIONS:
                known.append(s)
            else:
                uf.union(s, "us_equity")                    # unknown => conservative
            if s in BROAD_ETFS:
                uf.union(s, "us_equity")                    # broad index funds ARE the market
        for a in known:
            for b in known:
                if a < b:
                    rho = rolling_correlation(self.closes[a], self.closes[b], self.window)
                    if rho is None or abs(rho) >= CORR_THRESHOLD:
                        uf.union(a, b)
        for etf, weights in self.etf_holdings.items():
            for sym, w in weights.items():
                if etf in symbols and sym in symbols and w >= 1.0:
                    uf.union(etf, sym)                      # an ETF that holds a stock overlaps it
        return {s: "equity:" + uf.find(s) for s in symbols}

    def group_of(self, h: Holding, all_symbols: set[str]) -> str:
        if h.module in ("kalshi", "esports"):
            series = h.market.split("-")[0]
            if h.module == "kalshi" and _CRYPTO_SERIES.match(series):
                return "crypto"
            return f"event:{h.event_group}"
        if h.module in ("stocks", "etfs"):
            return self._equity_groups(all_symbols | {h.market})[h.market]
        return f"unknown:{h.market}"

    def groups(self, holdings: list[Holding]) -> dict[str, list[Holding]]:
        symbols = {h.market for h in holdings if h.module in ("stocks", "etfs")}
        eq = self._equity_groups(symbols) if symbols else {}
        out = defaultdict(list)
        for h in holdings:
            key = eq[h.market] if h.module in ("stocks", "etfs") else self.group_of(h, symbols)
            out[key].append(h)
        return dict(out)

    def check(self, new: Holding, existing: list[Holding], limit: Money) -> tuple[bool, str, str]:
        """Would adding `new` push its risk group over the limit? -> (ok, group, reason)."""
        groups = self.groups(existing + [new])
        key = next(k for k, hs in groups.items() if new in hs)
        total = sum((h.exposure for h in groups[key]), Money.ZERO)
        members = sorted({h.market for h in groups[key]})
        if total > limit:
            return False, key, (f"Correlated group {key} would hold {total} (limit {limit}) across "
                                f"{', '.join(members)}")
        return True, key, f"Correlated group {key}: {total} of {limit}"

    def sector_exposure(self, holdings: list[Holding]) -> dict[str, Money]:
        out = defaultdict(lambda: Money.ZERO)
        for h in holdings:
            if h.module in ("stocks", "etfs"):
                sector = "Broad market" if h.market in BROAD_ETFS else SECTORS.get(h.market, ("Unknown", ""))[0]
            elif h.module == "kalshi" and _CRYPTO_SERIES.match(h.market.split("-")[0]):
                sector = "Crypto events"
            else:
                sector = "Esports events" if h.module == "esports" else "Other prediction markets"
            out[sector] += h.exposure
        return dict(out)

    def etf_overlap(self, a: str, b: str) -> float | None:
        """Share (%) of ETF a's weight in holdings it shares with b (an ETF or a single stock)."""
        wa = self.etf_holdings.get(a)
        if wa is None:
            return None
        if b in self.etf_holdings:
            wb = self.etf_holdings[b]
            return sum(min(w, wb.get(s, 0.0)) for s, w in wa.items())
        return wa.get(b, 0.0)
