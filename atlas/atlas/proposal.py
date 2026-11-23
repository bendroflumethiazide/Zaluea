"""What a strategy may hand to the gateway: a PROPOSAL, never an order.

Strategies have no exchange access. They describe what they would like to
do and why; the gateway, both risk managers and (when enabled) the owner
decide whether it happens.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .money import Money


@dataclass(frozen=True)
class Quote:
    """The market data a decision was based on."""
    market: str
    side: str
    bid: Money | None
    ask: Money | None
    ask_size: int
    ts: float                      # when the quote was observed (unix seconds)
    source: str


@dataclass(frozen=True)
class Proposal:
    module: str                    # kalshi | esports | stocks | etfs
    market: str
    event_group: str               # underlying event / risk group, for correlation limits
    strategy: str
    model_version: str
    side: str                      # yes | no (Kalshi) or long (stocks/ETFs)
    qty: int
    limit_price: Money             # highest price ATLAS will pay
    quote: Quote
    prob_bp: int                   # estimated probability in basis points (0-10000)
    expected_value: Money          # net of fees, spread and slippage, for the whole order
    reason: str
    exit_plan: str
    fee_multiplier_pct: int = 100  # the market's fee multiplier x 100 (Kalshi series setting)
    proposal_id: str = field(default_factory=lambda: f"prop-{uuid.uuid4()}")
    action: str = "buy"
