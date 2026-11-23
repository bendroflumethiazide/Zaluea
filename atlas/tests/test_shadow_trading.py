"""Phase 5 / Part 25: shadow trading.

Covers: it cannot submit live orders; it keeps a separate balance; it charges transaction
costs; it models latency, displayed liquidity, queue position and partial fills; it never uses
future data; simulated and real P&L are reported separately; missed and rejected opportunities
are recorded and resolved.
"""
import ast
import hashlib
import json
from pathlib import Path

import pytest

from atlas.ledger import Ledger
from atlas.limits import DEFAULTS
from atlas.marketdata.btc import Reference
from atlas.money import KALSHI_ROUNDING_ALLOWANCE, Money, kalshi_taker_fee, kalshi_worst_case_fee
from atlas.paths import initialize
from atlas.proposal import Proposal, Quote
from atlas.shadow.account import SeparationError, SimAccount, research_limits
from atlas.shadow.journal import Journal
from atlas.shadow.report import report_markdown
from atlas.shadow.runner import ShadowRunner
from atlas.shadow.sim import Book, PublicTrade, SimExchange
from atlas.strategies.base import MarketView, NoTrade, Series, Signal, Strategy
from atlas.strategies.crypto_maker import CryptoMaker, MakerConfig

M = Money.parse
PKG = Path(__file__).resolve().parents[1] / "atlas"
T0 = 1_795_400_000.0


def book(bid, ask, bid_size=10, ask_size=10, ts=T0):
    return Book(M(bid) if bid else None, M(ask) if ask else None, bid_size, ask_size, ts)


def trade(i, yes_price, count, taker_side, ts):
    return PublicTrade(f"t{i}", M(yes_price), count, taker_side, ts)


# ----------------------------------------------------------------------------- the simulator
def test_taker_fill_is_limited_to_displayed_size_and_pays_the_worst_case_fee():
    sx = SimExchange(latency_s=1.0)
    sx.update_book("MKT", "yes", book("0.40", "0.42", ask_size=3))
    sx.submit("c1", "MKT", "yes", 5, M("0.42"), "taker", T0, T0 + 10)
    assert sx.step(T0 + 0.5) == []                         # latency: nothing happens yet
    fills = sx.step(T0 + 1.0)
    assert [(f[2], f[3]) for f in fills] == [(3, M("0.42"))]          # partial: only 3 displayed
    assert fills[0][4] == kalshi_worst_case_fee(3, M("0.42")) == kalshi_taker_fee(3, M("0.42")) + KALSHI_ROUNDING_ALLOWANCE
    assert sx.orders["c1"].status == "cancelled"           # immediate-or-cancel: the rest is gone


def test_taker_uses_the_book_after_the_latency_not_at_decision_time():
    sx = SimExchange(latency_s=1.0)
    sx.update_book("MKT", "yes", book("0.40", "0.42"))
    sx.submit("c1", "MKT", "yes", 2, M("0.42"), "taker", T0, T0 + 10)
    sx.update_book("MKT", "yes", book("0.44", "0.45", ts=T0 + 0.8))   # price ran away during the delay
    assert sx.step(T0 + 1.0) == []
    assert sx.orders["c1"].status == "cancelled" and sx.orders["c1"].filled == 0


def test_resting_order_fills_only_from_later_public_trades():
    sx = SimExchange(latency_s=1.0)
    sx.update_book("MKT", "yes", book("0.40", "0.45", bid_size=0))
    sx.submit("m1", "MKT", "yes", 4, M("0.41"), "maker", T0, T0 + 600)
    early = trade(1, "0.41", 10, "no", T0 + 0.5)            # printed before our order was live
    assert sx.step(T0 + 1.0, {"MKT": [early]}) == []
    assert sx.step(T0 + 5, {}) == []                         # no trades, no fill (even if the book moves)
    fills = sx.step(T0 + 6, {"MKT": [trade(2, "0.41", 3, "no", T0 + 5.5)]})
    assert [(f[2], f[3]) for f in fills] == [(3, M("0.41"))]
    assert sx.orders["m1"].status == "open"                  # partial fill keeps resting
    fills = sx.step(T0 + 7, {"MKT": [trade(3, "0.39", 5, "no", T0 + 6.5)]})   # trade THROUGH our price
    assert [f[2] for f in fills] == [1] and sx.orders["m1"].status == "filled"


