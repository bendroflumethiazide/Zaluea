"""Strategy framework.

A strategy turns a point-in-time view of the market into either a Signal
(a candidate trade with its probability, price, size and reasoning) or a
NoTrade with the reasons. It has no exchange access: the engine converts a
Signal into a Proposal, and only the gateway can act on that.

Look-ahead protection: strategies only ever see `Series` objects cut at the
decision time. Asking a cut series for anything later raises LookAheadError,
so a backtest cannot accidentally use the future.
"""
from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Generic, TypeVar

from ..money import Money

T = TypeVar("T")


class LookAheadError(RuntimeError):
    pass


class Series(Generic[T]):
    """Time-ordered values (ts, value). `cut(t)` returns a view that refuses anything after t."""

    def __init__(self, points: list[tuple[float, T]], cutoff: float | None = None):
        self._ts = [p[0] for p in points]
        self._v = [p[1] for p in points]
        self.cutoff = cutoff

    @classmethod
    def from_sorted(cls, points, cutoff=None):
        return cls(points, cutoff)

    def cut(self, t: float) -> "Series[T]":
        s = Series.__new__(Series)
        s._ts, s._v, s.cutoff = self._ts, self._v, t if self.cutoff is None else min(t, self.cutoff)
        return s

    def _guard(self, t: float):
        if self.cutoff is not None and t > self.cutoff:
            raise LookAheadError(f"asked for data at {t}, after the decision time {self.cutoff}")

    def at(self, t: float) -> tuple[float, T] | None:
        """Latest point at or before t."""
        self._guard(t)
        i = bisect.bisect_right(self._ts, t) - 1
        return (self._ts[i], self._v[i]) if i >= 0 else None

    def window(self, start: float, end: float) -> list[tuple[float, T]]:
        """Points with start < ts <= end."""
        self._guard(end)
        i, j = bisect.bisect_right(self._ts, start), bisect.bisect_right(self._ts, end)
        return list(zip(self._ts[i:j], self._v[i:j]))


@dataclass(frozen=True)
class MarketView:
    """Everything a crypto-window strategy may know at decision time `now`."""
    now: float
    market: str
    event: str
    module: str
    open_ts: float
    close_ts: float
    strike: Money | None                 # Kalshi's floor_strike for "up" markets
    fee_multiplier_pct: int
    quotes: dict                         # side -> Series[(bid Money|None, ask Money|None, ask_size int|None)]
    reference: Series                    # Series[Money]: BTC reference price, one point per observation
    max_order: Money = Money.dollars(2)


@dataclass
class Signal:
    strategy: str
    model_version: str
    market: str
    side: str
    qty: int
    limit_price: Money
    prob_bp: int
    market_prob_bp: int
    expected_value: Money
    fee: Money
    reasons: list[str]
    sources: list[str]
    exit_plan: str


@dataclass
class NoTrade:
    strategy: str
    market: str
    reasons: list[str]
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyConfig:
    def version(self, name: str) -> str:
        """A config change is a new model version (traceable, comparable)."""
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return f"{name}@{hashlib.sha256(payload.encode()).hexdigest()[:10]}"


class Strategy:
    name = "base"
    module = "kalshi"

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.version = config.version(self.name)

    def evaluate(self, view: MarketView) -> Signal | NoTrade:
        raise NotImplementedError
