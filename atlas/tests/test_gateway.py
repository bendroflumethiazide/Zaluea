"""Part 25: authorization, risk, shadow-risk and execution tests through the gateway."""
from dataclasses import replace

import pytest

from atlas.clock import ClockGuard
from atlas.gateway import Gateway
from atlas.money import Money
from atlas.risk import primary
from atlas.storage import StorageError

from .harness import build

M = Money.parse


@pytest.fixture
def st(tmp_path):
    s = build(tmp_path)
    yield s
    s.gw.stop()


def ex_orders(st):
    return list(st.ex.orders.values())


# ------------------------------------------------------------------ happy path
def test_approved_order_fills_and_is_accounted(st):
    st.ready()
    p = st.proposal(qty=2, limit="0.88")
    r = st.buy(p)
    assert r.status == "filled", r.reasons
    s = st.gw.ledger.snapshot(st.day)
    assert s.purchases == M("1.76") and s.fees == Money.cents(2) and s.spent == M("1.78")
    assert len(ex_orders(st)) == 1 and st.gw.reconcile()
    assert st.gw.audit.verify().ok


# ------------------------------------------------------------------ authorization
def test_no_authorization_blocks_entries(st):
    st.gw.control.set_module("kalshi", True)
    r = st.buy(st.proposal())
    assert r.status == "rejected" and any("authorized" in x for x in r.reasons)
    assert ex_orders(st) == []


def test_disabled_module_cannot_trade(st):
    st.ready(modules=("kalshi",))
    r = st.buy(st.proposal(market="CS2-A", module="esports", limit="0.88"))
    assert r.status == "rejected" and any("esports module disabled" in x for x in r.reasons)
    assert ex_orders(st) == []


def test_approval_required_and_bound_to_exact_order(st):
    st.ready()
    st.watchdog_ok()
    r = st.gw.submit_entry(st.proposal(), approval_id=None)
    assert r.status == "rejected" and "10 approval" in r.reasons[0]
    p = st.proposal(limit="0.88")
    aid = st.approve(p)
    changed = st.proposal(limit="0.89")                       # material change after approval
    r = st.gw.submit_entry(changed, approval_id=aid)
    assert r.status == "rejected" and "changed since it was approved" in r.reasons[0]
    assert ex_orders(st) == []


def test_restart_does_not_reauthorize_and_keeps_spending(st, tmp_path):
    st.ready()
    assert st.buy(st.proposal()).status == "filled"
    spent = st.gw.ledger.snapshot(st.day).spent
    st.gw.stop()
    gw2 = Gateway(st.paths, st.ex, ClockGuard([st.ex.server_time], local=st.now), st.shadow, clock=st.now)
    gw2.start()
    assert not gw2.control.is_authorized(gw2.verified_day())
    assert gw2.control.require_approval()
    assert gw2.ledger.snapshot(st.day).spent == spent
    st.gw = gw2


def test_second_gateway_cannot_run_at_the_same_time(st):
    other = Gateway(st.paths, st.ex, ClockGuard([st.ex.server_time], local=st.now), st.shadow, clock=st.now)
    with pytest.raises(StorageError):
        other.start()


def test_unverified_clock_blocks_entries(st):
    st.ready()
    st.gw.clock_guard.references = [lambda: st.now() + 60]     # exchange time disagrees by a minute
    st.gw.clock_guard.check()
    r = st.buy(st.proposal())
    assert r.status == "rejected" and "Clock not verified" in r.reasons[0]


# ------------------------------------------------------------------ hard limits
def test_max_purchase_is_two_dollars(st):
    st.ready()
    r = st.buy(st.proposal(qty=3, limit="0.88"))              # 2.64 + fee > $2
    assert r.status == "rejected" and any("max_order" in x for x in r.reasons)


def test_market_and_concurrent_position_limits(st):
    st.ready()
    for m in ("BTC-A", "BTC-B", "ETH-A"):
        assert st.buy(st.proposal(market=m, qty=1, limit="0.88")).status == "filled"
    st.gw.control.set_module("esports", True)
    r = st.buy(st.proposal(market="CS2-A", module="esports", qty=1, limit="0.88"))
    assert r.status == "rejected" and any("positions" in x for x in r.reasons)
    r = st.buy(st.proposal(market="BTC-A", qty=2, limit="0.88"))  # $0.89 held + $1.78 = $2.67 <= $3 -> ok
    assert r.status == "filled"
    r = st.buy(st.proposal(market="BTC-A", qty=1, limit="0.88"))  # would exceed $3 in BTC-A
    assert r.status == "rejected" and any("market_exposure" in x for x in r.reasons)


