"""BTC reference price from several exchanges.

Kalshi's BTC markets settle on CF Benchmarks' BRTI, an index built from
several exchanges' order books. A single exchange (the old bot used only
Coinbase) differed from the settlement index by up to ~$24. This module
takes the MEDIAN of independent public sources and refuses to answer when
the data is unreliable:

  * fewer than `min_sources` fresh prices        -> no price
  * sources disagree by more than `max_dispersion_bps` -> no price
  * a source's own timestamp is too old          -> that source is ignored

Every point keeps its source and timestamps. Prices are parsed from strings
into exact Money; floats are never used.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..money import Money

USER_AGENT = "atlas-research/1.0"


@dataclass(frozen=True)
class PricePoint:
    source: str
    price: Money
    source_ts: float | None        # the source's own timestamp, when it provides one
    received_ts: float


@dataclass
class Reference:
    ok: bool
    price: Money | None
    points: list[PricePoint] = field(default_factory=list)
    reason: str = ""
    ts: float = 0.0


def _get_json(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _price(s) -> Money:
    """Exchange prices as strings -> Money, trimming any digits past 6 decimals."""
    s = str(s)
    whole, _, frac = s.partition(".")
    return Money.parse(f"{whole}.{frac[:6]}" if frac else whole)


def coinbase(clock=time.time) -> PricePoint:
    d = _get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    return PricePoint("coinbase", _price(d["price"]), _iso(d["time"]), clock())


def kraken(clock=time.time) -> PricePoint:
    d = _get_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
    if d.get("error"):
        raise ValueError(f"kraken: {d['error']}")
    (row,) = d["result"].values()
    return PricePoint("kraken", _price(row["c"][0]), None, clock())   # Kraken gives no tick timestamp


def bitstamp(clock=time.time) -> PricePoint:
    d = _get_json("https://www.bitstamp.net/api/v2/ticker/btcusd/")
    return PricePoint("bitstamp", _price(d["last"]), float(int(d["timestamp"])), clock())


DEFAULT_SOURCES = (coinbase, kraken, bitstamp)


class BtcReference:
    def __init__(self, sources: tuple[Callable, ...] = DEFAULT_SOURCES, clock=time.time,
                 max_age_s: float = 10.0, max_dispersion_bps: int = 30, min_sources: int = 2):
        self.sources = sources
        self.clock = clock
        self.max_age_s = max_age_s
        self.max_dispersion_bps = max_dispersion_bps
        self.min_sources = min_sources

    def get(self) -> Reference:
        now = self.clock()
        fresh, notes = [], []
        for src in self.sources:
            name = getattr(src, "__name__", "source")
            try:
                p = src(self.clock)
            except Exception as e:
                notes.append(f"{name} unavailable ({type(e).__name__})")
                continue
            age = now - (p.source_ts if p.source_ts is not None else p.received_ts)
            if age > self.max_age_s or age < -self.max_age_s:
                notes.append(f"{p.source} stale ({age:.0f} s)")
                continue
            if p.price.units <= 0:
                notes.append(f"{p.source} returned a non-positive price")
                continue
            fresh.append(p)
        if len(fresh) < self.min_sources:
            return Reference(False, None, fresh, f"Only {len(fresh)} fresh source(s): " + "; ".join(notes), now)
        prices = sorted(p.price.units for p in fresh)
        n = len(prices)
        median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) // 2
        spread = prices[-1] - prices[0]
        if spread * 10_000 > self.max_dispersion_bps * median:
            return Reference(False, None, fresh, f"Sources disagree by {Money(spread)} "
                             f"(more than {self.max_dispersion_bps} bps)", now)
        return Reference(True, Money(median), fresh, "; ".join(notes) or "All sources agree", now)


def _get_json_exact(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(), parse_float=str)          # exact text, never floats


def recent_history(minutes: int = 90, max_dispersion_bps: int = 30) -> list[tuple[float, Money]]:
    """Last `minutes` of 1-minute BTC closes: mean of Coinbase and Bitstamp (= their median),
    stamped at the END of each minute. Minutes where they disagree, or one is missing, are left out."""
    now = int(time.time())
    start = now - minutes * 60
    cb = {}
    rows = _get_json_exact("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60&start="
                           + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)))
    for r in rows:
        cb[int(r[0])] = _price(r[4])
    bs = {}
    d = _get_json_exact(f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=60&limit={min(minutes, 1000)}&start={start}")
    for r in (d.get("data") or {}).get("ohlc") or []:
        bs[int(r["timestamp"])] = _price(r["close"])
    out = []
    for t in sorted(set(cb) & set(bs)):
        a, b = cb[t], bs[t]
        if abs(a.units - b.units) * 10_000 > max_dispersion_bps * min(a.units, b.units):
            continue
        if t + 60 <= now:                                      # only completed minutes
            out.append((float(t + 60), Money((a.units + b.units) // 2)))
    return out