def test_a_trade_printed_after_now_is_future_data_and_waits():
    sx = SimExchange(latency_s=0.0)
    sx.update_book("MKT", "yes", book("0.40", "0.45", bid_size=0))
    sx.submit("m1", "MKT", "yes", 2, M("0.41"), "maker", T0, T0 + 600)
    future = trade(1, "0.41", 2, "no", T0 + 50)
    assert sx.step(T0 + 10, {"MKT": [future]}) == []
    assert len(sx.step(T0 + 60, {"MKT": [future]})) == 1


def test_queue_ahead_at_our_price_fills_first():
    sx = SimExchange(latency_s=0.0)
    sx.update_book("MKT", "yes", book("0.41", "0.45", bid_size=5))   # 5 contracts already bid at 0.41
    sx.submit("m1", "MKT", "yes", 3, M("0.41"), "maker", T0, T0 + 600)
    sx.step(T0)
    assert sx.orders["m1"].queue_ahead == 5
    assert sx.step(T0 + 2, {"MKT": [trade(1, "0.41", 4, "no", T0 + 1)]}) == []
    fills = sx.step(T0 + 3, {"MKT": [trade(2, "0.41", 3, "no", T0 + 2.5)]})
    assert [f[2] for f in fills] == [2]                     # 1 more ahead of us, then 2 for us


def test_buyers_of_our_side_never_fill_our_bid():
    sx = SimExchange(latency_s=0.0)
    sx.update_book("MKT", "yes", book("0.40", "0.45", bid_size=0))
    sx.submit("m1", "MKT", "yes", 2, M("0.41"), "maker", T0, T0 + 600)
    sx.step(T0)
    assert sx.step(T0 + 2, {"MKT": [trade(1, "0.41", 9, "yes", T0 + 1)]}) == []


def test_no_side_resting_order_uses_the_no_price():
    sx = SimExchange(latency_s=0.0)
    sx.update_book("MKT", "no", book("0.30", "0.35", bid_size=0))
    sx.submit("m1", "MKT", "no", 2, M("0.31"), "maker", T0, T0 + 600)
    sx.step(T0)
    assert sx.step(T0 + 2, {"MKT": [trade(1, "0.68", 5, "yes", T0 + 1)]}) == []   # NO at 0.32: above our bid
    fills = sx.step(T0 + 3, {"MKT": [trade(2, "0.69", 5, "yes", T0 + 2)]})        # NO at 0.31
    assert [(f[2], f[3]) for f in fills] == [(2, M("0.31"))]


def test_resting_order_that_would_cross_is_rejected_as_post_only():
    sx = SimExchange(latency_s=0.0)
    sx.update_book("MKT", "yes", book("0.40", "0.41"))
    sx.submit("m1", "MKT", "yes", 2, M("0.41"), "maker", T0, T0 + 600)
    sx.step(T0)
    assert sx.orders["m1"].status == "cancelled" and sx.orders["m1"].filled == 0


def test_cancel_takes_the_latency_to_land():
    sx = SimExchange(latency_s=1.0)
    sx.update_book("MKT", "yes", book("0.40", "0.45", bid_size=0))
    sx.submit("m1", "MKT", "yes", 4, M("0.41"), "maker", T0, T0 + 600)
    sx.step(T0 + 1)
    sx.cancel("m1", T0 + 10)
    fills = sx.step(T0 + 12, {"MKT": [trade(1, "0.41", 1, "no", T0 + 10.5),     # before the cancel landed
                                      trade(2, "0.41", 1, "no", T0 + 11.5)]})   # after: not ours
    assert [f[2] for f in fills] == [1] and sx.orders["m1"].status == "cancelled"


def test_one_public_trade_is_not_counted_twice():
    sx = SimExchange(latency_s=0.0)
    sx.update_book("A", "yes", book("0.40", "0.45", bid_size=0))
    sx.submit("m1", "A", "yes", 3, M("0.41"), "maker", T0, T0 + 600)
    sx.submit("m2", "A", "yes", 3, M("0.41"), "maker", T0, T0 + 600)
    sx.step(T0)
    t = trade(1, "0.40", 4, "no", T0 + 1)
    fills = sx.step(T0 + 2, {"A": [t]})
    assert sum(f[2] for f in fills) == 4
    assert sx.step(T0 + 3, {"A": [t]}) == []                # the same trade fed again is ignored


