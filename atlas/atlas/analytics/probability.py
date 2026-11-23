"""Probability and expected-value engine.

Two separate questions, kept separate on purpose (spec Part 5):
  1. Does a model PREDICT well?   -> Brier score, log loss, calibration,
     measured on resolved predictions in the prediction log.
  2. Does it make PROFITABLE, EXECUTABLE trades? -> expected value after
     fees, spread and slippage, in exact Money (and later, realized P&L).

Probabilities are stored as integer basis points (0-10000). The scoring
statistics below use floats: they describe model quality and never decide
whether money is spent. Expected value, which DOES feed trading decisions,
is exact integer Money, rounded DOWN so an edge is never overstated.

A model's probabilities count as VALIDATED only after MIN_VALIDATION
resolved predictions; until then everything it produces is labelled
"unvalidated", never presented as a statistically supported probability.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..money import UNITS_PER_DOLLAR, Money

BP = 10_000
MIN_VALIDATION = 300

# --------------------------------------------------------------------------- expected value


def expected_value(prob_bp: int, price: Money, qty: int, fee: Money, slippage: Money = Money.ZERO) -> Money:
    """EV of buying `qty` binary contracts that pay $1 each if they win.

    EV = P(win) * qty * $1 - (qty * price + fee + slippage), exact, rounded down.
    """
    if not 0 <= prob_bp <= BP:
        raise ValueError("probability must be 0..10000 bp")
    if qty <= 0:
        raise ValueError("qty must be positive")
    win_units = prob_bp * qty * UNITS_PER_DOLLAR // BP          # floor: never overstate
    cost = price * qty + fee + slippage
    return Money(win_units - cost.units)


def breakeven_bp(price: Money, qty: int, fee: Money, slippage: Money = Money.ZERO) -> int:
    """Smallest probability (bp, rounded UP) at which the trade has EV >= 0."""
    cost = (price * qty + fee + slippage).units
    return -(-cost * BP // (qty * UNITS_PER_DOLLAR))


# --------------------------------------------------------------------------- scoring


def brier(pairs: list[tuple[int, int]]) -> float:
    """pairs: (prob_bp, outcome 0/1). Lower is better; 0.25 is a coin flip."""
    return sum((p / BP - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(pairs: list[tuple[int, int]], eps: float = 1e-4) -> float:
    total = 0.0
    for p, y in pairs:
        q = min(max(p / BP, eps), 1 - eps)
        total += -(y * math.log(q) + (1 - y) * math.log(1 - q))
    return total / len(pairs)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a frequency (supportable with small n)."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Bucket:
    lo_bp: int
    hi_bp: int
    n: int
    mean_pred: float
    observed: float
    ci_low: float
    ci_high: float

    @property
    def consistent(self) -> bool:
        """Is the mean prediction inside the 95% interval of what happened?"""
        return self.ci_low <= self.mean_pred <= self.ci_high


def calibration(pairs: list[tuple[int, int]], edges_bp=(0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000,
                                                         9000, 10001)) -> list[Bucket]:
    out = []
    for lo, hi in zip(edges_bp, edges_bp[1:]):
        sel = [(p, y) for p, y in pairs if lo <= p < hi]
        if sel:
            wins = sum(y for _, y in sel)
            lo_ci, hi_ci = wilson(wins, len(sel))
            out.append(Bucket(lo, min(hi, BP), len(sel), sum(p for p, _ in sel) / len(sel) / BP,
                              wins / len(sel), lo_ci, hi_ci))
    return out


# --------------------------------------------------------------------------- prediction log

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, strategy TEXT NOT NULL, model_version TEXT NOT NULL,
    market TEXT NOT NULL, market_type TEXT NOT NULL, side TEXT NOT NULL,
    prob_bp INTEGER NOT NULL CHECK (prob_bp BETWEEN 0 AND 10000),
    market_prob_bp INTEGER CHECK (market_prob_bp BETWEEN 0 AND 10000),
    ci_low_bp INTEGER, ci_high_bp INTEGER, sources TEXT NOT NULL,
    outcome INTEGER CHECK (outcome IN (0, 1)), resolved_ts REAL);
CREATE INDEX IF NOT EXISTS predictions_market ON predictions(market);
"""


@dataclass
class Scorecard:
    n: int
    brier: float | None
    log_loss: float | None
    market_brier: float | None
    buckets: list[Bucket]
    validated: bool

    def summary(self) -> str:
        if not self.n:
            return "No resolved predictions yet (unvalidated)"
        s = f"{self.n} resolved, Brier {self.brier:.4f}, log loss {self.log_loss:.4f}"
        if self.market_brier is not None:
            s += f" (market's own price: Brier {self.market_brier:.4f})"
        return s + ("" if self.validated else f"; UNVALIDATED until {MIN_VALIDATION} resolved")


class PredictionLog:
    def __init__(self, path: str | Path, clock=time.time):
        self.con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.clock = clock

    def record(self, *, strategy: str, model_version: str, market: str, market_type: str, side: str,
               prob_bp: int, market_prob_bp: int | None, sources: list[str],
               ci_bp: tuple[int, int] | None = None, ts: float | None = None) -> str:
        if not sources:
            raise ValueError("every prediction must name its data sources")
        pid = f"pred-{uuid.uuid4()}"
        self.con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                         (pid, ts if ts is not None else self.clock(), strategy, model_version, market,
                          market_type, side, int(prob_bp), market_prob_bp,
                          ci_bp[0] if ci_bp else None, ci_bp[1] if ci_bp else None, json.dumps(sources)))
        return pid

    def resolve(self, market: str, winning_side: str) -> int:
        """Score every open prediction on this market. Returns how many were resolved."""
        n = 0
        for r in self.con.execute("SELECT id, side FROM predictions WHERE market=? AND outcome IS NULL", (market,)):
            self.con.execute("UPDATE predictions SET outcome=?, resolved_ts=? WHERE id=?",
                             (1 if r["side"] == winning_side else 0, self.clock(), r["id"]))
            n += 1
        return n

    def scorecard(self, strategy: str | None = None, model_version: str | None = None,
                  market_type: str | None = None) -> Scorecard:
        q, args = "SELECT prob_bp, market_prob_bp, outcome FROM predictions WHERE outcome IS NOT NULL", []
        for col, val in (("strategy", strategy), ("model_version", model_version), ("market_type", market_type)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        rows = self.con.execute(q, args).fetchall()
        pairs = [(r["prob_bp"], r["outcome"]) for r in rows]
        mpairs = [(r["market_prob_bp"], r["outcome"]) for r in rows if r["market_prob_bp"] is not None]
        if not pairs:
            return Scorecard(0, None, None, None, [], False)
        return Scorecard(len(pairs), brier(pairs), log_loss(pairs), brier(mpairs) if mpairs else None,
                         calibration(pairs), len(pairs) >= MIN_VALIDATION)
