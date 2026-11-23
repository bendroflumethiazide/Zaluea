"""Part 25: authorization tests, limit-change rules and locks."""
import pytest

from atlas.audit import AuditLog
from atlas.control import (APPROVAL_SECONDS, ControlError, ControlStore, LockStore, order_fingerprint,
                           trading_locked)
from atlas.limits import ABSOLUTE_CEILINGS, DEFAULTS, LimitError, LimitStore
from atlas.money import Money
from atlas.storage import StorageError

DAY, NEXT = "2026-11-23", "2026-11-24"


@pytest.fixture
def env(tmp_path):
    t = [1_000_000.0]
    audit = AuditLog(tmp_path / "audit.db", create=True)
    ctl = ControlStore(tmp_path / "control.db", audit, create=True, clock=lambda: t[0])
    locks = LockStore(tmp_path / "locks.db", audit, create=True, clock=lambda: t[0])
    limits = LimitStore(tmp_path / "control.db", audit, create=True)
    ctl.boot()
    return dict(t=t, audit=audit, ctl=ctl, locks=locks, limits=limits, tmp=tmp_path)


# ---------------- limits ----------------
def test_spec_defaults():
    assert DEFAULTS.daily_spend == Money.dollars(20)
    assert DEFAULTS.max_order == Money.dollars(2)
    assert DEFAULTS.max_market_exposure == Money.dollars(3)
    assert DEFAULTS.max_daily_loss == Money.dollars(3)
    assert (DEFAULTS.max_positions, DEFAULTS.max_open_orders, DEFAULTS.max_consec_losses) == (3, 3, 3)


def test_lowering_is_immediate_raising_waits_for_tomorrow(env):
    L = env["limits"]
    assert L.change(DAY, NEXT, {"max_order": Money.dollars(1)}) == {"max_order": "now"}
    assert L.current(DAY).max_order == Money.dollars(1)
    with pytest.raises(LimitError):
        L.change(DAY, NEXT, {"daily_spend": Money.dollars(21)})               # no confirmation
    assert L.change(DAY, NEXT, {"daily_spend": Money.dollars(21)}, confirm="RAISE LIMITS") == {"daily_spend": "tomorrow"}
    assert L.current(DAY).daily_spend == Money.dollars(20)                     # $20 cannot become $21 today
    assert L.current(NEXT).daily_spend == Money.dollars(21)


def test_ceilings_and_bad_values_rejected(env):
    L = env["limits"]
    with pytest.raises(LimitError):
        L.change(DAY, NEXT, {"daily_spend": ABSOLUTE_CEILINGS.daily_spend + Money(1)}, confirm="RAISE LIMITS")
    for bad in ({"max_order": 2.0}, {"max_positions": Money(3)}, {"max_order": Money(0)}, {"nope": 1}):
        with pytest.raises(LimitError):
            L.change(DAY, NEXT, bad, confirm="RAISE LIMITS")
    env["audit"].verify()


def test_limit_changes_are_audited(env):
    env["limits"].change(DAY, NEXT, {"max_positions": 2})
    cats = [e["category"] for e in env["audit"].export()]
    assert "config_change" in cats


# ---------------- authorization ----------------
def test_authorization_default_off_and_requires_confirmation(env):
    ctl = env["ctl"]
    assert not ctl.is_authorized(DAY)
    with pytest.raises(ControlError):
        ctl.authorize_today(DAY, confirm="yes")
    with pytest.raises(ControlError):
        ctl.authorize_today(None, confirm="AUTHORIZE")                        # clock unverified
    ctl.authorize_today(DAY, confirm="AUTHORIZE")
    assert ctl.is_authorized(DAY)


def test_authorization_expires_at_end_of_day(env):
    ctl = env["ctl"]
    ctl.authorize_today(DAY, confirm="AUTHORIZE")
    assert not ctl.is_authorized(NEXT)
    assert not ctl.is_authorized(None)                                         # unverified clock


