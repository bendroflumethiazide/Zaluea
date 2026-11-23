"""Part 25: spending tests (and daily loss / consecutive loss accounting)."""
import multiprocessing as mp
import sqlite3
from dataclasses import replace

import pytest

from atlas.audit import AuditLog
from atlas.ledger import BudgetError, Ledger, LedgerIntegrityError, ReservationRequest
from atlas.limits import DEFAULTS
from atlas.money import Money, kalshi_taker_fee
from atlas.storage import StorageError

DAY = "2026-11-23"
M = Money.parse
# Roomy limits so budget tests are only limited by spending caps.
ROOMY = replace(DEFAULTS, max_positions=50, max_open_orders=50, max_total_exposure=Money.dollars(100),
                max_event_exposure=Money.dollars(100), max_strategy_exposure=Money.dollars(100),
                max_market_exposure=Money.dollars(25))


def req(market, cost="2.00", module="kalshi", qty=2, price="0.90", event=None, strategy="s1", side="yes"):
    return ReservationRequest(module=module, market=market, event_group=event or market, strategy=strategy,
                              side=side, qty=qty, limit_price=M(price), max_cost=M(cost))


@pytest.fixture
def led(tmp_path):
    audit = AuditLog(tmp_path / "audit.db", create=True)
    return Ledger(tmp_path / "ledger.db", audit, create=True)


def test_daily_cap_cannot_become_21(led):
    for i in range(5):
        led.reserve(req(f"K{i}"), ROOMY, DAY)                       # Kalshi $10 (its ceiling)
    for i in range(5):
        led.reserve(req(f"S{i}", module="stocks"), ROOMY, DAY)      # Stocks $10 -> $20 total
    assert led.snapshot(DAY).spent == Money.dollars(20)
    with pytest.raises(BudgetError) as e:
        led.reserve(req("E0", cost="0.01", qty=1, price="0.01", module="etfs"), ROOMY, DAY)
    assert "spending limit" in e.value.reason
    assert led.snapshot(DAY).spent == Money.dollars(20)


def test_modules_share_one_global_budget_and_category_ceilings(led):
    lim = replace(ROOMY, cat_kalshi=Money.dollars(8), cat_stocks=Money.dollars(7), cat_etfs=Money.dollars(10))
    for i in range(4):
        led.reserve(req(f"K{i}"), lim, DAY)                         # $8 Kalshi
    with pytest.raises(BudgetError) as e:
        led.reserve(req("K9", module="esports"), lim, DAY)          # E-Sports uses the Kalshi ceiling
    assert "kalshi category" in e.value.reason
    for i in range(3):
        led.reserve(req(f"S{i}", module="stocks"), lim, DAY)        # $6 stocks
    led.reserve(req("S9", cost="1.00", qty=1, price="0.99", module="stocks"), lim, DAY)  # $7 stocks
    led.reserve(req("E1", cost="2.00", module="etfs"), lim, DAY)    # $17
    led.reserve(req("E2", cost="2.00", module="etfs"), lim, DAY)    # $19
    with pytest.raises(BudgetError):
        led.reserve(req("E3", cost="1.01", qty=1, price="1.00", module="etfs"), lim, DAY)  # would be $20.01
    led.reserve(req("E4", cost="1.00", qty=1, price="1.00", module="etfs"), lim, DAY)      # exactly $20
    assert led.snapshot(DAY).spent == Money.dollars(20)


def test_per_purchase_limit_includes_fees(led):
    with pytest.raises(BudgetError) as e:
        led.reserve(req("K", cost="2.01", qty=2, price="0.99"), DEFAULTS, DAY)
    assert "per-purchase" in e.value.reason


def test_fees_count_against_budget(led):
    coid = led.reserve(req("K1", cost="1.79", qty=2, price="0.88"), ROOMY, DAY)
    fee = kalshi_taker_fee(2, M("0.88"))
    led.record_execution("f1", coid, "buy", 2, M("0.88"), fee, DAY)
    s = led.snapshot(DAY)
    assert s.purchases == M("1.76") and s.fees == fee and s.spent == M("1.76") + fee


def test_open_orders_reserve_capital_and_unknown_keeps_reservation(led):
    coid = led.reserve(req("K1", cost="1.80"), ROOMY, DAY)
    led.set_status(coid, "submitted")
    led.set_status(coid, "unknown", note="timeout")                 # outcome unknown: keep the money reserved
    assert led.snapshot(DAY).reserved == M("1.80")
    assert led.snapshot(DAY).open_entry_orders == 1


