from fractions import Fraction

import pytest

from atlas.clock import ClockGuard, accounting_day
from atlas.money import Money, MoneyError, kalshi_taker_fee
from atlas.storage import InstanceLock, StorageError


def test_money_refuses_floats():
    for bad in (0.1, 1.0, float("nan")):
        with pytest.raises(MoneyError):
            Money(bad)
    with pytest.raises(MoneyError):
        Money.parse(1.25)
    with pytest.raises(MoneyError):
        Money.cents(2.0)
    with pytest.raises(MoneyError):
        Money(100) * 1.5
    with pytest.raises(MoneyError):
        Money(100) + 1


def test_money_parse_and_format_exact():
    assert Money.parse("0.9870").units == 9870
    assert Money.parse("$20").units == 200_000
    assert Money.parse("-1.5").to_str() == "-1.5000"
    assert str(Money.parse("1234.5")) == "$1,234.50"
    assert str(Money.parse("0.9875")) == "$0.9875"
    with pytest.raises(MoneyError):
        Money.parse("0.12345")
    # 0.1 + 0.2 is exact
    assert Money.parse("0.1") + Money.parse("0.2") == Money.parse("0.3")


def test_kalshi_fee_matches_exact_fraction_everywhere():
    for c in (1, 2, 3, 7, 50, 999):
        for p in range(0, 10_001, 37):
            exact = Fraction(7, 100) * c * Fraction(p, 10_000) * (1 - Fraction(p, 10_000))
            cents = -(-exact * 100 // 1)
            assert kalshi_taker_fee(c, Money(p)) == Money.cents(int(cents)), (c, p)


def test_kalshi_fee_known_values():
    assert kalshi_taker_fee(1, Money.parse("0.50")) == Money.cents(2)    # 1.75c -> 2c
    assert kalshi_taker_fee(100, Money.parse("0.50")) == Money.cents(175)
    assert kalshi_taker_fee(2, Money.parse("0.95")) == Money.cents(1)    # 0.665c -> 1c


def test_accounting_day_uses_new_york_time():
    # 2026-11-23 03:30 UTC is still 2026-11-22 in New York
    assert accounting_day(1795404600) == "2026-11-22"
    assert accounting_day(1795404600 + 2 * 3600) == "2026-11-23"


def test_clock_guard():
    t = [1_000_000.0]
    good = ClockGuard([lambda: t[0] + 0.5], local=lambda: t[0])
    assert good.check().verified and good.verified_day() is not None
    t[0] += 400  # verification goes stale
    assert good.verified_day() is None
    bad = ClockGuard([lambda: t[0] + 30], local=lambda: t[0])
    assert not bad.check().verified
    none = ClockGuard([lambda: (_ for _ in ()).throw(OSError())], local=lambda: t[0])
    assert not none.check().verified and none.verified_day() is None


def test_instance_lock_is_exclusive(tmp_path):
    a, b = InstanceLock(tmp_path / "g.lock"), InstanceLock(tmp_path / "g.lock")
    a.acquire()
    with pytest.raises(StorageError):
        b.acquire()
    a.release()
    b.acquire()
    b.release()
