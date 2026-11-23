"""Walk-forward backtest mechanics on synthetic data."""
from atlas.analytics import backtest as bt
from atlas.money import Money
from atlas.strategies.base import Series

M = Money.parse
WEEK = 7 * 86_400
T0 = 1_790_000_000.0


def synthetic(n_weeks=5, per_week=6, win=True):
    markets, ref_pts = [], []
    for w in range(n_weeks):
        for i in range(per_week):
            open_ts = T0 + w * WEEK + i * 3600
            q = [(open_ts + 60 * m, (M("0.90"), M("0.91"), None)) for m in range(16)]
            qn = [(t, (M("0.09"), M("0.10"), None)) for t, _ in q]
            markets.append(bt.MarketData(f"KXBTC15M-W{w}-{i}", f"E{w}{i}", open_ts, open_ts + 900, M("60000"),
                                         "yes" if win else "no", {"yes": Series(q), "no": Series(qn)}))
            p = 60400.0
            for k in range(-70, 16):
                p += 12.0 if k % 2 else -12.0
                ref_pts.append((open_ts + 60 * k, M(f"{p:.2f}")))
    ref_pts.sort(key=lambda x: x[0])
    return markets, Series(ref_pts)


SMALL_GRID = {"vol_scale": ("1.0",), "z_min": ("2.0", "3.0"), "min_price": ("0.80",)}


def test_walk_forward_tests_only_after_training_weeks():
    markets, ref = synthetic()
    res = bt.walk_forward(markets, ref, min_train_weeks=3, min_train_trades=5, grid=SMALL_GRID)
    tested = [w["week"] for w in res.weeks if w.get("config")]
    all_weeks = sorted({bt.week_of(m.open_ts) for m in markets})
    assert tested == all_weeks[3:]                         # the first 3 weeks are training only
    assert all(t.week in tested for t in res.oos)


def test_costs_are_exact_and_slippage_applied():
    markets, ref = synthetic(n_weeks=1, per_week=1)
    from atlas.strategies.crypto_flip import CryptoFlipFavorite, FlipConfig
    trades = bt.run_config(CryptoFlipFavorite(FlipConfig(slippage_per_contract="0.01")), markets, ref)
    (t,) = trades.values()
    assert t.slippage == M("0.01") * t.qty
    assert t.pnl == Money.dollars(1) * t.qty - (t.price * t.qty + t.fee + t.slippage)


def test_losing_strategy_gets_no_reliable_edge_verdict():
    markets, ref = synthetic(win=False)
    res = bt.walk_forward(markets, ref, min_train_weeks=3, min_train_trades=5, grid=SMALL_GRID)
    assert not res.passed and res.verdict.startswith("NO RELIABLE EDGE")


def test_too_few_trades_is_never_a_pass_even_if_profitable():
    markets, ref = synthetic(win=True)
    res = bt.walk_forward(markets, ref, min_train_weeks=3, min_train_trades=5, grid=SMALL_GRID)
    assert res.meta["oos"]["pnl"].units > 0 and not res.passed and "out-of-sample trades" in res.verdict
