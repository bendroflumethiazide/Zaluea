"""Global spending ledger: the authoritative record of ATLAS's money.

Design
------
* One durable SQLite file (ledger.db). Opening it requires the file to exist
  and carry the ATLAS marker; a missing or foreign file is a StorageError and
  callers must block new orders (fail closed). Creating a ledger is an
  explicit, one-time `create=True` step.
* `orders` holds every order ATLAS reserved budget for, with its maximum
  potential cost (price x quantity + worst-case fee).
* `executions` is the single source of truth for what actually happened:
  buy fills, sell fills and settlements, in order. Positions, realized P&L,
  spending and fees are all *derived* from it, so they cannot drift apart.

Spending rules (ATLAS spec, Part 7)
-----------------------------------
For an accounting day D, spending =
    filled purchase cost + purchase fees of buy orders reserved on day D
  + the unfilled remainder of every buy order on day D that is not yet
    CONFIRMED finished (reserved, submitted, open, partially filled,
    cancel requested, or UNKNOWN after a timeout)
  + fees paid on sells executed on day D.
Selling, settling or winning NEVER reduces spending. A reservation is
released only by `finalize()` with an exchange-confirmed final state.

`reserve()` checks every hard limit and inserts the reservation inside one
`BEGIN IMMEDIATE` transaction, so simultaneous reservations from different
threads or processes can never jointly exceed a limit.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .audit import AuditLog
from .limits import MODULE_CATEGORY, Limits
from .money import Money
from .storage import StorageError, connect, immediate

MARKER = "atlas-ledger-v1"
FINAL = {"filled", "cancelled", "rejected", "expired"}
OPEN_STATES = {"reserved", "submitted", "open", "partially_filled", "cancel_requested", "unknown"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    module TEXT NOT NULL, category TEXT NOT NULL, market TEXT NOT NULL,
    event_group TEXT NOT NULL, strategy TEXT NOT NULL,
    side TEXT NOT NULL, action TEXT NOT NULL,
    qty INTEGER NOT NULL, limit_price INTEGER NOT NULL, max_cost INTEGER NOT NULL,
    day TEXT NOT NULL, status TEXT NOT NULL, exchange_order_id TEXT,
    created_ts REAL NOT NULL, updated_ts REAL NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS executions (
    seq INTEGER PRIMARY KEY,
    exec_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT REFERENCES orders(client_order_id),
    module TEXT NOT NULL, market TEXT NOT NULL, event_group TEXT NOT NULL, strategy TEXT NOT NULL,
    side TEXT NOT NULL, action TEXT NOT NULL CHECK (action IN ('buy','sell','settle')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    price INTEGER NOT NULL CHECK (price >= 0),
    fee INTEGER NOT NULL CHECK (fee >= 0),
    spend_day TEXT NOT NULL,
    day TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS exec_no_update BEFORE UPDATE ON executions
BEGIN SELECT RAISE(ABORT, 'executions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS exec_no_delete BEFORE DELETE ON executions
BEGIN SELECT RAISE(ABORT, 'executions are append-only'); END;
"""


class BudgetError(Exception):
    """A reservation was refused by a hard limit. `.reason` explains which."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LedgerIntegrityError(RuntimeError):
    """The ledger's records contradict each other. Trading must stop."""


@dataclass
class ReservationRequest:
    module: str
    market: str
    event_group: str
    strategy: str
    side: str
    qty: int
    limit_price: Money
    max_cost: Money              # price x qty + worst-case fee
    proposal_id: str = ""


@dataclass
class Position:
    module: str
    market: str
    side: str
    event_group: str
    strategy: str
    qty: int = 0
    basis: int = 0               # remaining cost basis (units), fees included


@dataclass
class ClosedTrade:
    market: str
    side: str
    strategy: str
    qty: int
    basis: int
    proceeds: int
    pnl: int
    day: str
    seq: int


@dataclass
class Snapshot:
    day: str
    spent: Money
    spent_by_category: dict[str, Money]
    purchases: Money
    fees: Money
    reserved: Money
    open_entry_orders: int
    positions: list[Position]
    exposure_total: Money
    exposure_by_market: dict[str, Money]
    exposure_by_event: dict[str, Money]
    exposure_by_strategy: dict[str, Money]
    realized_today: Money
    consecutive_losses: int
    consecutive_losses_by_strategy: dict[str, int]
    closed_trades: list[ClosedTrade] = field(default_factory=list)


