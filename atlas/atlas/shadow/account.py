"""A simulated account: its own SimExchange, its own ledger and audit log in its own folder,
and the same hard limits the real system enforces.

It can never touch real money: it holds no exchange client at all, refuses to open any file
that is not inside a folder named `sim`, and refuses any file name the real system uses there.
"""
from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from ..audit import AuditLog
from ..ledger import BudgetError, Ledger, ReservationRequest
from ..limits import Limits
from ..money import Money
from ..risk.primary import max_cost
from ..proposal import Proposal
from .journal import Journal
from .sim import SimExchange

SIM_DIRNAME = "sim"


class SeparationError(RuntimeError):
    """A simulated account was pointed at something that is not simulated."""


def research_limits(base: Limits) -> Limits:
    """Same per-order and per-market caps as live, but no daily stop-outs, so the research
    account keeps collecting evidence after the as-live account has hit its daily limits."""
    big = Money.dollars(1_000_000)
    return dataclasses.replace(base, daily_spend=big, cat_kalshi=big, cat_stocks=big, cat_etfs=big,
                               max_daily_loss=big, max_drawdown=big, max_consec_losses=1_000_000)


class SimAccount:
    def __init__(self, folder: str | Path, name: str, strategy: str, kind: str, limits: Limits,
                 journal: Journal, latency_s: float = 1.0, maker_fee_pct: int = 25, clock=None):
        folder = Path(folder).resolve()
        if SIM_DIRNAME not in folder.parts:
            raise SeparationError(f"Simulated accounts live only under a '{SIM_DIRNAME}' folder, not {folder}")
        folder.mkdir(parents=True, exist_ok=True)
        self.folder, self.name, self.strategy, self.kind = folder, name, strategy, kind
        self.limits, self.journal = limits, journal
        self.sim = SimExchange(latency_s=latency_s, maker_fee_pct=maker_fee_pct)
        create = not (folder / "sim_ledger.db").exists()
        self.audit = AuditLog(folder / "sim_audit.db", create=not (folder / "sim_audit.db").exists())
        kw = {"clock": clock} if clock else {}
        self.ledger = Ledger(folder / "sim_ledger.db", self.audit, create=create, **kw)
        self.meta: dict[str, dict] = {}            # coid -> {market, side, expires_at, event_group}

    # ------------------------------------------------------------------ entries
    def precheck(self, day: str) -> list[str]:
        """Loss-based stops the ledger's reserve() does not cover (the risk manager does, live)."""
        s, L = self.ledger.snapshot(day), self.limits
        out = []
        if -s.realized_today >= L.max_daily_loss:
            out.append(f"Daily loss limit reached ({s.realized_today})")
        if s.consecutive_losses >= L.max_consec_losses:
            out.append(f"{s.consecutive_losses} losses in a row (limit {L.max_consec_losses})")
        return out

    def submit(self, p: Proposal, *, now: float, day: str, expires_at: float, kind: str) -> tuple[str | None, str]:
        """Reserve exactly like the live ledger, then hand the order to the simulator."""
        stops = self.precheck(day)
        if stops:
            return None, "; ".join(stops)
        req = ReservationRequest(p.module, p.market, p.event_group, p.strategy, p.side, p.qty, p.limit_price,
                                 max_cost(p), p.proposal_id)
        try:
            coid = self.ledger.reserve(req, self.limits, day, client_order_id=f"sim-{p.side}-buy-{uuid.uuid4()}")
        except BudgetError as e:
            return None, e.reason
        self.ledger.set_status(coid, "submitted", note="simulated")
        self.sim.submit(coid, p.market, p.side, p.qty, p.limit_price, kind, now, expires_at, p.fee_multiplier_pct)
        self.meta[coid] = {"market": p.market, "side": p.side, "event_group": p.event_group}
        return coid, "submitted"

    def cancel(self, coid: str, now: float) -> None:
        self.sim.cancel(coid, now)

    # ------------------------------------------------------------------ time moves
    def step(self, now: float, day: str, trades_by_market: dict) -> list[dict]:
        """Advance the simulator and book what happened. Returns journal-ready events."""
        before = {c: o.status for c, o in self.sim.orders.items()}
        events = []
        for coid, fill_id, q, price, fee, ts in self.sim.step(now, trades_by_market):
            self.ledger.record_execution(fill_id, coid, "buy", q, price, fee, day)
            events.append({"kind": "fill", "coid": coid, "qty": q, "price": price, "fee": fee, "ts": ts})
        for coid, o in self.sim.orders.items():
            if o.status in ("cancelled", "expired") and before.get(coid) not in ("cancelled", "expired"):
                self.ledger.finalize(coid, o.status, note="simulated")
                events.append({"kind": o.status, "coid": coid, "qty": o.qty - o.filled, "price": o.limit,
                               "fee": None, "ts": now})
        return events

    def settle(self, market: str, result: str, day: str) -> list[dict]:
        """Pay out every simulated position in a settled market ($1 a winning contract, no fee)."""
        out = []
        for p in self.ledger.snapshot(day).positions:
            if p.market != market or p.qty <= 0:
                continue
            price = Money.dollars(1) if p.side == result else Money.ZERO
            if self.ledger.record_execution(f"settle-{market}-{p.side}", None, "settle", p.qty, price,
                                            Money.ZERO, day, module=p.module, market=market, side=p.side,
                                            event_group=p.event_group, strategy=p.strategy):
                out.append({"kind": "settled", "side": p.side, "qty": p.qty, "price": price,
                            "pnl": price * p.qty - Money(p.basis)})
        return out

    def held_markets(self, day: str) -> set[str]:
        return {p.market for p in self.ledger.snapshot(day).positions if p.qty > 0}

    def close(self):
        self.ledger.con.close()
        self.audit.close()