def test_daily_loss_limit_locks_and_needs_manual_resume(st):
    st.ready()
    for m in ("BTC-A", "ETH-A"):
        assert st.buy(st.proposal(market=m, qty=2, limit="0.88")).status == "filled"
        st.ex.settle(m, "no")                                  # each loses $1.78
    st.gw.reconcile()
    assert any(l["kind"] == "daily_loss" for l in st.gw.locks.active())
    r = st.buy(st.proposal(market="BTC-B", qty=1, limit="0.88"))
    assert r.status == "rejected" and "locked" in r.reasons[0]
    assert st.gw.ledger.snapshot(st.day).positions == []      # nothing was liquidated by the lock itself


def test_consecutive_losses_pause_entries(st):
    st.ready()
    st.gw.limits.change(st.day, "2099-01-01", {"max_daily_loss": M("3.00")})
    for m in ("BTC-A", "BTC-B", "ETH-A"):
        assert st.buy(st.proposal(market=m, qty=1, limit="0.88")).status == "filled"
        st.ex.settle(m, "no")
        st.gw.reconcile()
    assert any(l["kind"] == "consecutive_losses" for l in st.gw.locks.active())


def test_emergency_stop_blocks_every_entry_and_revokes_authorization(st):
    st.ready()
    st.gw.emergency_stop("owner pressed STOP")
    assert not st.gw.control.is_authorized(st.day)
    r = st.buy(st.proposal())
    assert r.status == "rejected" and "STOP ALL NEW TRADING" in r.reasons[0]
    st.gw.clear_lock("emergency_stop", confirm="RESUME")
    r = st.buy(st.proposal())                                  # still not authorized after clearing
    assert r.status == "rejected" and any("authorized" in x for x in r.reasons)


def test_profits_do_not_refill_budget(st):
    st.ready()
    assert st.buy(st.proposal(qty=2, limit="0.88")).status == "filled"
    before = st.gw.ledger.snapshot(st.day).spent
    st.ex.settle("BTC-A", "yes")
    st.gw.reconcile()
    s = st.gw.ledger.snapshot(st.day)
    assert s.realized_today == M("0.22") and s.spent == before


# ------------------------------------------------------------------ duplicates and faults
def test_same_proposal_cannot_be_submitted_twice(st):
    st.ready()
    p = st.proposal(qty=1)
    assert st.buy(p).status == "filled"
    r = st.buy(p)
    assert r.status == "rejected" and "already processed" in r.reasons[0]
    assert len(ex_orders(st)) == 1


def test_timeout_after_accept_is_never_resubmitted(st):
    st.ready()
    st.ex.faults.append("timeout_after_accept")
    r = st.buy(st.proposal(qty=2))
    assert len(ex_orders(st)) == 1                             # placed exactly once
    assert r.status == "filled"                                # learned the truth by looking it up
    assert st.gw.ledger.snapshot(st.day).spent == M("1.78")


def test_timeout_before_accept_keeps_reservation_until_confirmed(st):
    st.ready()
    st.ex.faults.append("timeout_before_accept")
    r = st.buy(st.proposal(qty=2))
    assert r.status == "unknown" and ex_orders(st) == []
    assert st.gw.ledger.snapshot(st.day).reserved == M("1.78")  # money stays reserved while unknown
    st.advance(60)
    st.gw.reconcile()
    assert st.gw.ledger.snapshot(st.day).reserved == M("1.78")
    st.advance(70)                                             # > 120 s: exchange confirms no such order
    st.gw.clock_guard.check()
    st.gw.reconcile()
    assert st.gw.ledger.snapshot(st.day).spent == Money.ZERO
    assert ex_orders(st) == []                                 # and it was never retried


def test_server_error_after_accept_is_reconciled_not_retried(st):
    st.ready()
    st.ex.faults.append("error_after_accept")
    r = st.buy(st.proposal(qty=1))
    assert r.status == "filled" and len(ex_orders(st)) == 1


def test_partial_fill_accounting(st):
    st.ready()
    st.ex.faults.append("partial")
    r = st.buy(st.proposal(qty=2, limit="0.88"))
    assert r.status == "cancelled"                             # IOC remainder cancelled by exchange
    s = st.gw.ledger.snapshot(st.day)
    assert s.positions[0].qty == 1 and s.spent == M("0.89") and s.reserved == Money.ZERO


def test_exchange_down_rejects(st):
    st.ready()
    st.ex.down = True
    r = st.buy(st.proposal())
    assert r.status == "rejected" and "Exchange unavailable" in r.reasons[0]