def test_cancel_releases_only_after_confirmation(led):
    coid = led.reserve(req("K1", cost="1.80"), ROOMY, DAY)
    led.set_status(coid, "cancel_requested")
    assert led.snapshot(DAY).spent == M("1.80")                     # still reserved
    led.finalize(coid, "cancelled", note="exchange confirmed")
    assert led.snapshot(DAY).spent == Money.ZERO


def test_partial_fill_counts_actual_execution(led):
    coid = led.reserve(req("K1", cost="1.82", qty=2, price="0.90"), ROOMY, DAY)
    led.record_execution("f1", coid, "buy", 1, M("0.89"), Money.cents(1), DAY)
    s = led.snapshot(DAY)
    assert s.spent == M("1.82")                                     # 0.90 filled + 0.92 still reserved
    led.finalize(coid, "cancelled", note="rest cancelled")
    s = led.snapshot(DAY)
    assert s.spent == M("0.90") and s.purchases == M("0.89") and s.fees == Money.cents(1)
    assert s.positions[0].qty == 1


def test_profits_do_not_replenish_budget(led):
    coid = led.reserve(req("K1", cost="1.80", qty=2, price="0.89"), ROOMY, DAY)
    led.record_execution("f1", coid, "buy", 2, M("0.89"), Money.cents(1), DAY)
    before = led.snapshot(DAY).spent
    led.record_execution("settle1", None, "settle", 2, M("1.00"), Money.ZERO, DAY,
                         module="kalshi", market="K1", side="yes", event_group="K1", strategy="s1")
    s = led.snapshot(DAY)
    assert s.realized_today == M("0.21") and s.spent == before
    exit_id = led.reserve_exit("kalshi", "K1", "yes", 1, M("0.50"), DAY, "s1", "K1") if s.positions else None
    assert exit_id is None                                          # nothing left to sell


def test_restart_keeps_spending(tmp_path):
    audit = AuditLog(tmp_path / "audit.db", create=True)
    led = Ledger(tmp_path / "ledger.db", audit, create=True)
    led.reserve(req("K1", cost="2.00"), ROOMY, DAY)
    led.con.close()
    again = Ledger(tmp_path / "ledger.db", audit)
    assert again.snapshot(DAY).spent == Money.dollars(2)


def test_failed_accounting_blocks_orders(tmp_path):
    audit = AuditLog(tmp_path / "audit.db", create=True)
    with pytest.raises(StorageError):
        Ledger(tmp_path / "missing.db", audit)                      # missing ledger: no fresh $0 start
    sqlite3.connect(tmp_path / "foreign.db").execute("CREATE TABLE meta (key, value)")
    with pytest.raises(StorageError):
        Ledger(tmp_path / "foreign.db", audit)
    Ledger(tmp_path / "ledger.db", audit, create=True)
    with pytest.raises(StorageError):
        Ledger(tmp_path / "ledger.db", audit, create=True)          # never overwrite a real ledger


def test_daily_loss_and_consecutive_losses(led):
    for i, (price, payout) in enumerate([("0.90", "0.00"), ("0.90", "0.00"), ("0.90", "1.00"), ("0.90", "0.00")]):
        coid = led.reserve(req(f"K{i}", cost="0.91", qty=1, price=price), ROOMY, DAY)
        led.record_execution(f"b{i}", coid, "buy", 1, M(price), Money.cents(1), DAY)
        led.record_execution(f"s{i}", None, "settle", 1, M(payout), Money.ZERO, DAY,
                             module="kalshi", market=f"K{i}", side="yes", event_group=f"K{i}", strategy="s1")
    s = led.snapshot(DAY)
    assert s.realized_today == M("-0.91") * 3 + M("0.09")
    assert s.consecutive_losses == 1                                # win in position 3 breaks the streak


def test_fill_beyond_reservation_is_an_integrity_error(led):
    coid = led.reserve(req("K1", cost="1.00", qty=1, price="0.95"), ROOMY, DAY)
    with pytest.raises(LedgerIntegrityError):
        led.record_execution("f1", coid, "buy", 1, M("0.99"), Money.cents(2), DAY)
    with pytest.raises(LedgerIntegrityError):
        led.record_execution("f2", coid, "buy", 2, M("0.40"), Money.ZERO, DAY)
    assert led.integrity_check()[0]                                 # rejected fills left no trace


