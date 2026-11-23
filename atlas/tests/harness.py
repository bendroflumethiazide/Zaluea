"""Builds a complete ATLAS safety stack on a fake exchange for tests."""
from dataclasses import dataclass

from atlas.clock import ClockGuard, accounting_day
from atlas.control import order_fingerprint
from atlas.exchange.fake import FakeExchange
from atlas.gateway import Gateway
from atlas.money import Money
from atlas.paths import initialize
from atlas.proposal import Proposal, Quote
from atlas.risk import primary
from atlas.shadowrisk.checker import ShadowRiskManager

T0 = 1_795_400_000.0          # 2026-11-22 evening, New York


@dataclass
class Stack:
    t: list
    paths: object
    ex: FakeExchange
    shadow: ShadowRiskManager
    gw: Gateway

    def now(self):
        return self.t[0]

    def advance(self, s):
        self.t[0] += s

    @property
    def day(self):
        return accounting_day(self.t[0])

    def proposal(self, market="BTC-A", side="yes", qty=2, limit="0.88", ask=None, bid=None, size=None,
                 module="kalshi", event=None, ev="0.06", strategy="flip_fav", quote_age=0.5):
        book = self.ex.markets[market]["books"][side]
        q = Quote(market, side, Money.parse(bid) if bid else Money(book.bid),
                  Money.parse(ask) if ask else Money(book.ask), size if size is not None else book.ask_size,
                  self.t[0] - quote_age, "test")
        return Proposal(module=module, market=market, event_group=event or self.ex.markets[market]["event"],
                        strategy=strategy, model_version="test-1", side=side, qty=qty, limit_price=Money.parse(limit),
                        quote=q, prob_bp=9_190, expected_value=Money.parse(ev), reason="test", exit_plan="hold")

    def approve(self, p):
        fp = order_fingerprint(p.module, p.market, p.side, p.action, p.qty, p.limit_price, primary.max_cost(p))
        return self.gw.control.grant_approval(fp, "test approval")

    def ready(self, modules=("kalshi",)):
        """Owner enables modules and authorizes today's budget."""
        for m in modules:
            self.gw.control.set_module(m, True)
        self.gw.control.authorize_today(self.gw.verified_day(), confirm="AUTHORIZE")

    def watchdog_ok(self):
        """Stand-in heartbeat from a healthy watchdog (the real one is tested in test_watchdog.py)."""
        self.gw.health.beat("watchdog", {"ok": True})

    def buy(self, p, approve=True):
        self.watchdog_ok()
        return self.gw.submit_entry(p, self.approve(p) if approve else None)


def build(tmp_path) -> Stack:
    t = [T0]
    clock = lambda: t[0]
    paths = initialize(tmp_path / "data")
    ex = FakeExchange(clock=clock)
    for i, ev in enumerate(["BTC-1245", "BTC-1300", "ETH-1245", "CS2-VIT-FAZE"]):
        ex.add_market(["BTC-A", "BTC-B", "ETH-A", "CS2-A"][i], ev, module="esports" if ev.startswith("CS2") else "kalshi")
    shadow = ShadowRiskManager(ex, paths.control, paths.locks, paths.shadow_journal,
                               paths.shadow_private_key.read_bytes(), clock=clock, health_db=paths.health)
    gw = Gateway(paths, ex, ClockGuard([ex.server_time], local=clock), shadow, clock=clock)
    gw.start()
    return Stack(t, paths, ex, shadow, gw)