def test_orders_expire():
    sx = SimExchange(latency_s=0.0)
    sx.update_book("MKT", "yes", book("0.40", "0.45", bid_size=0))
    sx.submit("m1", "MKT", "yes", 2, M("0.41"), "maker", T0, T0 + 60)
    sx.step(T0)
    sx.step(T0 + 61)
    assert sx.orders["m1"].status == "expired"


def test_maker_fee_assumption_is_pessimistic_and_can_be_switched_off():
    assert SimExchange(maker_fee_pct=25).maker_fee(4, M("0.50"), 100) == \
        kalshi_taker_fee(4, M("0.50"), 25) + KALSHI_ROUNDING_ALLOWANCE
    assert SimExchange(maker_fee_pct=0).maker_fee(4, M("0.50"), 100) == Money.ZERO


# ----------------------------------------------------------------------------- accounts
def proposal(market="MKT", side="yes", qty=2, price="0.40", ev="0.10"):
    q = Quote(market, side, M("0.39"), M("0.45"), 10, T0, "test")
    return Proposal("kalshi", market, "EVT-" + market, "stub", "stub@1", side, qty, M(price), q, 6000, M(ev),
                    "test", "hold", 100)


def account(tmp_path, limits=DEFAULTS, name="a"):
    return SimAccount(tmp_path / "sim" / name, name, "stub", "maker", limits, Journal(tmp_path / "j.db"),
                      latency_s=0.0, clock=lambda: T0)


def test_simulated_account_refuses_a_folder_outside_sim(tmp_path):
    with pytest.raises(SeparationError):
        SimAccount(tmp_path / "real", "x", "stub", "maker", DEFAULTS, Journal(tmp_path / "j.db"))


def test_fill_and_settlement_pnl_include_every_fee(tmp_path):
    a = account(tmp_path)
    day = "2026-11-22"
    a.sim.update_book("MKT", "yes", book("0.39", "0.45", bid_size=0))
    coid, why = a.submit(proposal(), now=T0, day=day, expires_at=T0 + 600, kind="maker")
    assert coid and why == "submitted"
    a.step(T0, day, {})
    ev = a.step(T0 + 2, day, {"MKT": [trade(1, "0.40", 5, "no", T0 + 1)]})
    fee = a.sim.maker_fee(2, M("0.40"), 100)
    assert ev[0]["fee"] == fee and a.ledger.order(coid)["status"] == "filled"
    out = a.settle("MKT", "yes", day)
    assert out[0]["pnl"] == Money.dollars(2) - M("0.80") - fee
    assert a.ledger.snapshot(day).realized_today == Money.dollars(2) - M("0.80") - fee
    assert a.settle("MKT", "yes", day) == []                # idempotent


def test_as_live_account_enforces_the_real_limits(tmp_path):
    a = account(tmp_path)
    coid, why = a.submit(proposal(qty=5, price="0.40"), now=T0, day="2026-11-22", expires_at=T0 + 60, kind="maker")
    assert coid is None and "per-purchase limit" in why


def test_as_live_stops_after_three_losses_but_research_keeps_going(tmp_path):
    live, research = account(tmp_path, name="live"), account(tmp_path, research_limits(DEFAULTS), name="research")
    day = "2026-11-22"
    for acct in (live, research):
        for i in range(3):
            m = f"L{i}"
            acct.sim.update_book(m, "yes", book("0.39", "0.45", bid_size=0))
            coid, _ = acct.submit(proposal(market=m, qty=1), now=T0, day=day, expires_at=T0 + 600, kind="maker")
            acct.step(T0, day, {})
            acct.step(T0 + 2, day, {m: [trade(f"{m}{acct.name}", "0.40", 1, "no", T0 + 1)]})
            acct.settle(m, "no", day)                       # every one loses
    assert live.submit(proposal(market="L9", qty=1), now=T0, day=day, expires_at=T0 + 60, kind="maker")[0] is None
    assert research.submit(proposal(market="L9", qty=1), now=T0, day=day, expires_at=T0 + 60, kind="maker")[0]


# ----------------------------------------------------------------------------- strategy
def maker_view(now_min=6.0, yes=("0.70", "0.74"), ref_price="60030", strike="60000"):
    now = T0 + now_min * 60
    pts = [(T0 + 30 * i, (M(yes[0]), M(yes[1]), 50)) for i in range(int(now_min * 2) + 1)]
    no = [(t, (Money.dollars(1) - a, Money.dollars(1) - b, s)) for t, (b, a, s) in pts]
    ref, p = [], float(ref_price)
    for i in range(90):
        p += 12.0 if i % 2 else -12.0
        ref.append((now - (89 - i) * 60, M(f"{p:.2f}")))
    return MarketView(now=now, market="KXBTC15M-T", event="KXBTC15M-E", module="kalshi", open_ts=T0,
                      close_ts=T0 + 900, strike=M(strike), fee_multiplier_pct=100,
                      quotes={"yes": Series(pts).cut(now), "no": Series(no).cut(now)}, reference=Series(ref).cut(now))


