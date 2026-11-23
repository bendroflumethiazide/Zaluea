"""The execution gateway: the ONLY code path that can place an order.

Strategies hand over a Proposal. The gateway runs the twelve verification
steps of ATLAS spec Part 16, in order, and rejects on the first failed
step (all failures are fail-closed: any exception means "no").

  1  authentication     exchange reachable with valid credentials
  2  account status     balance readable
  3  budget             ledger readable, consistent, on the verified day
  4  risk               primary risk manager approves
  5  shadow risk        shadow risk manager approves, with a valid signature
  6  correlation        event-group exposure (full engine arrives in Phase 4)
  7  positions          reconciled recently, shadow sees the same positions
  8  market data        quote fresh and sane (inside 4/5)
  9  price              a fresh re-read still fits the limit and slippage
 10  approval           owner approved THIS exact order (when required)
 11  duplicates         one order per market in flight; proposal ids single-use
 12  final              locks re-checked, budget reserved atomically, submit

Unknown outcomes (timeout, connection drop, 5xx) are never retried. The
order stays reserved as UNKNOWN until reconciliation finds out what the
exchange actually did.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .audit import AuditLog
from .clock import ClockGuard, accounting_day
from .control import ControlStore, LockStore, order_fingerprint, trading_locked
from .exchange.errors import AuthError, DataError, DuplicateOrder, ExchangeError, OrderRejected
from .health import HealthBoard
from .ledger import BudgetError, Ledger, LedgerIntegrityError, ReservationRequest
from .limits import LimitStore
from .money import Money, kalshi_taker_fee
from .paths import DataPaths
from .proposal import Proposal, Quote
from .risk import primary
from .shadowrisk.checker import verify_signature
from .storage import InstanceLock, StorageError

SHADOW_DECISION_MAX_AGE_S = 10.0
ORDER_EXPIRY_S = 30.0
UNKNOWN_GIVE_UP_S = 120.0
WATCHDOG_MAX_AGE_S = 15.0
INTEGRITY_RISK_CHECKS = {"authorization", "daily_spend", "category", "locks", "reconciliation", "day", "clock",
                         "max_order", "daily_loss", "consecutive_losses", "market_exposure", "total_exposure",
                         "event_exposure", "positions", "open_orders", "cash", "module"}
# Shadow findings that can differ from the primary purely because the market moved between reads.
SHADOW_MARKET_CODES = {"DATA_AGE", "DEPTH", "SLIPPAGE", "CROSSED"}


@dataclass
class GatewayResult:
    status: str                      # rejected | submitted | filled | partially_filled | unknown | cancelled
    reasons: list[str] = field(default_factory=list)
    client_order_id: str | None = None
    fingerprint: str | None = None


class Gateway:
    def __init__(self, paths: DataPaths, exchange, clock_guard: ClockGuard, shadow, clock=time.time):
        self.paths = paths
        self.ex = exchange
        self.clock_guard = clock_guard
        self.shadow = shadow
        self.clock = clock
        self.lock = InstanceLock(paths.gateway_lock)
        self.started = False
        self.reconciled_ok = False
        self.reconciled_at: float | None = None
        self.seen_proposals: set[str] = set()
        self.unknown_since: dict[str, float] = {}
        self.last_write_ts = 0.0          # when ATLAS last placed or cancelled an order
        self.waiting_for_exchange_data = False

    # ---------------------------------------------------------------- startup
    def start(self) -> None:
        self.lock.acquire()                                  # one gateway per data folder
        p = self.paths
        self.audit = AuditLog(p.audit)                       # all must already exist (fail closed)
        self.ledger = Ledger(p.ledger, self.audit, clock=self.clock)
        self.control = ControlStore(p.control, self.audit, clock=self.clock)
        self.limits = LimitStore(p.control, self.audit)
        self.locks = LockStore(p.locks, self.audit, clock=self.clock)
        self.health = HealthBoard(p.health, clock=self.clock)
        self.shadow_pub = p.shadow_public_key.read_bytes()
        self.control.boot()                                  # authorization OFF, approval ON
        v = self.audit.verify()
        if not v.ok:
            self.locks.set_lock("audit", v.reason, "gateway")
            self.health.notify("critical", "Audit log verification failed", v.reason)
        self.clock_guard.check()
        self.reconcile()
        self.started = True
        self.heartbeat()

    def stop(self) -> None:
        self.lock.release()

    def verified_day(self) -> str | None:
        return self.clock_guard.verified_day()

    def heartbeat(self) -> None:
        day = self.verified_day()
        status = {"reconciled_ok": self.reconciled_ok, "reconciled_at": self.reconciled_at, "day": day}
        try:
            if day:
                s = self.ledger.snapshot(day)
                status |= {"spent_units": s.spent.units, "reserved_units": s.reserved.units,
                           "open_entry_orders": s.open_entry_orders}
        except Exception as e:
            status["ledger_error"] = str(e)
        self.health.beat("gateway", status)
        self.health.beat("primary_risk", {"ok": True})
        try:
            self.shadow.heartbeat()
        except Exception:
            pass

    def watchdog_healthy(self) -> tuple[bool, str]:
        """Live trading fails closed if the watchdog is not running and healthy."""
        try:
            wd = self.health.read().get("watchdog")
        except Exception as e:
            return False, f"Cannot read watchdog status: {e}"
        if wd is None:
            return False, "Watchdog is not running"
        if self.clock() - wd["ts"] > WATCHDOG_MAX_AGE_S:
            return False, "Watchdog heartbeat is stale"
        if wd["status"].get("ok") is False:
            return False, "Watchdog reports a problem"
        return True, ""

    # ----------------------------------------------------------- owner controls
    def emergency_stop(self, reason: str, actor: str = "owner") -> None:
        """STOP ALL NEW TRADING: block entries, revoke authorization, cancel
        outstanding entry orders, preserve positions, reconcile, notify."""
        self.locks.set_lock("emergency_stop", reason, actor)
        self.control.revoke_authorization("emergency stop", actor)
        self.cancel_entry_orders(reason="emergency stop")
        self.reconcile()
        self.health.notify("critical", "STOP ALL NEW TRADING", f"{reason} (by {actor}). Positions preserved.")

    def clear_lock(self, kind: str, confirm: str, actor: str = "owner") -> None:
        ok = self.reconcile()
        self.locks.clear(kind, confirm, reconciled_ok=ok, actor=actor)

    # ------------------------------------------------------------- the pipeline
    def submit_entry(self, prop: Proposal, approval_id: str | None = None) -> GatewayResult:
        try:
            return self._submit_entry(prop, approval_id)
        except Exception as e:                               # fail closed on anything unexpected
            self.audit.append("trade_rejection", asset=prop.market, strategy=prop.strategy,
                              decision="rejected", reason=f"Internal error: {e}")
            return GatewayResult("rejected", [f"Internal error: {e}"])

    def _reject(self, prop: Proposal, step: str, reasons: list[str], fp: str | None = None,
                coid: str | None = None) -> GatewayResult:
        if coid:
            try:
                self.shadow.release(coid)          # shadow verifies with the exchange itself
            except Exception:
                pass
        self.audit.append("trade_rejection", asset=prop.market, strategy=prop.strategy,
                          model_version=prop.model_version, decision="rejected",
                          reason=f"Step {step}: " + "; ".join(reasons),
                          data={"proposal_id": prop.proposal_id})
        return GatewayResult("rejected", [f"{step}: {r}" for r in reasons], fingerprint=fp)

    def _submit_entry(self, prop: Proposal, approval_id: str | None) -> GatewayResult:
        if not self.started:
            return GatewayResult("rejected", ["Gateway not started"])
        cost = primary.max_cost(prop)
        fp = order_fingerprint(prop.module, prop.market, prop.side, prop.action, prop.qty, prop.limit_price, cost)
        self.audit.append("trade_proposal", asset=prop.market, strategy=prop.strategy,
                          model_version=prop.model_version, reason=prop.reason,
                          data={"proposal_id": prop.proposal_id, "side": prop.side, "qty": prop.qty,
                                "limit_price": prop.limit_price, "max_cost": cost, "prob_bp": prop.prob_bp,
                                "expected_value": prop.expected_value, "fingerprint": fp})
        if prop.proposal_id in self.seen_proposals:                                    # step 11 (early)
            return self._reject(prop, "11 duplicates", ["This proposal was already processed"], fp)
        self.seen_proposals.add(prop.proposal_id)
        locked, why = trading_locked(self.locks)
        if locked:
            return self._reject(prop, "12 final", [f"Trading locked: {why}"], fp)
        wd_ok, wd_why = self.watchdog_healthy()
        if not wd_ok:
            return self._reject(prop, "12 final", [wd_why], fp)

        # 1-2 authentication and account status
        try:
            self.ex.server_time()
            cash = Money.parse(self.ex.balance())
        except Exception as e:
            return self._reject(prop, "1-2 exchange", [f"Exchange unavailable or credentials failed: {e}"], fp)

        # 7 positions: reconcile with the exchange right now, before any decision
        if not self.reconcile():
            return self._reject(prop, "7 positions", ["Reconciliation with the exchange failed"], fp)
        locked, why = trading_locked(self.locks)     # reconciliation may have set a loss lock
        if locked:
            return self._reject(prop, "12 final", [f"Trading locked: {why}"], fp)

        # fees: the proposal's fee assumption must match the market's real fee schedule
        if prop.module in ("kalshi", "esports"):
            try:
                info = self.ex.market_info(prop.market)
            except Exception as e:
                return self._reject(prop, "8 market data", [f"Could not read market details: {e}"], fp)
            pct = primary.fee_multiplier_pct(info.get("fee_type"), info.get("fee_multiplier"))
            if pct is None:
                return self._reject(prop, "8 market data", [f"Fee type {info.get('fee_type')!r} x "
                                    f"{info.get('fee_multiplier')} cannot be computed exactly; not trading it"], fp)
            if pct != prop.fee_multiplier_pct:
                return self._reject(prop, "8 market data", [f"Proposal assumed a {prop.fee_multiplier_pct}% fee "
                                    f"multiplier; the market uses {pct}%"], fp)
            if info.get("module") != prop.module:
                return self._reject(prop, "8 market data", [f"Market is {info.get('module')}, not {prop.module}"], fp)

        # 3 budget state
        day = self.verified_day()
        if day is None:
            return self._reject(prop, "3 budget", ["Clock not verified: accounting day uncertain"], fp)
        ok, why = self.ledger.integrity_check()
        if not ok:
            self.locks.set_lock("integrity", why, "gateway")
            return self._reject(prop, "3 budget", [why], fp)
        limits = self.limits.current(day)
        snap = self.ledger.snapshot(day)
        snap_ts = self.clock()

        # 4 primary risk
        ctx = primary.RiskContext(
            now=self.clock(), verified_day=day, limits=limits, snapshot=snap,
            authorized=self.control.is_authorized(day), module_enabled=self.control.module_enabled(prop.module),
            locked=locked, lock_reason=why, reconciled_ok=self.reconciled_ok, reconciled_at=self.reconciled_at,
            cash=cash, unrealized=Money.ZERO)
        decision = primary.evaluate(prop, ctx)

        # 5 shadow risk (always asked, so disagreements are always visible)
        coid = f"atlas-{prop.side}-buy-{uuid.uuid4()}"   # intent is recoverable from the exchange
        order = {"module": prop.module, "market": prop.market, "side": prop.side, "qty": prop.qty,
                 "limit_price": prop.limit_price.to_str(), "max_cost": cost.to_str(), "strategy": prop.strategy,
                 "client_order_id": coid, "fingerprint": fp}
        view = {"day": day, "spent": snap.spent.to_str(), "snapshot_ts": snap_ts,
                "authorized": ctx.authorized,
                "positions": [{"market": p.market, "side": p.side, "qty": p.qty} for p in snap.positions]}
        sd = self.shadow.review(order, view)
        sig_ok = (verify_signature(self.shadow_pub, sd.payload, sd.signature)
                  and sd.payload.get("fingerprint") == fp and sd.payload.get("client_order_id") == coid
                  and self.clock() - float(sd.payload.get("ts", 0)) <= SHADOW_DECISION_MAX_AGE_S)
        shadow_ok = sd.approved and sig_ok
        self.audit.append("risk_decision", asset=prop.market, strategy=prop.strategy,
                          decision="approved" if decision.approved else "rejected",
                          reason="; ".join(decision.reasons) or "All checks passed",
                          data={"manager": "primary", "fingerprint": fp})
        self.audit.append("shadow_risk_decision", asset=prop.market, strategy=prop.strategy,
                          decision="approved" if shadow_ok else "rejected",
                          reason="; ".join(f"{c}: {m}" for c, m in sd.problems) or
                                 ("Signature invalid" if not sig_ok else "All checks passed"),
                          data={"manager": "shadow", "fingerprint": fp, "signature_valid": sig_ok})
        if decision.approved != shadow_ok:
            return self._disagreement(prop, decision, sd, sig_ok, fp, coid)
        if not decision.approved:
            return self._reject(prop, "4-5 risk", decision.reasons, fp)

        # 6 correlation (conservative until the Phase 4 engine): event group must be named
        if not prop.event_group:
            return self._reject(prop, "6 correlation", ["No event group: correlation unknown"], fp, coid)

        # 9 price: re-read the market right before submitting
        try:
            fresh = self.ex.orderbook(prop.market, prop.side)
        except Exception as e:
            return self._reject(prop, "9 price", [f"Could not re-read the market: {e}"], fp, coid)
        ask = Money.parse(fresh["ask"])
        slip = Money(-(-prop.quote.ask.units * limits.max_slippage_bps // 10_000))
        if ask > prop.limit_price or ask > prop.quote.ask + slip or fresh["ask_size"] < prop.qty:
            return self._reject(prop, "9 price", [f"Market moved: ask now {ask} x {fresh['ask_size']}"], fp, coid)

        # 10 approval
        if self.control.require_approval():
            ok, why_a = self.control.consume_approval(approval_id, fp)
            if not ok:
                return self._reject(prop, "10 approval", [why_a], fp, coid)

        # 11-12 duplicates + final: locks again, then atomic reservation
        locked, why = trading_locked(self.locks)
        if locked:
            return self._reject(prop, "12 final", [f"Trading locked: {why}"], fp, coid)
        try:
            self.ledger.reserve(ReservationRequest(prop.module, prop.market, prop.event_group, prop.strategy,
                                                   prop.side, prop.qty, prop.limit_price, cost, prop.proposal_id),
                                limits, day, client_order_id=coid)
        except BudgetError as e:
            return self._reject(prop, "12 final", [e.reason], fp, coid)
        self.audit.append("order_submission", asset=prop.market, strategy=prop.strategy, decision="submitting",
                          data={"client_order_id": coid, "fingerprint": fp, "max_cost": cost})
        return self._place(prop, coid, fp)

    def _disagreement(self, prop, decision, sd, sig_ok, fp, coid) -> GatewayResult:
        try:
            self.shadow.release(coid)
        except Exception:
            pass
        primary_failed = {c.name for c in decision.checks if not c.ok}
        shadow_codes = {c for c, _ in sd.problems}
        integrity = (
            not sig_ok
            or sd.integrity_problem
            # primary approved something the shadow rejected for a non-market reason: primary is wrong
            or (decision.approved and bool(shadow_codes - SHADOW_MARKET_CODES))
            # shadow approved something the primary rejected for a hard limit: shadow is wrong
            or (not decision.approved and sd.approved and bool(primary_failed & INTEGRITY_RISK_CHECKS))
        )
        detail = (f"Primary {'approved' if decision.approved else 'rejected'} "
                  f"({'; '.join(decision.reasons) or 'all checks passed'}); "
                  f"shadow {'approved' if sd.approved and sig_ok else 'rejected'} "
                  f"({'; '.join(f'{c}: {m}' for c, m in sd.problems) or ('bad signature' if not sig_ok else 'all checks passed')})")
        self.audit.append("risk_disagreement", asset=prop.market, strategy=prop.strategy, decision="rejected",
                          reason=detail, data={"fingerprint": fp, "integrity": integrity})
        self.health.notify("warning", "Risk managers disagreed", detail)
        if integrity:
            self.locks.set_lock("risk_disagreement", detail[:300], "gateway")
        return GatewayResult("rejected", ["Risk managers disagreed: " + detail], fingerprint=fp)

    # --------------------------------------------------------------- execution
    def _place(self, prop: Proposal, coid: str, fp: str) -> GatewayResult:
        self.ledger.set_status(coid, "submitted")
        self.last_write_ts = self.clock()
        try:
            self.ex.place_order(coid, prop.market, prop.side, "buy", prop.qty, prop.limit_price.to_str(),
                                tif="ioc", expiration_ts=self.clock() + ORDER_EXPIRY_S)
        except OrderRejected as e:
            self.ledger.finalize(coid, "rejected", note=str(e))
            return GatewayResult("rejected", [f"Exchange rejected: {e}"], coid, fp)
        except DuplicateOrder:
            pass                                             # it exists: learn its state below
        except (TimeoutError, ConnectionError, ExchangeError, OSError) as e:
            # The order MAY have been placed. Never resubmit; find out instead.
            self.ledger.set_status(coid, "unknown", note=f"{type(e).__name__}: {e}")
            self.unknown_since[coid] = self.clock()
            self.health.notify("warning", "Order outcome unknown", f"{prop.market}: {e}. Reconciling; not retrying.")
            try:
                self.sync_order(coid)
            except Exception:
                pass                                         # reconciliation will keep trying
            return GatewayResult(self.ledger.order(coid)["status"], [f"Outcome unknown after {type(e).__name__}"], coid, fp)
        status = self.sync_order(coid)
        return GatewayResult(status, [], coid, fp)

    def sync_order(self, coid: str) -> str:
        """Bring one order up to date from the exchange. Never resubmits."""
        led = self.ledger.order(coid)
        o = self.ex.get_order(coid, market=led["market"])
        if o is None:
            since = self.unknown_since.setdefault(coid, self.clock())
            if led["status"] in ("unknown", "submitted") and self.clock() - since > UNKNOWN_GIVE_UP_S:
                self.ledger.finalize(coid, "rejected", note="Exchange confirms no such order exists")
                return "rejected"
            return led["status"]
        for f in self.ex.fills(0.0):
            if f["client_order_id"] == coid:
                self.ledger.record_execution(f["fill_id"], coid, f["action"], f["qty"], Money.parse(f["price"]),
                                             Money.parse(f["fee"]), accounting_day(f["ts"]))
        self.unknown_since.pop(coid, None)
        if o["status"] in ("filled", "cancelled", "expired", "rejected"):
            self.ledger.finalize(coid, o["status"], note="confirmed by exchange")
        elif o["status"] == "open":
            cur = self.ledger.order(coid)["status"]
            if cur not in ("partially_filled", "cancel_requested"):
                self.ledger.set_status(coid, "open", exchange_order_id=o["order_id"])
        return self.ledger.order(coid)["status"]

    def cancel_entry_orders(self, reason: str) -> None:
        for o in self.ledger.open_orders():
            if o["action"] != "buy":
                continue
            try:
                r = self.ex.cancel(o["client_order_id"], market=o["market"])
                if r["status"] == "cancel_requested":
                    self.ledger.set_status(o["client_order_id"], "cancel_requested", note=reason)
                self.sync_order(o["client_order_id"])
            except Exception as e:
                self.audit.append("order_cancellation", asset=o["market"], decision="failed",
                                  reason=f"{reason}: {e}")

    # ---------------------------------------------------------- reconciliation
    def reconcile(self) -> bool:
        """Compare the ledger with the exchange. Any unexplained difference
        locks trading. Returns True only if everything matches."""
        # The exchange's account data lags its writes slightly. Until it includes our
        # latest order, a comparison would show false mismatches: report "not yet", no lock.
        try:
            as_of = self.ex.data_as_of() if hasattr(self.ex, "data_as_of") else self.clock()
        except Exception as e:
            self.reconciled_ok = False
            self.audit.append("reconciliation", decision="deferred", reason=f"Exchange data time unavailable: {e}")
            return False
        if as_of < self.last_write_ts:
            self.reconciled_ok = False
            self.waiting_for_exchange_data = True
            return False
        self.waiting_for_exchange_data = False
        try:
            day = self.verified_day() or accounting_day(self.clock())
            for o in self.ledger.open_orders():
                self.sync_order(o["client_order_id"])
            known = {o["client_order_id"] for o in self.ledger.con.execute("SELECT client_order_id FROM orders")}
            for f in self.ex.fills(0.0):
                if f["client_order_id"] not in known:
                    raise LedgerIntegrityError(f"Exchange shows a fill ATLAS never ordered ({f['market']})")
                self.ledger.record_execution(f["fill_id"], f["client_order_id"], f["action"], f["qty"],
                                             Money.parse(f["price"]), Money.parse(f["fee"]), accounting_day(f["ts"]))
            positions = {(p.market, p.side): p for p in self.ledger.snapshot(day).positions}
            for s in self.ex.settlements(0.0):
                p = positions.get((s["market"], s["side"]))
                already = self.ledger.con.execute("SELECT 1 FROM executions WHERE exec_id=?", (s["settle_id"],)).fetchone()
                if already:
                    continue
                if p is None:
                    raise LedgerIntegrityError(f"Settlement for a position ATLAS does not hold ({s['market']})")
                payout_each = Money(Money.parse(s["payout"]).units // s["qty"]) if s["qty"] else Money.ZERO
                self.ledger.record_execution(s["settle_id"], None, "settle", s["qty"], payout_each, Money.ZERO,
                                             accounting_day(s["ts"]),
                                             module=p.module, market=p.market, side=p.side,
                                             event_group=p.event_group, strategy=p.strategy)
            ours = {(p.market, p.side): p.qty for p in self.ledger.snapshot(day).positions}
            theirs = {(p["market"], p["side"]): int(p["qty"]) for p in self.ex.positions()}
            if ours != theirs:
                raise LedgerIntegrityError(f"Positions differ: ledger {ours} vs exchange {theirs}")
            ok, why = self.ledger.integrity_check()
            if not ok:
                raise LedgerIntegrityError(why)
            self._check_loss_limits(day)
        except Exception as e:
            self.reconciled_ok = False
            self.locks.set_lock("reconciliation", str(e)[:300], "gateway")
            self.health.notify("critical", "Reconciliation failed", str(e))
            self.audit.append("reconciliation", decision="failed", reason=str(e))
            return False
        self.reconciled_ok, self.reconciled_at = True, self.clock()
        self.audit.append("reconciliation", decision="ok", reason="Ledger matches exchange")
        return True

    def _check_loss_limits(self, day: str) -> None:
        limits = self.limits.current(day)
        s = self.ledger.snapshot(day)
        if -s.realized_today.units >= limits.max_daily_loss.units:
            self.locks.set_lock("daily_loss", f"Realized {s.realized_today} today", "gateway")
            self.cancel_entry_orders("daily loss limit")
            self.health.notify("critical", "Daily loss limit reached", f"Realized {s.realized_today}. New entries stopped.")
        if s.consecutive_losses >= limits.max_consec_losses:
            self.locks.set_lock("consecutive_losses", f"{s.consecutive_losses} losing trades in a row", "gateway")
            self.health.notify("warning", "Consecutive losses", f"{s.consecutive_losses} in a row. New entries paused.")


def kalshi_quote_from_book(book: dict, clock=time.time) -> Quote:
    return Quote(book["market"], book["side"], Money.parse(book["bid"]), Money.parse(book["ask"]),
                 int(book["ask_size"]), float(book["ts"]), "exchange")
