"""The whole ATLAS stack (gateway, ledger, both risk managers) driving the REAL
KalshiExchange client against a simulator that speaks Kalshi's V2 HTTP API."""
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.clock import ClockGuard
from atlas.exchange.fake import FakeExchange
from atlas.exchange.kalshi import DEMO, KalshiExchange, Signer
from atlas.gateway import Gateway
from atlas.money import Money
from atlas.paths import initialize
from atlas.shadowrisk.checker import ShadowRiskManager

from .harness import T0, Stack
from .kalshi_sim import KalshiSim

M = Money.parse


def signer():
    k = Ed25519PrivateKey.generate()
    return Signer("demo-key", k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))


@pytest.fixture
def st(tmp_path):
    t = [T0]
    clock = lambda: t[0]
    paths = initialize(tmp_path / "data")
    fake = FakeExchange(clock=clock)
    for m, ev in (("KXBTC15M-A", "KXBTC15M-1845"), ("KXBTC15M-B", "KXBTC15M-1900"), ("KXETH15M-A", "KXETH15M-1845")):
        fake.add_market(m, ev)
    sim = KalshiSim(fake)
    trading_client = KalshiExchange(DEMO, signer(), transport=sim, clock=clock, min_interval=0)
    shadow_client = KalshiExchange(DEMO, signer(), transport=sim, clock=clock, min_interval=0)   # its own key
    shadow = ShadowRiskManager(shadow_client, paths.control, paths.locks, paths.shadow_journal,
                               paths.shadow_private_key.read_bytes(), clock=clock, health_db=paths.health)
    gw = Gateway(paths, trading_client, ClockGuard([trading_client.server_time], local=clock), shadow, clock=clock)
    gw.start()
    s = Stack(t, paths, fake, shadow, gw)
    s.sim = sim
    yield s
    gw.stop()


def posts(st):
    return [r for r in st.sim.requests if r[0] == "POST"]


def test_buy_fills_and_reconciles_through_the_real_client(st):
    st.ready()
    r = st.buy(st.proposal(market="KXBTC15M-A", qty=2, limit="0.88"))
    assert r.status == "filled", r.reasons
    s = st.gw.ledger.snapshot(st.day)
    assert s.spent == M("1.78") and s.positions[0].qty == 2
    assert st.gw.reconcile() and st.gw.locks.active() == []
    body = posts(st)[0][2]
    assert body["side"] == "bid" and body["price"] == "0.880000" and body["time_in_force"] == "immediate_or_cancel"
    assert body["client_order_id"].startswith("atlas-yes-buy-")


def test_timeout_after_processing_is_not_resubmitted(st):
    st.ready()
    st.sim.http_faults.append("timeout_after")
    r = st.buy(st.proposal(market="KXBTC15M-A", qty=2))
    assert len(posts(st)) == 1
    assert r.status == "filled" and st.gw.ledger.snapshot(st.day).spent == M("1.78")


def test_timeout_before_processing_holds_then_releases_without_retry(st):
    st.ready()
    st.sim.http_faults.append("timeout_before")
    r = st.buy(st.proposal(market="KXBTC15M-A", qty=2))
    assert r.status == "unknown" and st.gw.ledger.snapshot(st.day).reserved == M("1.78")
    st.advance(130)
    st.gw.clock_guard.check()
    st.gw.reconcile()
    assert st.gw.ledger.snapshot(st.day).spent == Money.ZERO
    assert len(posts(st)) == 1 and st.ex.orders == {}


def test_bad_gateway_after_processing_is_reconciled(st):
    st.ready()
    st.sim.http_faults.append("502_after")
    r = st.buy(st.proposal(market="KXBTC15M-A", qty=1))
    assert r.status == "filled" and len(posts(st)) == 1


def test_losses_from_settlements_trigger_daily_loss_lock(st):
    st.ready()
    for m in ("KXBTC15M-A", "KXETH15M-A"):
        assert st.buy(st.proposal(market=m, qty=2, limit="0.88")).status == "filled"
        st.ex.settle(m, "no")
    st.gw.reconcile()
    assert any(l["kind"] == "daily_loss" for l in st.gw.locks.active())
    assert st.gw.ledger.snapshot(st.day).realized_today == M("-3.56")


def test_every_portfolio_call_was_signed(st):
    st.ready()
    st.buy(st.proposal(market="KXBTC15M-A", qty=1))
    assert any(p.startswith("/portfolio") for _, p, _ in st.sim.requests)   # the sim asserts signatures


def test_unknown_fee_type_is_refused(st):
    st.ready()
    st.sim.series["KXBTC15M"] = {"tags": [], "category": "Crypto", "fee_type": "flat", "fee_multiplier": 1}
    r = st.buy(st.proposal(market="KXBTC15M-B", qty=1))
    assert r.status == "rejected" and "cannot be computed exactly" in r.reasons[0]
    assert posts(st) == []
