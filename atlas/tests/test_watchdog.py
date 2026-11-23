"""Part 25: watchdog tests. Uses real child processes for crash/freeze cases."""
import ast
import subprocess
import sys
import time
from pathlib import Path

import pytest

from atlas.watchdog import CancelOnlyExchange, Watchdog

from .harness import build

ROOT = Path(__file__).resolve().parents[1]
BEAT = """
import sys, time
sys.path.insert(0, {root!r})
from atlas.health import HealthBoard
hb = HealthBoard({health!r}, clock=lambda: {ts!r})
hb.beat('gateway', {{'reconciled_ok': True, 'reconciled_at': {ts!r}}})
print('beat', flush=True)
time.sleep({sleep})
"""


@pytest.fixture
def st(tmp_path):
    s = build(tmp_path)
    yield s
    s.gw.stop()


def make_wd(st):
    wd = Watchdog(st.paths, exchange=st.ex, clock=st.now)
    wd.start()
    return wd


def healthy(st):
    st.gw.heartbeat()                       # gateway, primary risk and shadow risk report in


def lock_kinds(st):
    return {l["kind"] for l in st.gw.locks.active()}


def test_healthy_system_has_no_findings(st):
    healthy(st)
    wd = make_wd(st)
    assert wd.check_once() == []
    assert lock_kinds(st) == set()
    wd.stop()


