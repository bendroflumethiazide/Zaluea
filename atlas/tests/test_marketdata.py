"""BTC reference price: freshness, disagreement and source failures."""
from atlas.marketdata.btc import BtcReference, PricePoint
from atlas.money import Money

NOW = 1_795_400_000.0


def src(name, price, age=1.0, ts=True, fail=False):
    def f(clock):
        if fail:
            raise ConnectionError("down")
        return PricePoint(name, Money.parse(price), (NOW - age) if ts else None, NOW)
    f.__name__ = name
    return f


def ref(*sources, **kw):
    return BtcReference(sources, clock=lambda: NOW, **kw).get()


def test_median_of_agreeing_sources():
    r = ref(src("a", "60744.91"), src("b", "60750.00"), src("c", "60740.10"))
    assert r.ok and r.price == Money.parse("60744.91") and len(r.points) == 3


def test_even_number_of_sources_averages_the_middle_two():
    r = ref(src("a", "100.00"), src("b", "100.02"))
    assert r.ok and r.price == Money.parse("100.01")


def test_stale_sources_are_ignored_and_too_few_means_no_price():
    r = ref(src("a", "60744.91"), src("b", "60745.00", age=60), src("c", "60745", fail=True))
    assert not r.ok and r.price is None and "stale" in r.reason and "unavailable" in r.reason


def test_contradictory_sources_mean_no_price():
    r = ref(src("a", "60000.00"), src("b", "60600.00"))           # 1% apart
    assert not r.ok and "disagree" in r.reason


def test_source_without_timestamp_uses_receipt_time():
    r = ref(src("a", "60744.91"), src("kraken", "60745.20", ts=False))
    assert r.ok and any(p.source_ts is None for p in r.points)


def test_every_point_has_source_and_time():
    r = ref(src("a", "1.00"), src("b", "1.00"))
    assert all(p.source and p.received_ts for p in r.points)