def test_duplicate_fill_is_idempotent(led):
    coid = led.reserve(req("K1", cost="1.00", qty=1, price="0.95"), ROOMY, DAY)
    assert led.record_execution("f1", coid, "buy", 1, M("0.95"), Money.cents(1), DAY)
    assert not led.record_execution("f1", coid, "buy", 1, M("0.95"), Money.cents(1), DAY)
    assert led.snapshot(DAY).positions[0].qty == 1


def test_never_both_sides_of_one_market(led):
    coid = led.reserve(req("K1", cost="0.91", qty=1, price="0.90"), ROOMY, DAY)
    with pytest.raises(BudgetError) as e:
        led.reserve(req("K1", cost="0.20", qty=1, price="0.10", side="no"), ROOMY, DAY)
    assert "opposite side" in e.value.reason
    led.record_execution("f1", coid, "buy", 1, M("0.90"), Money.cents(1), DAY)
    with pytest.raises(BudgetError):
        led.reserve(req("K1", cost="0.20", qty=1, price="0.10", side="no"), ROOMY, DAY)


def test_one_order_in_flight_per_market(led):
    led.reserve(req("K1", cost="1.00", qty=1, price="0.95"), ROOMY, DAY)
    with pytest.raises(BudgetError) as e:
        led.reserve(req("K1", cost="1.00", qty=1, price="0.95"), ROOMY, DAY)
    assert "already in flight" in e.value.reason


def test_default_position_and_order_limits(led):
    for i in range(3):
        led.reserve(req(f"K{i}", cost="1.00", qty=1, price="0.95"), DEFAULTS, DAY)
    with pytest.raises(BudgetError) as e:
        led.reserve(req("K9", cost="1.00", qty=1, price="0.95"), DEFAULTS, DAY)
    assert "open entry orders" in e.value.reason and "positions" in e.value.reason


def test_market_and_event_exposure_limits(led):
    lim = replace(ROOMY, max_market_exposure=Money.dollars(3), max_event_exposure=Money.dollars(3))
    led.reserve(req("K1", cost="2.00", event="BTC-1245"), lim, DAY)
    with pytest.raises(BudgetError) as e:                            # same event, different market
        led.reserve(req("K2", cost="1.50", qty=1, price="0.90", event="BTC-1245", side="no"), lim, DAY)
    assert "event group" in e.value.reason and "this market" not in e.value.reason
    led.reserve(req("K2", cost="1.00", qty=1, price="0.90", event="BTC-1245", side="no"), lim, DAY)  # $3 exactly


# ---------------- concurrency across processes ----------------
def _hammer(path, audit_path, worker, out):
    audit = AuditLog(audit_path)
    led = Ledger(path, audit)
    ok = 0
    for i in range(10):
        try:
            led.reserve(req(f"W{worker}-{i}", cost="1.50", qty=1, price="1.00"), ROOMY, DAY)
            ok += 1
        except BudgetError:
            pass
    out.put(ok)


def test_simultaneous_processes_cannot_bypass_the_cap(tmp_path):
    audit = AuditLog(tmp_path / "audit.db", create=True)
    Ledger(tmp_path / "ledger.db", audit, create=True)
    lim_path = tmp_path / "ledger.db"
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_hammer, args=(lim_path, tmp_path / "audit.db", w, q)) for w in range(8)]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    total_ok = sum(q.get() for _ in procs)
    spent = Ledger(lim_path, audit).snapshot(DAY).spent
    # Kalshi ceiling is $10: at most 6 x $1.50 = $9.00 can ever be reserved.
    assert total_ok == 6 and spent == M("9.00")
    assert audit.verify().ok


def test_kalshi_rounding_fee_never_exceeds_the_reservation(led):
    """Kalshi docs (fee_rounding): trade fee rounded to $0.000001, then a rounding fee that can add
    just under a cent. Reservations include a $0.01 allowance, so this is not an integrity error."""
    from atlas.money import kalshi_worst_case_fee
    price = M("0.975")                                              # a deci-cent price
    cost = price * 2 + kalshi_worst_case_fee(2, price)
    coid = led.reserve(req("K1", cost=cost.to_str(), qty=2, price="0.975"), ROOMY, DAY)
    charged = kalshi_taker_fee(2, price) + M("0.0099")              # model fee + worst rounding
    led.record_execution("f1", coid, "buy", 2, price, charged, DAY)
    assert led.integrity_check()[0]
