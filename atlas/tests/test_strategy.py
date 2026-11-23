"""Strategy framework: look-ahead guard and the crypto flip-favorite strategy."""
import math

import pytest

from atlas.money import Money
from atlas.strategies.base import LookAheadError, MarketView, NoTrade, Series, Signal
from atlas.strategies.crypto_flip import CryptoFlipFavorite, FlipConfig

M = Money.parse
OPEN = 1_795_400_000.0


def quotes(points):
    yes = Series([(t, (M(b), M(a), 100)) for t, b, a in points])
    no = Series([(t, (M("1") - M(a), M("1") - M(b), 100)) for t, b, a in points])
    return {"yes": yes, "no": no}


def reference(start_price="60000", drift=0.0, wiggle=12.0, minutes=90, end=OPEN + 12 * 60):
    pts, p = [], float(start_price)
    for i in range(minutes):
        t = end - (minutes - 1 - i) * 60
        p = p + drift + (wiggle if i % 2 else -wiggle)
        pts.append((t, M(f"{p:.2f}")))
    return Series(pts)


def view(now_min=12.0, strike="60000", ref=None, q=None):
    now = OPEN + now_min * 60
    q = q or quotes([(OPEN + 60 * m, "0.90", "0.91") for m in range(15)])
    ref = ref or reference("60400", end=now)
    return MarketView(now=now, market="KXBTC15M-T", event="KXBTC15M-E", module="kalshi", open_ts=OPEN,
                      close_ts=OPEN + 900, strike=M(strike), fee_multiplier_pct=100,
                      quotes={k: s.cut(now) for k, s in q.items()}, reference=ref.cut(now))


def test_series_refuses_the_future():
    s = Series([(1.0, "a"), (2.0, "b"), (3.0, "c")]).cut(2.0)
    assert s.at(2.0) == (2.0, "b")
    with pytest.raises(LookAheadError):
        s.at(2.5)
    with pytest.raises(LookAheadError):
        s.window(0, 3.0)


def test_strategy_cannot_peek_even_if_it_tries():
    v = view()
    with pytest.raises(LookAheadError):
        v.reference.at(v.now + 1)


def test_buys_a_safe_favorite_within_the_purchase_limit():
    s = CryptoFlipFavorite()
    r = s.evaluate(view())
    assert isinstance(r, Signal), getattr(r, "reasons", None)
    assert r.side == "yes" and r.limit_price == M("0.91")
    assert r.qty * r.limit_price + r.fee <= Money.dollars(2)
    assert r.expected_value >= M("0.01") and r.prob_bp > 9100
    assert r.model_version.startswith("crypto_flip_favorite@")


def test_outside_the_entry_window_is_no_trade():
    r = CryptoFlipFavorite().evaluate(view(now_min=5))
    assert isinstance(r, NoTrade) and "minutes 10-14" in r.reasons[0]


def test_too_close_to_strike_is_no_trade():
    r = CryptoFlipFavorite().evaluate(view(ref=reference("60010", end=OPEN + 720)))
    assert isinstance(r, NoTrade) and any("standard deviations" in x for x in r.reasons)


def test_flip_under_way_is_no_trade():
    pts = [(OPEN + 60 * m, "0.97", "0.98") if m < 11 else (OPEN + 60 * m, "0.89", "0.90") for m in range(15)]
    r = CryptoFlipFavorite().evaluate(view(q=quotes(pts)))
    assert isinstance(r, NoTrade) and any("flip under way" in x for x in r.reasons)


def test_price_outside_range_and_wide_spread_are_reported():
    r = CryptoFlipFavorite().evaluate(view(q=quotes([(OPEN + 60 * m, "0.93", "0.99") for m in range(15)])))
    assert isinstance(r, NoTrade)
    assert any("outside" in x for x in r.reasons) and any("Spread" in x for x in r.reasons)


def test_no_side_favorite_when_btc_is_below_strike():
    q = quotes([(OPEN + 60 * m, "0.08", "0.09") for m in range(15)])      # NO is the favorite at 0.92
    r = CryptoFlipFavorite().evaluate(view(strike="60400", ref=reference("60000", end=OPEN + 720), q=q))
    assert isinstance(r, Signal) and r.side == "no" and r.limit_price == M("0.92")


def test_stale_reference_is_no_trade():
    r = CryptoFlipFavorite().evaluate(view(ref=reference("60400", end=OPEN + 600)))
    assert isinstance(r, NoTrade) and any("stale" in x for x in r.reasons)


def test_config_change_is_a_new_model_version():
    a, b = CryptoFlipFavorite(), CryptoFlipFavorite(FlipConfig(z_min="2.5"))
    assert a.version != b.version