def test_maker_rests_inside_the_spread_with_value_after_fees():
    r = CryptoMaker().evaluate(maker_view())
    assert isinstance(r, Signal), getattr(r, "reasons", None)
    assert r.side == "yes" and r.limit_price == M("0.71")   # one cent better than the 0.70 bid, below the ask
    assert r.limit_price * r.qty + kalshi_worst_case_fee(r.qty, r.limit_price) <= Money.dollars(2)
    assert r.expected_value >= M("0.03") * r.qty


def test_maker_refuses_when_model_and_market_disagree_too_much():
    r = CryptoMaker().evaluate(maker_view(yes=("0.20", "0.24")))
    assert isinstance(r, NoTrade) and any("disagrees with the market" in x for x in r.reasons)


def test_maker_only_quotes_inside_its_window():
    r = CryptoMaker().evaluate(maker_view(now_min=13.5))
    assert isinstance(r, NoTrade) and "minutes 2-13" in r.reasons[0]


# ----------------------------------------------------------------------------- the runner
class FakePublicKalshi:
    """Public market data only: there is no order method at all."""
    signer = None

    def __init__(self, clock):
        self.clock = clock
        self.yes = ("0.40", "0.45", 0, 10)
        self.trades = []
        self.result = None
        self.info = {"event": "KXBTC15M-E", "close_ts": T0 + 900, "open_ts": T0, "fee_type": "quadratic",
                     "fee_multiplier": "1", "strike": 60000}

    def open_markets(self, series, limit=5):
        return [{"ticker": "KXBTC15M-T", "close_time": "x"}] if self.clock() < T0 + 900 else []

    def market_info(self, m):
        return dict(self.info)

    def book(self, m):
        b, a, bs, as_ = self.yes
        one = Money.dollars(1)
        return {"market": m, "ts": self.clock(), "yes": {"bid": b, "bid_size": bs, "ask": a, "ask_size": as_},
                "no": {"bid": str(one - M(a)), "bid_size": as_, "ask": str(one - M(b)), "ask_size": bs}}

    def recent_trades(self, m, min_ts):
        return [t for t in self.trades if t["ts"] >= min_ts], True

    def market_result(self, m):
        return self.result


class FakeRef:
    def __init__(self, clock):
        self.clock = clock

    def get(self):
        return Reference(True, M("60100"), [], "ok", self.clock())


class StubMaker(Strategy):
    name, module, kind = "stub_maker", "kalshi", "maker"

    def __init__(self):
        super().__init__(MakerConfig())
        self.on = True

    def evaluate(self, v):
        if not self.on:
            return NoTrade(self.name, v.market, ["off"])
        return Signal(self.name, self.version, v.market, "yes", 2, M("0.41"), 6000, 4250, M("0.30"),
                      Money.ZERO, ["stub"], ["test"], "hold")


def make_runner(tmp_path, real=True):
    now = [T0 + 120]
    clock = lambda: now[0]
    if real:
        initialize(tmp_path / "data")
    k = FakePublicKalshi(clock)
    strat = StubMaker()
    r = ShadowRunner(tmp_path / "data", kalshi=k, reference=FakeRef(clock), history=lambda n: [],
                     strategies=[strat], clock=clock, latency_s=1.0)
    return r, k, strat, now


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_runner_end_to_end_with_separate_balance(tmp_path):
    r, k, strat, now = make_runner(tmp_path)
    real_ledger = tmp_path / "data" / "ledger.db"
    before = file_hash(real_ledger)
    st = r.tick()                                            # rests a bid in both accounts
    assert all(a["submitted"] == 1 and a["open_orders"] == 1 for a in st["accounts"])
    now[0] += 3
    k.trades = [{"trade_id": "x1", "yes_price": "0.41", "count": 1, "taker_side": "no", "ts": now[0] - 1}]
    st = r.tick()
    assert all(a["contracts_filled"] == 1 for a in st["accounts"])
    strat.on = False                                         # the strategy withdraws: the rest is cancelled
    now[0] += 3
    r.tick()
    now[0] += 3
    st = r.tick()
    assert all(a["open_orders"] == 0 and a["missed"] == 1 for a in st["accounts"])
    now[0] = T0 + 900 + 120
    k.result = "yes"
    st = r.tick()
    for a in st["accounts"]:
        fee = SimExchange().maker_fee(1, M("0.41"), 100)
        assert a["settled"] == 1 and a["pnl_total"] == str(Money.dollars(1) - M("0.41") - fee)
        assert a["missed_resolved"] == 1 and a["missed_hypothetical_pnl"] == str(
            Money.dollars(1) - M("0.41") - SimExchange().maker_fee(1, M("0.41"), 100))
    saved = json.loads((tmp_path / "data" / "sim" / "shadow.json").read_text())
    assert saved["simulated"] is True and "SIMULATED" in saved["label"]
    assert saved["real"]["available"] and saved["real"]["pnl_total"] == "$0.00"   # real P&L kept apart
    assert file_hash(real_ledger) == before                  # the real ledger was never touched
    real = Ledger(real_ledger, None, readonly=True)
    assert real.open_orders() == [] and real.snapshot("2026-11-22").positions == []
    assert "SIMULATED" in report_markdown(tmp_path / "data")


