"""Primary risk manager.

A pure function of a RiskContext that the gateway assembles from its own
sources (ledger, limits, controls, locks, reconciliation, live quote). It
runs EVERY check (not just until the first failure) so each decision is
fully explained, and approves only if all pass. The most restrictive limit
always wins because every limit is an independent hard check.

All money comparisons use integer Money units.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..limits import MODULE_CATEGORY, Limits
from ..ledger import Snapshot
from ..money import Money, kalshi_taker_fee
from ..proposal import Proposal

RECONCILE_MAX_AGE_S = 30.0
KALSHI_MAX_PRICE = Money.parse("0.99")


KNOWN_KALSHI_FEE_TYPES = {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}


def fee_multiplier_pct(fee_type: str | None, multiplier: str | None) -> int | None:
    """Exact fee multiplier in percent, or None if ATLAS cannot compute this
    market's fee exactly (unknown fee type, or a multiplier that is not a
    whole percent). None means: do not trade this market."""
    if fee_type not in KNOWN_KALSHI_FEE_TYPES or multiplier in (None, "", "None"):
        return None
    try:
        m = Money.parse(str(multiplier))           # exact decimal parse; floats never involved
    except Exception:
        return None
    if m.units < 0 or m.units % 10_000:            # must be a whole percent
        return None
    return m.units // 10_000


def worst_case_fee(module: str, qty: int, price: Money, multiplier_pct: int = 100) -> Money:
    """Highest taker fee this order could incur."""
    if module in ("kalshi", "esports"):
        return kalshi_taker_fee(qty, price, multiplier_pct)
    if module in ("stocks", "etfs"):
        return Money.ZERO            # Alpaca charges no commission on buys
    raise ValueError(f"Unknown module {module}")


def max_cost(p: Proposal) -> Money:
    return p.limit_price * p.qty + worst_case_fee(p.module, p.qty, p.limit_price, p.fee_multiplier_pct)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Decision:
    approved: bool
    checks: list[Check]
    max_cost: Money
    manager: str = "primary"

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok]


@dataclass
class RiskContext:
    now: float
    verified_day: str | None
    limits: Limits
    snapshot: Snapshot
    authorized: bool
    module_enabled: bool
    locked: bool
    lock_reason: str
    reconciled_ok: bool
    reconciled_at: float | None
    cash: Money
    unrealized: Money
    extra: dict = field(default_factory=dict)


def _spread_ok(p: Proposal, L: Limits) -> tuple[bool, str]:
    q = p.quote
    if q.bid is None or q.ask is None:
        return False, "No two-sided quote"
    spread = q.ask - q.bid
    if spread.units < 0:
        return False, "Crossed quote (bid above ask): data is unreliable"
    if p.module in ("kalshi", "esports"):
        return spread <= L.kalshi_max_spread, f"Spread {spread} (max {L.kalshi_max_spread})"
    # stocks/ETFs: spread / mid <= bps   <=>   spread * 10000 * 2 <= bps * (ask + bid)
    ok = spread.units * 20_000 <= L.stock_max_spread_bps * (q.ask.units + q.bid.units)
    return ok, f"Spread {spread} vs {L.stock_max_spread_bps} bps limit"


def evaluate(p: Proposal, ctx: RiskContext) -> Decision:
    L, s = ctx.limits, ctx.snapshot
    cost = max_cost(p)
    cat = MODULE_CATEGORY.get(p.module)
    q = p.quote
    checks: list[Check] = []
    add = lambda name, ok, detail: checks.append(Check(name, bool(ok), detail))

    add("entry_only", p.action == "buy", "Only entries go through this path")
    add("module", ctx.module_enabled and cat is not None, f"{p.module} module {'enabled' if ctx.module_enabled else 'disabled'}")
    add("clock", ctx.verified_day is not None, "Clock verified" if ctx.verified_day else "Clock not verified")
    add("day", ctx.verified_day == s.day, f"Ledger day {s.day} vs verified day {ctx.verified_day}")
    add("authorization", ctx.authorized, "Today's budget authorized" if ctx.authorized else "Today's budget is not authorized")
    add("locks", not ctx.locked, ctx.lock_reason or "No safety locks")
    fresh = ctx.reconciled_ok and ctx.reconciled_at is not None and ctx.now - ctx.reconciled_at <= RECONCILE_MAX_AGE_S
    add("reconciliation", fresh, "Reconciled recently" if fresh else "Account not reconciled recently")

    age_ms = (ctx.now - q.ts) * 1000
    add("data_age", 0 <= age_ms <= L.max_data_age_ms, f"Quote is {age_ms:.0f} ms old (max {L.max_data_age_ms})")
    add("quote_identity", q.market == p.market and q.side == p.side, "Quote matches the proposed market and side")
    ok, detail = _spread_ok(p, L)
    add("spread", ok, detail)
    add("liquidity", q.ask_size >= p.qty, f"{q.ask_size} available at the ask for {p.qty}")
    if q.ask is not None:
        slip = Money(-(-q.ask.units * L.max_slippage_bps // 10_000))
        add("limit_vs_ask", p.limit_price >= q.ask, f"Limit {p.limit_price} vs ask {q.ask}")
        add("slippage", p.limit_price <= q.ask + slip, f"Limit {p.limit_price} vs ask {q.ask} + {L.max_slippage_bps} bps")
    if p.module in ("kalshi", "esports"):
        add("price_range", Money(1) <= p.limit_price <= KALSHI_MAX_PRICE, f"Kalshi price {p.limit_price}")
    add("quantity", p.qty > 0, f"Quantity {p.qty}")
    add("probability", 0 < p.prob_bp < 10_000, f"Probability {p.prob_bp} bp")
    add("expected_value", p.expected_value.units > 0, f"Expected value {p.expected_value} after costs")

    add("max_order", cost <= L.max_order, f"Cost {cost} (max {L.max_order})")
    add("daily_spend", s.spent + cost <= L.daily_spend, f"{s.spent} spent + {cost} (max {L.daily_spend})")
    if cat:
        used = s.spent_by_category.get(cat, Money.ZERO)
        add("category", used + cost <= L.category_limit(p.module), f"{cat}: {used} + {cost} (max {L.category_limit(p.module)})")
    add("daily_loss", -s.realized_today < L.max_daily_loss, f"Realized today {s.realized_today} (limit -{L.max_daily_loss})")
    add("drawdown", -(s.realized_today + ctx.unrealized) < L.max_drawdown,
        f"Realized + unrealized {s.realized_today + ctx.unrealized} (limit -{L.max_drawdown})")
    add("consecutive_losses", s.consecutive_losses < L.max_consec_losses,
        f"{s.consecutive_losses} losses in a row (limit {L.max_consec_losses})")
    strat_streak = s.consecutive_losses_by_strategy.get(p.strategy, 0)
    add("strategy_losses", strat_streak < L.max_consec_losses, f"{p.strategy}: {strat_streak} losses in a row")
    add("market_exposure", s.exposure_by_market.get(p.market, Money.ZERO) + cost <= L.max_market_exposure,
        f"Market exposure would be {s.exposure_by_market.get(p.market, Money.ZERO) + cost} (max {L.max_market_exposure})")
    add("total_exposure", s.exposure_total + cost <= L.max_total_exposure,
        f"Total exposure would be {s.exposure_total + cost} (max {L.max_total_exposure})")
    add("event_exposure", s.exposure_by_event.get(p.event_group, Money.ZERO) + cost <= L.max_event_exposure,
        f"Event {p.event_group} exposure would be {s.exposure_by_event.get(p.event_group, Money.ZERO) + cost} (max {L.max_event_exposure})")
    add("strategy_exposure", s.exposure_by_strategy.get(p.strategy, Money.ZERO) + cost <= L.max_strategy_exposure,
        f"Strategy exposure (max {L.max_strategy_exposure})")
    held = {(x.module, x.market, x.side) for x in s.positions}
    new_pos = (p.module, p.market, p.side) not in held
    n_pos = len(held) + s.open_entry_orders + (1 if new_pos else 0)   # pending orders count as positions
    add("positions", n_pos <= L.max_positions,
        f"{len(held)} held + {s.open_entry_orders} pending + {1 if new_pos else 0} new (max {L.max_positions})")
    add("open_orders", s.open_entry_orders < L.max_open_orders, f"{s.open_entry_orders} open entry orders (max {L.max_open_orders})")
    add("cash", ctx.cash >= cost + s.reserved, f"Cash {ctx.cash} vs {cost} + {s.reserved} reserved")

    return Decision(all(c.ok for c in checks), checks, cost)