def test_restart_does_not_reauthorize(env):
    ctl, tmp = env["ctl"], env["tmp"]
    ctl.authorize_today(DAY, confirm="AUTHORIZE")
    ctl.set_require_approval(False, confirm="AUTONOMOUS")
    again = ControlStore(tmp / "control.db", env["audit"])                     # process restart
    assert not again.is_authorized(DAY)                                        # not booted yet
    again.boot()
    assert not again.is_authorized(DAY)
    assert again.require_approval()                                            # forced back ON


def test_require_approval_default_on_and_turning_off_needs_confirmation(env):
    ctl = env["ctl"]
    assert ctl.require_approval()
    with pytest.raises(ControlError):
        ctl.set_require_approval(False, confirm="ok")
    ctl.set_require_approval(False, confirm="AUTONOMOUS")
    assert not ctl.require_approval()


def test_approval_bound_to_exact_order_single_use_and_expiring(env):
    ctl, t = env["ctl"], env["t"]
    fp = order_fingerprint("kalshi", "KXBTC15M-A", "yes", "buy", 2, Money.parse("0.88"), Money.parse("1.78"))
    changed = order_fingerprint("kalshi", "KXBTC15M-A", "yes", "buy", 2, Money.parse("0.89"), Money.parse("1.80"))
    aid = ctl.grant_approval(fp, "Buy 2 YES")
    assert ctl.consume_approval(aid, changed) == (False, "Order changed since it was approved; a new approval is needed")
    assert ctl.consume_approval(aid, fp)[0]
    assert not ctl.consume_approval(aid, fp)[0]                                # single use
    aid2 = ctl.grant_approval(fp, "Buy 2 YES")
    t[0] += APPROVAL_SECONDS + 1
    assert ctl.consume_approval(aid2, fp) == (False, "Approval expired")
    assert not ctl.consume_approval(None, fp)[0]


def test_restart_voids_pending_approvals(env):
    ctl = env["ctl"]
    fp = order_fingerprint("kalshi", "M", "yes", "buy", 1, Money.parse("0.5"), Money.parse("0.52"))
    aid = ctl.grant_approval(fp, "x")
    ctl.boot()
    assert not ctl.consume_approval(aid, fp)[0]


def test_modules_default_off(env):
    ctl = env["ctl"]
    assert not any(ctl.module_enabled(m) for m in ("kalshi", "esports", "stocks", "etfs"))
    ctl.set_module("kalshi", True)
    assert ctl.module_enabled("kalshi") and not ctl.module_enabled("stocks")
    assert not ctl.module_enabled("options")


# ---------------- locks ----------------
def test_locks_block_and_need_manual_clear(env):
    locks = env["locks"]
    assert trading_locked(locks) == (False, "")
    locks.set_lock("emergency_stop", "owner pressed STOP", "dashboard")
    assert trading_locked(locks)[0]
    with pytest.raises(ControlError):
        locks.clear("emergency_stop", confirm="ok", reconciled_ok=True)
    locks.clear("emergency_stop", confirm="RESUME", reconciled_ok=True)
    assert not trading_locked(locks)[0]


def test_integrity_locks_need_reconciliation(env):
    locks = env["locks"]
    locks.set_lock("watchdog", "gateway heartbeat stale", "watchdog")
    with pytest.raises(ControlError):
        locks.clear("watchdog", confirm="RESUME", reconciled_ok=False)
    locks.clear("watchdog", confirm="RESUME", reconciled_ok=True)
    assert not trading_locked(locks)[0]


def test_unreadable_lock_store_means_locked(env):
    locks = env["locks"]
    locks.con.close()
    locked, why = trading_locked(locks)
    assert locked and "Cannot read" in why


def test_missing_control_db_fails(tmp_path):
    with pytest.raises(StorageError):
        ControlStore(tmp_path / "missing.db", AuditLog(tmp_path / "a.db", create=True))