def test_runner_refuses_a_kalshi_client_with_a_key(tmp_path):
    class Keyed(FakePublicKalshi):
        signer = object()
    with pytest.raises(SeparationError):
        ShadowRunner(tmp_path / "d", kalshi=Keyed(lambda: T0), reference=FakeRef(lambda: T0), history=lambda n: [])


def test_restart_cancels_simulated_orders_left_open(tmp_path):
    r, k, strat, now = make_runner(tmp_path, real=False)
    r.tick()
    r.close()
    r2 = ShadowRunner(tmp_path / "data", kalshi=k, reference=FakeRef(lambda: now[0]), history=lambda n: [],
                      strategies=[strat], clock=lambda: now[0])
    for _, acct in r2.accounts:
        assert acct.ledger.open_orders() == []


def test_rejections_are_recorded_with_their_reason(tmp_path):
    r, k, strat, now = make_runner(tmp_path, real=False)
    strat.evaluate = lambda v: Signal("stub_maker", strat.version, v.market, "yes", 9, M("0.41"), 6000, 4250,
                                      M("0.30"), Money.ZERO, ["stub"], ["test"], "hold")
    st = r.tick()
    for a in st["accounts"]:
        assert a["rejected"] == 1 and "per-purchase limit" in a["rejected_reasons"][0][0]


# ----------------------------------------------------------------------------- cannot trade for real
SHADOW_FILES = sorted((PKG / "shadow").glob("*.py")) + [PKG / "strategies" / "crypto_maker.py"]


def test_shadow_trading_cannot_reach_live_execution():
    forbidden_calls = {"place_order", "load_signer", "reserve_exit"}
    forbidden_imports = {"gateway", "exchange.alpaca", "check_connection", "shadowrisk", "watchdog", "control"}
    for f in SHADOW_FILES:
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                assert name not in forbidden_calls, f"{f.name} calls {name}"
                if name == "KalshiExchange":
                    kw = {k.arg: k.value for k in n.keywords}
                    assert isinstance(kw.get("signer"), ast.Constant) and kw["signer"].value is None, \
                        f"{f.name} builds a Kalshi client that could carry a key"
            if isinstance(n, ast.ImportFrom):
                mod = (n.module or "")
                assert not any(mod.endswith(x) for x in forbidden_imports), f"{f.name} imports {mod}"



# ----------------------------------------------------------------------------- dashboard
def test_dashboard_shows_simulated_results_labelled_and_survives_a_bad_file(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
    from real_state import RealState
    r, k, strat, now = make_runner(tmp_path)
    r.tick()
    rs = RealState(tmp_path / "data")
    sh = rs._shadow(now[0] + 5)
    assert sh["real"] and sh["running"] and "SIMULATED" in sh["label"]
    assert sh["account"] == "stub_maker/as_live" and len(sh["accounts"]) == 2
    assert sh["accounts"][0]["orders"] == 1 and sh["accounts"][0]["open"] == 1
    assert not rs._shadow(now[0] + 600)["running"]                       # stale file is flagged
    (tmp_path / "data" / "sim" / "shadow.json").write_text('{"simulated": true, "accounts": [{}]}')
    with pytest.raises(KeyError):
        rs._shadow(now[0])                                               # overlay() catches this
