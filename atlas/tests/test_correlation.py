"""Part 25: correlation tests."""
import math
import random

from atlas.analytics.correlation import CorrelationModel, Holding, rolling_correlation
from atlas.money import Money

from .harness import build

M = Money.parse


def walk(seed, n=120, base=None, beta=0.0):
    rnd, p, out = random.Random(seed), 100.0, []
    for i in range(n):
        shock = rnd.gauss(0, 0.01)
        mkt = base[i] if base else 0.0
        p *= math.exp(beta * mkt + shock)
        out.append(p)
    return out


def market_shocks(seed=1, n=120):
    rnd = random.Random(seed)
    return [rnd.gauss(0, 0.015) for _ in range(n)]


def test_detects_highly_correlated_stock_holdings():
    shocks = market_shocks()
    m = CorrelationModel(closes={"NVDA": walk(2, base=shocks, beta=1.5), "AMD": walk(3, base=shocks, beta=1.5),
                                 "XOM": walk(4)})
    assert rolling_correlation(m.closes["NVDA"], m.closes["AMD"]) > 0.6
    g = m.groups([Holding("stocks", s, s, M("1.00")) for s in ("NVDA", "AMD", "XOM")])
    together = [sorted(h.market for h in hs) for hs in g.values()]
    assert ["AMD", "NVDA"] in together and ["XOM"] in together


def test_detects_overlapping_etf_exposure():
    m = CorrelationModel(etf_holdings={"QQQ": {"NVDA": 8.9, "AAPL": 8.4, "MSFT": 8.0},
                                       "XLK": {"NVDA": 14.0, "AAPL": 13.0, "MSFT": 12.0}},
                         holdings_as_of="test fixture")
    assert m.etf_overlap("QQQ", "XLK") == 8.9 + 8.4 + 8.0
    assert m.etf_overlap("QQQ", "NVDA") == 8.9
    assert m.etf_overlap("UNKNOWN_ETF", "NVDA") is None


def test_concentration_across_modules_crypto_is_one_group():
    m = CorrelationModel()
    hs = [Holding("kalshi", "KXBTC15M-26NOV231200-00", "KXBTC15M-26NOV231200", M("1.78")),
          Holding("kalshi", "KXETH15M-26NOV231200-00", "KXETH15M-26NOV231200", M("1.78"))]
    ok, group, why = m.check(hs[1], hs[:1], Money.dollars(3))
    assert not ok and group == "crypto" and "KXBTC15M" in why and "KXETH15M" in why


def test_prevents_exceeding_the_configured_limit():
    m = CorrelationModel()
    a = Holding("esports", "KXCS2MAP-X-1-A", "CS2-MATCH-1", M("1.50"))
    b = Holding("esports", "KXCS2GAME-X-A", "CS2-MATCH-1", M("1.60"))
    assert m.check(b, [a], Money.dollars(4))[0]
    assert not m.check(b, [a], Money.dollars(3))[0]
    other = Holding("esports", "KXCS2GAME-Y-B", "CS2-MATCH-2", M("1.60"))
    assert m.check(other, [a], Money.dollars(2))[0]                  # a different match is a different group


def test_missing_data_is_handled_conservatively():
    m = CorrelationModel(closes={"AAPL": walk(5, n=10)})               # far too little history
    g = m.groups([Holding("stocks", "AAPL", "AAPL", M("1")), Holding("stocks", "XYZ", "XYZ", M("1")),
                  Holding("etfs", "SPY", "SPY", M("1"))])
    assert len(g) == 1 and list(g)[0] == "equity:us_equity"


def test_sector_exposure_report():
    m = CorrelationModel()
    s = m.sector_exposure([Holding("stocks", "NVDA", "NVDA", M("1.00")), Holding("stocks", "MSFT", "MSFT", M("0.50")),
                           Holding("etfs", "SPY", "SPY", M("2.00")), Holding("stocks", "ZZZZ", "ZZZZ", M("0.25"))])
    assert s == {"Information Technology": M("1.50"), "Broad market": M("2.00"), "Unknown": M("0.25")}


def test_gateway_rejects_a_trade_that_overloads_a_correlated_group(tmp_path):
    st = build(tmp_path)
    st.ex.add_market("KXBTC15M-26NOV231200-00", "KXBTC15M-26NOV231200")
    st.ex.add_market("KXETH15M-26NOV231200-00", "KXETH15M-26NOV231200")
    st.ready()
    st.gw.limits.change(st.day, "2099-01-01", {"max_correlated_exposure": Money.dollars(3)})
    r1 = st.buy(st.proposal(market="KXBTC15M-26NOV231200-00", qty=2, limit="0.88"))
    assert r1.status == "filled", r1.reasons
    r2 = st.buy(st.proposal(market="KXETH15M-26NOV231200-00", qty=2, limit="0.88"))
    assert r2.status == "rejected" and "6 correlation" in r2.reasons[0] and "crypto" in r2.reasons[0]
    st.gw.stop()


def test_new_limit_is_added_to_older_data_folders(tmp_path):
    from atlas.audit import AuditLog
    from atlas.limits import DEFAULTS, LimitStore
    audit = AuditLog(tmp_path / "a.db", create=True)
    store = LimitStore(tmp_path / "c.db", audit, create=True)
    store.con.execute("DELETE FROM limit_changes WHERE field='max_correlated_exposure'")   # simulate an old folder
    again = LimitStore(tmp_path / "c.db", audit)
    assert again.current("2026-11-23").max_correlated_exposure == DEFAULTS.max_correlated_exposure
    assert any(e["decision"] == "limits_added" for e in audit.export())