def child_beats_as_gateway(st, sleep):
    p = subprocess.Popen([sys.executable, "-c", BEAT.format(root=str(ROOT), health=str(st.paths.health),
                                                            ts=st.now(), sleep=sleep)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "beat"
    return p


def test_detects_crashed_main_process(st):
    healthy(st)
    st.gw.control.set_module("kalshi", True)
    st.gw.control.authorize_today(st.day, confirm="AUTHORIZE")
    child = child_beats_as_gateway(st, sleep=0)
    child.wait(10)                                             # the "gateway" process has died
    st.advance(20)
    st.shadow.heartbeat()
    st.gw.health.beat("primary_risk", {"ok": True})
    wd = make_wd(st)
    findings = wd.check_once()
    assert any("crashed" in f.detail for f in findings)
    assert "watchdog" in lock_kinds(st)
    assert not st.gw.control.is_authorized(st.day)             # authorization revoked
    wd.stop()


def test_detects_frozen_main_process(st):
    healthy(st)
    child = child_beats_as_gateway(st, sleep=60)               # alive, but stops reporting
    try:
        st.advance(20)
        st.shadow.heartbeat()
        st.gw.health.beat("primary_risk", {"ok": True})
        wd = make_wd(st)
        findings = wd.check_once()
        assert any("frozen" in f.detail for f in findings)
        assert "watchdog" in lock_kinds(st)
        wd.stop()
    finally:
        child.kill()


def test_detects_risk_manager_failure(st):
    healthy(st)
    st.advance(20)
    st.gw.health.beat("gateway", {"reconciled_ok": True, "reconciled_at": st.now()})
    st.gw.health.beat("primary_risk", {"ok": True})            # shadow risk manager went silent
    wd = make_wd(st)
    assert any("shadow_risk" in f.detail for f in wd.check_once())
    wd.stop()


def test_detects_database_failure(st):
    healthy(st)
    st.paths.ledger.rename(st.paths.ledger.with_suffix(".moved"))
    wd = make_wd(st)
    findings = wd.check_once()
    assert any(f.check in ("database", "ledger") for f in findings)
    assert "integrity" in lock_kinds(st)
    wd.stop()


def test_watchdog_lock_disables_new_orders(st):
    st.ready()
    healthy(st)
    st.paths.ledger.rename(st.paths.ledger.with_suffix(".moved"))
    wd = make_wd(st)
    wd.check_once()
    st.paths.ledger.with_suffix(".moved").rename(st.paths.ledger)
    r = st.buy(st.proposal())
    assert r.status == "rejected" and "locked" in r.reasons[0]
    assert st.ex.orders == {}
    wd.stop()


def test_does_not_re_enable_trading_after_recovery(st):
    healthy(st)
    st.gw.control.authorize_today(st.day, confirm="AUTHORIZE")
    st.advance(20)                                             # everything goes quiet
    wd = make_wd(st)
    assert wd.check_once()
    healthy(st)                                                # system recovers
    st.watchdog_ok()
    assert wd.check_once() == []
    assert "watchdog" in lock_kinds(st)                        # the lock stays until the owner clears it
    assert not st.gw.control.is_authorized(st.day)
    wd.stop()


def test_cancels_outstanding_entry_orders(st):
    st.ready()
    healthy(st)
    from atlas.ledger import ReservationRequest
    from atlas.money import Money
    coid = st.gw.ledger.reserve(ReservationRequest("kalshi", "BTC-B", "BTC-1300", "s", "yes", 1,
                                                   Money.parse("0.50"), Money.parse("0.52")),
                                st.gw.limits.current(st.day), st.day)
    st.ex.place_order(coid, "BTC-B", "yes", "buy", 1, "0.5000", tif="gtc")   # rests below the ask
    st.gw.ledger.set_status(coid, "open")
    st.advance(20)
    wd = make_wd(st)
    wd.check_once()
    assert st.ex.orders[coid].status == "cancelled"
    assert st.ex.positions() == []                             # positions untouched (none were held)
    wd.stop()


def test_detects_audit_tampering(st):
    healthy(st)
    wd = make_wd(st)
    assert wd.check_once() == []
    st.gw.audit.con.execute("DROP TRIGGER events_no_update")
    st.gw.audit.con.execute("UPDATE events SET reason='forged' WHERE seq=2")
    st.advance(61)
    healthy(st)
    findings = wd.check_once()
    assert any(f.check == "audit" for f in findings) and "audit" in lock_kinds(st)
    wd.stop()


def test_gateway_fails_closed_without_a_healthy_watchdog(st):
    st.ready()
    r = st.gw.submit_entry(st.proposal(), st.approve(st.proposal()))   # no watchdog heartbeat at all
    assert r.status == "rejected" and "Watchdog" in r.reasons[0]
    st.gw.health.beat("watchdog", {"ok": True})
    st.advance(20)
    st.gw.clock_guard.check()
    p = st.proposal()
    r = st.gw.submit_entry(p, st.approve(p))                    # watchdog heartbeat went stale
    assert r.status == "rejected" and "stale" in r.reasons[0]
    assert st.ex.orders == {}


def test_runs_as_a_separate_process_without_the_dashboard(st):
    st.gw.stop()                                               # main application is not running at all
    from atlas.health import HealthBoard
    old = HealthBoard(st.paths.health, clock=lambda: time.time() - 60)   # last heard from a minute ago
    for comp in ("gateway", "primary_risk", "shadow_risk"):
        old.beat(comp, {"reconciled_ok": True, "reconciled_at": time.time() - 60})
    out = subprocess.run([sys.executable, "-m", "atlas.watchdog", "--data", str(st.paths.root), "--once"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 2 and "TRADING DISABLED" in out.stdout and "heartbeat" in out.stdout
    assert "watchdog" in lock_kinds(st)
    st.gw.lock.acquire()                                       # let the fixture's stop() succeed


def test_heartbeat_from_the_future_is_a_clock_problem(st):
    healthy(st)
    st.advance(-30)                                            # watchdog's clock is behind the others
    wd = make_wd(st)
    assert any(f.check == "clock" for f in wd.check_once()) and "clock" in lock_kinds(st)
    wd.stop()


def test_watchdog_can_only_cancel():
    assert not hasattr(CancelOnlyExchange(object()), "place_order")
    src = (ROOT / "atlas" / "watchdog.py").read_text()
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("place_order", "authorize_today", "change", "clear", "grant_approval", "set_module"):
        assert forbidden not in called, forbidden