class Ledger:
    def __init__(self, path: str | Path, audit: AuditLog | None, create: bool = False, clock=time.time,
                 readonly: bool = False):
        self.path = Path(path)
        self.audit = audit
        self.clock = clock
        if create and self.path.exists():
            raise StorageError("Refusing to create a ledger over an existing file")
        self.con = connect(self.path, must_exist=not create, readonly=readonly and not create)
        if create:
            self.con.executescript(SCHEMA)
            with immediate(self.con):
                self.con.execute("INSERT INTO meta VALUES ('marker', ?)", (MARKER,))
                self.con.execute("INSERT INTO meta VALUES ('created_ts', ?)", (str(clock()),))
            audit.append("ledger", decision="created", reason="New spending ledger created")
        else:
            try:
                row = self.con.execute("SELECT value FROM meta WHERE key='marker'").fetchone()
            except Exception as e:
                raise StorageError(f"Ledger unreadable: {e}") from e
            if row is None or row["value"] != MARKER:
                raise StorageError("Ledger is not a valid ATLAS ledger")

    # ------------------------------------------------------------------ replay
    def _replay(self, day: str):
        """Derive positions, closed trades and per-day money from executions."""
        pos: dict[tuple, Position] = {}
        closed: list[ClosedTrade] = []
        buy_spend_by_order: dict[str, int] = defaultdict(int)
        buy_qty_by_order: dict[str, int] = defaultdict(int)
        sell_fees_today = 0
        for r in self.con.execute("SELECT * FROM executions ORDER BY seq"):
            key = (r["module"], r["market"], r["side"])
            p = pos.setdefault(key, Position(r["module"], r["market"], r["side"],
                                             r["event_group"], r["strategy"]))
            if r["action"] == "buy":
                cost = r["qty"] * r["price"] + r["fee"]
                p.qty += r["qty"]
                p.basis += cost
                if r["client_order_id"]:
                    buy_spend_by_order[r["client_order_id"]] += cost
                    buy_qty_by_order[r["client_order_id"]] += r["qty"]
            else:
                if r["qty"] > p.qty:
                    raise LedgerIntegrityError(f"{r['action']} of {r['qty']} exceeds position {p.qty} in {r['market']}")
                removed = p.basis * r["qty"] // p.qty if p.qty else 0
                if r["qty"] == p.qty:
                    removed = p.basis
                proceeds = r["qty"] * r["price"] - r["fee"]
                p.qty -= r["qty"]
                p.basis -= removed
                closed.append(ClosedTrade(r["market"], r["side"], r["strategy"], r["qty"], removed,
                                          proceeds, proceeds - removed, r["day"], r["seq"]))
                if r["action"] == "sell" and r["day"] == day:
                    sell_fees_today += r["fee"]
        return pos, closed, buy_spend_by_order, buy_qty_by_order, sell_fees_today

    def snapshot(self, day: str) -> Snapshot:
        pos, closed, spend_by_order, _, sell_fees = self._replay(day)
        spent = sell_fees
        by_cat = defaultdict(int)
        purchases = fees = reserved = 0
        open_entries = 0
        res_by_market, res_by_event, res_by_strategy = defaultdict(int), defaultdict(int), defaultdict(int)
        for o in self.con.execute("SELECT * FROM orders WHERE action='buy'"):
            filled = spend_by_order.get(o["client_order_id"], 0)
            remaining = 0 if o["status"] in FINAL else max(0, o["max_cost"] - filled)
            if o["status"] in OPEN_STATES:
                open_entries += 1
                res_by_market[o["market"]] += remaining
                res_by_event[o["event_group"]] += remaining
                res_by_strategy[o["strategy"]] += remaining
            if o["day"] == day:
                spent += filled + remaining
                by_cat[o["category"]] += filled + remaining
                reserved += remaining
        for r in self.con.execute("SELECT qty, price, fee FROM executions WHERE action='buy' AND spend_day=?", (day,)):
            purchases += r["qty"] * r["price"]
            fees += r["fee"]
        fees += sell_fees
        open_pos = [p for p in pos.values() if p.qty > 0]
        exp_m, exp_e, exp_s = defaultdict(int), defaultdict(int), defaultdict(int)
        for p in open_pos:
            exp_m[p.market] += p.basis
            exp_e[p.event_group] += p.basis
            exp_s[p.strategy] += p.basis
        for d, src in ((exp_m, res_by_market), (exp_e, res_by_event), (exp_s, res_by_strategy)):
            for k, v in src.items():
                d[k] += v
        realized = sum(c.pnl for c in closed if c.day == day)
        streak, by_strat = 0, {}
        for c in reversed(closed):
            if c.pnl < 0:
                streak += 1
            else:
                break
        for strat in {c.strategy for c in closed}:
            n = 0
            for c in reversed([c for c in closed if c.strategy == strat]):
                if c.pnl < 0:
                    n += 1
                else:
                    break
            by_strat[strat] = n
        M = Money
        return Snapshot(
            day=day, spent=M(spent), spent_by_category={k: M(v) for k, v in by_cat.items()},
            purchases=M(purchases), fees=M(fees), reserved=M(reserved), open_entry_orders=open_entries,
            positions=open_pos, exposure_total=M(sum(exp_m.values())),
            exposure_by_market={k: M(v) for k, v in exp_m.items()},
            exposure_by_event={k: M(v) for k, v in exp_e.items()},
            exposure_by_strategy={k: M(v) for k, v in exp_s.items()},
            realized_today=M(realized), consecutive_losses=streak,
            consecutive_losses_by_strategy=by_strat, closed_trades=closed)

    # ---------------------------------------------------------------- reserve
    def reserve(self, req: ReservationRequest, limits: Limits, day: str, client_order_id: str | None = None) -> str:
        """Atomically check every hard limit and reserve the order's maximum
        cost. Returns the new client_order_id or raises BudgetError."""
        if req.module not in MODULE_CATEGORY:
            raise BudgetError(f"Unknown module {req.module}")
        if req.qty <= 0 or req.max_cost.units <= 0:
            raise BudgetError("Order quantity and cost must be positive")
        if req.max_cost.units < req.qty * req.limit_price.units:
            raise BudgetError("Maximum cost is less than price x quantity")
        cost = req.max_cost.units
        cat = MODULE_CATEGORY[req.module]
        coid = client_order_id or f"atlas-{uuid.uuid4()}"
        now = self.clock()
        with immediate(self.con):
            if self.con.execute("SELECT 1 FROM orders WHERE client_order_id=?", (coid,)).fetchone():
                raise BudgetError("This client order id was already used")
            s = self.snapshot(day)
            checks = [
                (cost <= limits.max_order.units,
                 f"Order cost {Money(cost)} is above the {limits.max_order} per-purchase limit"),
                (s.spent.units + cost <= limits.daily_spend.units,
                 f"Would exceed today's {limits.daily_spend} spending limit ({s.spent} used)"),
                (s.spent_by_category.get(cat, Money.ZERO).units + cost <= limits.category_limit(req.module).units,
                 f"Would exceed the {limits.category_limit(req.module)} {cat} category ceiling"),
                (s.open_entry_orders + 1 <= limits.max_open_orders,
                 f"Already {s.open_entry_orders} open entry orders (limit {limits.max_open_orders})"),
                (s.exposure_by_market.get(req.market, Money.ZERO).units + cost <= limits.max_market_exposure.units,
                 f"Would exceed the {limits.max_market_exposure} exposure limit for this market"),
                (s.exposure_total.units + cost <= limits.max_total_exposure.units,
                 f"Would exceed the {limits.max_total_exposure} total exposure limit"),
                (s.exposure_by_event.get(req.event_group, Money.ZERO).units + cost <= limits.max_event_exposure.units,
                 f"Would exceed the {limits.max_event_exposure} limit for event group {req.event_group}"),
                (s.exposure_by_strategy.get(req.strategy, Money.ZERO).units + cost <= limits.max_strategy_exposure.units,
                 f"Would exceed the {limits.max_strategy_exposure} limit for strategy {req.strategy}"),
            ]
            held = {(p.module, p.market, p.side) for p in s.positions}
            in_flight = self.con.execute(
                "SELECT 1 FROM orders WHERE market=? AND side=? AND action='buy' AND status IN "
                f"({','.join('?' * len(OPEN_STATES))})", (req.market, req.side, *OPEN_STATES)).fetchone()
            checks.append((in_flight is None, "An order for this market and side is already in flight"))
            other = "no" if req.side == "yes" else "yes"
            opposite = self.con.execute(
                "SELECT 1 FROM orders WHERE market=? AND side=? AND action='buy' AND status IN "
                f"({','.join('?' * len(OPEN_STATES))})", (req.market, other, *OPEN_STATES)).fetchone()
            checks.append(((req.module, req.market, other) not in held and opposite is None,
                           "Already holding or buying the opposite side of this market (Kalshi nets YES and NO)"))
            new_position = (req.module, req.market, req.side) not in held
            open_markets = {(p.module, p.market, p.side) for p in s.positions}
            pending_markets = {(r["module"], r["market"], r["side"]) for r in self.con.execute(
                "SELECT module, market, side FROM orders WHERE action='buy' AND status IN "
                f"({','.join('?' * len(OPEN_STATES))})", tuple(OPEN_STATES))}
            n_pos = len(open_markets | pending_markets) + (1 if new_position else 0)
            checks.append((n_pos <= limits.max_positions,
                           f"Would hold {n_pos} positions (limit {limits.max_positions})"))
            failed = [msg for ok, msg in checks if not ok]
            if failed:
                raise BudgetError("; ".join(failed))
            self.con.execute(
                "INSERT INTO orders (client_order_id, proposal_id, module, category, market, event_group, strategy,"
                " side, action, qty, limit_price, max_cost, day, status, created_ts, updated_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (coid, req.proposal_id, req.module, cat, req.market, req.event_group, req.strategy, req.side,
                 "buy", req.qty, req.limit_price.units, cost, day, "reserved", now, now))
        self.audit.append("budget_change", asset=req.market, strategy=req.strategy, decision="reserved",
                          reason=f"Reserved {Money(cost)} for {req.qty} {req.side}",
                          data={"client_order_id": coid, "max_cost": Money(cost), "day": day})
        return coid

    def reserve_exit(self, module: str, market: str, side: str, qty: int, limit_price: Money, day: str,
                     strategy: str, event_group: str) -> str:
        """Exits reduce risk, so they never consume the purchase budget, but they
        are still tracked so duplicates and unknown outcomes are handled."""
        coid = f"atlas-{side}-sell-{uuid.uuid4()}"
        now = self.clock()
        with immediate(self.con):
            s = self.snapshot(day)
            held = next((p for p in s.positions if (p.module, p.market, p.side) == (module, market, side)), None)
            if held is None or held.qty < qty:
                raise BudgetError("Cannot sell more than the ledger holds")
            self.con.execute(
                "INSERT INTO orders (client_order_id, module, category, market, event_group, strategy, side, action,"
                " qty, limit_price, max_cost, day, status, created_ts, updated_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (coid, module, MODULE_CATEGORY[module], market, event_group, strategy, side, "sell", qty,
                 limit_price.units, 0, day, "reserved", now, now))
        return coid

    # ------------------------------------------------------------ order states
    def _order(self, coid: str):
        row = self.con.execute("SELECT * FROM orders WHERE client_order_id=?", (coid,)).fetchone()
        if row is None:
            raise LedgerIntegrityError(f"Unknown client order id {coid}")
        return row

    def set_status(self, coid: str, status: str, exchange_order_id: str | None = None, note: str = ""):
        if status in FINAL:
            raise ValueError("Use finalize() for final states")
        if status not in OPEN_STATES:
            raise ValueError(f"Unknown status {status}")
        with immediate(self.con):
            o = self._order(coid)
            if o["status"] in FINAL:
                raise LedgerIntegrityError(f"Order {coid} is already final ({o['status']})")
            self.con.execute("UPDATE orders SET status=?, exchange_order_id=COALESCE(?, exchange_order_id),"
                             " updated_ts=?, note=? WHERE client_order_id=?",
                             (status, exchange_order_id, self.clock(), note, coid))
        self.audit.append("order_state", asset=o["market"], decision=status, reason=note or None,
                          data={"client_order_id": coid, "exchange_order_id": exchange_order_id})

    def finalize(self, coid: str, status: str, note: str = ""):
        """Record an EXCHANGE-CONFIRMED final state. Releases any unfilled
        reservation. Never call this on a guess (e.g. after a timeout)."""
        if status not in FINAL:
            raise ValueError(f"{status} is not a final state")
        with immediate(self.con):
            o = self._order(coid)
            if o["status"] in FINAL:
                return
            self.con.execute("UPDATE orders SET status=?, updated_ts=?, note=? WHERE client_order_id=?",
                             (status, self.clock(), note, coid))
        self.audit.append("order_state", asset=o["market"], decision=status, reason=note or None,
                          data={"client_order_id": coid, "released": status != "filled"})

    def record_execution(self, exec_id: str, coid: str | None, action: str, qty: int, price: Money,
                         fee: Money, day: str, *, module: str | None = None, market: str | None = None,
                         side: str | None = None, event_group: str | None = None,
                         strategy: str | None = None) -> bool:
        """Record a fill or settlement. Idempotent on exec_id (returns False if
        already recorded). Raises LedgerIntegrityError if it contradicts the order."""
        with immediate(self.con):
            if self.con.execute("SELECT 1 FROM executions WHERE exec_id=?", (exec_id,)).fetchone():
                return False
            if coid:
                o = self._order(coid)
                module, market, side = o["module"], o["market"], o["side"]
                event_group, strategy, spend_day = o["event_group"], o["strategy"], o["day"]
                if o["action"] != action:
                    raise LedgerIntegrityError(f"Execution {action} does not match order action {o['action']}")
                done = self.con.execute("SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(qty*price+fee),0) c "
                                        "FROM executions WHERE client_order_id=?", (coid,)).fetchone()
                if done["q"] + qty > o["qty"]:
                    raise LedgerIntegrityError(f"Fills for {coid} exceed the order quantity")
                if action == "buy" and done["c"] + qty * price.units + fee.units > o["max_cost"]:
                    raise LedgerIntegrityError(f"Fills for {coid} cost more than was reserved")
            else:
                if action != "settle" or None in (module, market, side, event_group, strategy):
                    raise LedgerIntegrityError("Only settlements may be recorded without an order")
                spend_day = day
            self.con.execute(
                "INSERT INTO executions (exec_id, client_order_id, module, market, event_group, strategy, side,"
                " action, qty, price, fee, spend_day, day, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (exec_id, coid, module, market, event_group, strategy, side, action, qty, price.units,
                 fee.units, spend_day, day, self.clock()))
            # Validate the derived state inside the same transaction.
            self._replay(day)
            if coid:
                filled = self.con.execute("SELECT SUM(qty) q FROM executions WHERE client_order_id=?",
                                          (coid,)).fetchone()["q"]
                if filled == self._order(coid)["qty"]:
                    self.con.execute("UPDATE orders SET status='filled', updated_ts=? WHERE client_order_id=?",
                                     (self.clock(), coid))
                elif self._order(coid)["status"] not in FINAL:
                    self.con.execute("UPDATE orders SET status='partially_filled', updated_ts=? "
                                     "WHERE client_order_id=?", (self.clock(), coid))
        self.audit.append("fill" if action != "settle" else "settlement", asset=market, strategy=strategy,
                          decision=action, data={"exec_id": exec_id, "client_order_id": coid, "qty": qty,
                                                 "price": price, "fee": fee})
        return True

    # ---------------------------------------------------------------- queries
    def open_orders(self) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            f"SELECT * FROM orders WHERE status IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY created_ts",
            tuple(OPEN_STATES))]

    def order(self, coid: str) -> dict:
        return dict(self._order(coid))

    def integrity_check(self) -> tuple[bool, str]:
        """Cross-check records. Any contradiction means trading must stop."""
        try:
            row = self.con.execute("SELECT value FROM meta WHERE key='marker'").fetchone()
            if row is None or row["value"] != MARKER:
                return False, "Ledger marker missing"
            for o in self.con.execute("SELECT * FROM orders"):
                agg = self.con.execute("SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(qty*price+fee),0) c "
                                       "FROM executions WHERE client_order_id=?", (o["client_order_id"],)).fetchone()
                if agg["q"] > o["qty"]:
                    return False, f"Order {o['client_order_id']} has more fills than its quantity"
                if o["action"] == "buy" and agg["c"] > o["max_cost"]:
                    return False, f"Order {o['client_order_id']} cost more than reserved"
                if o["status"] == "filled" and agg["q"] != o["qty"]:
                    return False, f"Order {o['client_order_id']} is marked filled but fills are incomplete"
            orphan = self.con.execute("SELECT exec_id FROM executions WHERE client_order_id IS NOT NULL AND "
                                      "client_order_id NOT IN (SELECT client_order_id FROM orders)").fetchone()
            if orphan:
                return False, f"Execution {orphan['exec_id']} references an unknown order"
            self._replay("0000-00-00")
        except LedgerIntegrityError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Ledger unreadable: {e}"
        return True, "Ledger consistent"