def test_stale_quote_and_moved_market_rejected(st):
    st.ready()
    r = st.buy(st.proposal(quote_age=30))
    assert r.status == "rejected" and any("data_age" in x for x in r.reasons)
    p = st.proposal(limit="0.88")
    st.ex.set_book("BTC-A", "yes", "0.88", "0.90", 50)       # price jumps after the decision
    r = st.buy(p)
    assert r.status == "rejected"
    assert ex_orders(st) == []


def test_negative_expected_value_rejected(st):
    st.ready()
    r = st.buy(st.proposal(ev="-0.01"))
    assert r.status == "rejected" and any("expected_value" in x for x in r.reasons)


# ------------------------------------------------------------------ shadow risk manager
def test_shadow_blocks_order_the_primary_wrongly_approved(st, monkeypatch):
    st.ready()
    real = primary.evaluate
    monkeypatch.setattr(primary, "evaluate",
                        lambda p, ctx: replace(real(p, ctx), approved=True))   # simulated primary bug
    r = st.buy(st.proposal(qty=3, limit="0.88"))              # $2.66 > $2 limit
    assert r.status == "rejected" and "disagreed" in r.reasons[0]
    assert ex_orders(st) == []
    assert any(l["kind"] == "risk_disagreement" for l in st.gw.locks.active())


def test_shadow_detects_spending_discrepancy(st, monkeypatch):
    st.ready()
    assert st.buy(st.proposal(market="BTC-A", qty=1)).status == "filled"
    real = st.gw.ledger.snapshot
    monkeypatch.setattr(st.gw.ledger, "snapshot", lambda day: replace(real(day), spent=Money.ZERO))
    r = st.buy(st.proposal(market="ETH-A", qty=1))
    assert r.status == "rejected" and "SPEND_MISMATCH" in r.reasons[0]
    assert any(l["kind"] == "risk_disagreement" for l in st.gw.locks.active())


def test_shadow_detects_missing_authorization(st, monkeypatch):
    st.gw.control.set_module("kalshi", True)                   # NOT authorized in the database
    monkeypatch.setattr(st.gw.control, "is_authorized", lambda day: True)  # primary is fooled
    r = st.buy(st.proposal())
    assert r.status == "rejected" and "AUTH_MISMATCH" in r.reasons[0]
    assert ex_orders(st) == []


def test_shadow_detects_stale_budget_state(st):
    st.ready()
    order = {"module": "kalshi", "market": "BTC-A", "side": "yes", "qty": 1, "limit_price": "0.8800",
             "max_cost": "0.8900", "strategy": "s", "client_order_id": "c1", "fingerprint": "f"}
    view = {"day": st.day, "spent": "0.0000", "snapshot_ts": st.now() - 60, "authorized": True, "positions": []}
    d = st.shadow.review(order, view)
    assert not d.approved and any(c == "STALE_PRIMARY" for c, _ in d.problems)


def test_shadow_rejects_during_reconciliation_failure(st):
    st.ready()
    st.ex.positions_map[("BTC-B", "yes")] = [5, 4_400]          # activity ATLAS never ordered
    assert not st.gw.reconcile()
    assert any(l["kind"] == "reconciliation" for l in st.gw.locks.active())
    order = {"module": "kalshi", "market": "BTC-A", "side": "yes", "qty": 1, "limit_price": "0.8800",
             "max_cost": "0.8900", "strategy": "s", "client_order_id": "c2", "fingerprint": "f"}
    view = {"day": st.day, "spent": "0.0000", "snapshot_ts": st.now(), "authorized": True, "positions": []}
    d = st.shadow.review(order, view)
    codes = {c for c, _ in d.problems}
    assert not d.approved and {"POSITION_MISMATCH", "LOCKED"} <= codes


def test_forged_shadow_signature_is_rejected(st, monkeypatch):
    st.ready()
    real = st.shadow.review

    def forged(order, view):
        d = real(order, view)
        d.signature = d.signature[:-4] + ("AAAA" if not d.signature.endswith("AAAA") else "BBBB")
        return d
    monkeypatch.setattr(st.shadow, "review", forged)
    r = st.buy(st.proposal())
    assert r.status == "rejected" and ex_orders(st) == []


def test_every_decision_is_in_the_audit_log(st):
    st.ready()
    st.buy(st.proposal(qty=1))
    st.buy(st.proposal(qty=9))
    cats = [e["category"] for e in st.gw.audit.export()]
    for c in ("trade_proposal", "risk_decision", "shadow_risk_decision", "trade_approval", "budget_change",
              "order_submission", "fill", "trade_rejection", "reconciliation", "restart"):
        assert c in cats, c
    assert st.gw.audit.verify().ok
