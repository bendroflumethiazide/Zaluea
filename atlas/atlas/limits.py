"""Financial limits: defaults, absolute ceilings, and the change rules.

Rules (confirmed with the owner):
  * Lowering a limit takes effect immediately.
  * Raising a limit needs the typed confirmation "RAISE LIMITS" and takes
    effect at the start of the NEXT accounting day.
  * Nothing can be raised above ABSOLUTE_CEILINGS without a code change.
  * Only the owner's control path calls `LimitStore.change()`. Strategies,
    optimizers and other automated components get read-only `Limits`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, fields, replace
from pathlib import Path

from .audit import AuditLog
from .money import Money
from .storage import StorageError, connect, immediate

RAISE_CONFIRMATION = "RAISE LIMITS"
CATEGORIES = ("kalshi", "stocks", "etfs")
MODULE_CATEGORY = {"kalshi": "kalshi", "esports": "kalshi", "stocks": "stocks", "etfs": "etfs"}


@dataclass(frozen=True)
class Limits:
    # Spending and losses (Money)
    daily_spend: Money = Money.dollars(20)            # purchases + fees, all modules
    max_order: Money = Money.dollars(2)               # one purchase, including fees
    max_market_exposure: Money = Money.dollars(3)
    max_total_exposure: Money = Money.dollars(9)
    max_daily_loss: Money = Money.dollars(3)          # realized
    max_drawdown: Money = Money.dollars(5)            # realized + unrealized, emergency pause
    cat_kalshi: Money = Money.dollars(10)             # category ceilings inside daily_spend
    cat_stocks: Money = Money.dollars(10)
    cat_etfs: Money = Money.dollars(10)
    max_event_exposure: Money = Money.dollars(3)      # one underlying event / risk group
    max_strategy_exposure: Money = Money.dollars(6)
    # Counts
    max_positions: int = 3
    max_open_orders: int = 3
    max_consec_losses: int = 3
    # Market quality
    max_data_age_ms: int = 5_000
    max_slippage_bps: int = 200                       # vs. the quote the decision used
    kalshi_max_spread: Money = Money.cents(4)
    stock_max_spread_bps: int = 30

    def category_limit(self, module: str) -> Money:
        return getattr(self, "cat_" + MODULE_CATEGORY[module])


DEFAULTS = Limits()

ABSOLUTE_CEILINGS = Limits(
    daily_spend=Money.dollars(100), max_order=Money.dollars(25), max_market_exposure=Money.dollars(25),
    max_total_exposure=Money.dollars(100), max_daily_loss=Money.dollars(50), max_drawdown=Money.dollars(75),
    cat_kalshi=Money.dollars(100), cat_stocks=Money.dollars(100), cat_etfs=Money.dollars(100),
    max_event_exposure=Money.dollars(25), max_strategy_exposure=Money.dollars(50),
    max_positions=10, max_open_orders=10, max_consec_losses=10,
    max_data_age_ms=30_000, max_slippage_bps=500, kalshi_max_spread=Money.cents(10), stock_max_spread_bps=100,
)

# For these, a LARGER number is MORE risk. Everything in Limits is like that.
FIELD_NAMES = [f.name for f in fields(Limits)]


class LimitError(ValueError):
    pass


def _encode(v) -> str:
    return f"M:{v.units}" if isinstance(v, Money) else f"I:{v}"


def _decode(s: str):
    kind, val = s.split(":", 1)
    return Money(int(val)) if kind == "M" else int(val)


SCHEMA = """
CREATE TABLE IF NOT EXISTS limit_changes (
    id INTEGER PRIMARY KEY,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    effective_day TEXT NOT NULL,
    created_ts REAL NOT NULL,
    reason TEXT
);
"""


class LimitStore:
    def __init__(self, path: str | Path, audit: AuditLog | None, create: bool = False, readonly: bool = False):
        self.con = connect(path, must_exist=not create, readonly=readonly)
        if not readonly:
            self.con.executescript(SCHEMA)
        self.audit = audit
        if create and not self.con.execute("SELECT 1 FROM limit_changes LIMIT 1").fetchone():
            with immediate(self.con):
                for name in FIELD_NAMES:
                    self.con.execute("INSERT INTO limit_changes (field,value,effective_day,created_ts,reason) "
                                     "VALUES (?,?,?,?,?)", (name, _encode(getattr(DEFAULTS, name)),
                                                            "0000-00-00", time.time(), "default"))
            audit.append("config_change", decision="initialized", reason="Default limits installed",
                         data={n: _encode(getattr(DEFAULTS, n)) for n in FIELD_NAMES})

    def current(self, day: str) -> Limits:
        """Limits in force on accounting day `day`. Raises StorageError if any
        field is missing, so callers fail closed."""
        vals = {}
        for name in FIELD_NAMES:
            row = self.con.execute(
                "SELECT value FROM limit_changes WHERE field=? AND effective_day<=? ORDER BY id DESC LIMIT 1",
                (name, day)).fetchone()
            if row is None:
                raise StorageError(f"Limit {name} is missing")
            v = _decode(row["value"])
            ceiling = getattr(ABSOLUTE_CEILINGS, name)
            if (v.units if isinstance(v, Money) else v) > (ceiling.units if isinstance(ceiling, Money) else ceiling):
                raise StorageError(f"Stored limit {name} exceeds its absolute ceiling")
            vals[name] = v
        return Limits(**vals)

    def change(self, today: str, tomorrow: str, changes: dict, confirm: str = "",
               actor: str = "owner") -> dict:
        """Apply owner-requested changes. Returns {field: 'now'|'tomorrow'}."""
        cur = self.current(today)
        plan = {}
        for name, new in changes.items():
            if name not in FIELD_NAMES:
                raise LimitError(f"Unknown limit {name}")
            old = getattr(cur, name)
            if type(new) is not type(old) or isinstance(new, bool):
                raise LimitError(f"{name} must be {type(old).__name__}")
            n = new.units if isinstance(new, Money) else new
            o = old.units if isinstance(old, Money) else old
            ceiling = getattr(ABSOLUTE_CEILINGS, name)
            c = ceiling.units if isinstance(ceiling, Money) else ceiling
            if n <= 0:
                raise LimitError(f"{name} must be greater than zero")
            if n > c:
                raise LimitError(f"{name} cannot exceed its absolute ceiling of {ceiling}")
            if n == o:
                continue
            plan[name] = (new, "now" if n < o else "tomorrow")
        if any(when == "tomorrow" for _, when in plan.values()) and confirm != RAISE_CONFIRMATION:
            raise LimitError(f'Raising a limit needs the confirmation "{RAISE_CONFIRMATION}"')
        with immediate(self.con):
            for name, (new, when) in plan.items():
                self.con.execute("INSERT INTO limit_changes (field,value,effective_day,created_ts,reason) "
                                 "VALUES (?,?,?,?,?)", (name, _encode(new), today if when == "now" else tomorrow,
                                                        time.time(), f"{actor}: {'lower' if when == 'now' else 'raise'}"))
        if plan:
            self.audit.append("config_change", decision="limits_changed", reason=f"by {actor}",
                              data={n: {"value": _encode(v), "effective": w} for n, (v, w) in plan.items()})
        return {n: w for n, (_, w) in plan.items()}
