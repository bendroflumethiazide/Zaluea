"""Stock/ETF daily backtest (docs/09-stocks-plan.md): indicators, execution timing, costs, verdicts, data."""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from atlas.analytics.daily import (COST, Bar, judge_pullback, judge_trend, rsi, rule_pullback, rule_trend,
                                   rule_buy_hold, simulate, sma, window)
from atlas.exchange.alpaca import AlpacaClient


def days(n, start=date(2015, 3, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def walk(n, seed=1, drift=0.0003, vol=0.01):
    rnd, p, out = random.Random(seed), 100.0, []
    for d in days(n):
        o = p * (1 + rnd.gauss(0, vol / 3))
        p = o * (1 + rnd.gauss(drift, vol))
        out.append(Bar(d, o, p))
    return out


def test_indicators():
    closes = [1, 2, 3, 4, 5]
    assert sma(closes, 4, 5) == 3 and sma(closes, 3, 5) is None
    assert rsi([10, 9, 8], 2) == 0.0 and rsi([8, 9, 10], 2) == 100.0
    assert rsi([10, 11, 10], 2) == pytest.approx(50.0)


def test_buy_and_hold_buys_at_the_next_open_and_pays_one_cost():
    bars = [Bar(d, 100.0 + i, 100.5 + i) for i, d in enumerate(days(260))]
    r = simulate(bars, rule_buy_hold)
    assert r.trades[0]["entry"] == bars[201].day                       # decided at close 200, bought at open 201
    assert r.equity[-1][1] == pytest.approx((1 - COST) * bars[-1].c / bars[201].o)


def test_a_decision_never_depends_on_later_prices():
    bars = walk(500)
    for rule in (rule_trend, rule_pullback):
        base = simulate(bars, rule)
        cut = 400
        shocked = bars[:cut + 1] + [Bar(b.day, b.o * 3, b.c * 0.2) for b in bars[cut + 1:]]
        other = simulate(shocked, rule)
        k = cut - 200 + 1                                                # equity points up to and including day `cut`
        assert base.equity[:k] == other.equity[:k]
        assert [t for t in base.trades if t["exit"] and t["exit"] <= bars[cut].day] == \
               [t for t in other.trades if t["exit"] and t["exit"] <= bars[cut].day]


def test_a_round_trip_pays_cost_twice_and_trades_at_opens():
    ds = days(205)
    flat = [Bar(d, 100.0, 100.0) for d in ds[:200]]
    tail = [Bar(ds[200], 100.0, 110.0), Bar(ds[201], 110.0, 110.0), Bar(ds[202], 120.0, 120.0),
            Bar(ds[203], 120.0, 120.0), Bar(ds[204], 120.0, 120.0)]
    bars = flat + tail
    seq = iter([True, False, False, False, False])                        # buy signal at close of ds[200] only
    r = simulate(bars, lambda c, t, h, held: next(seq), start=200)
    tr = r.trades[0]
    assert tr["entry"] == ds[201] and tr["exit"] == ds[202]
    assert tr["ret"] == pytest.approx((1 - COST) ** 2 * (120 / 110) - 1)   # bought 110 open, sold 120 open


def test_pullback_exits_within_five_days():
    bars = walk(900, seed=7, drift=0.0005)
    r = simulate(bars, rule_pullback)
    idx = {b.day: i for i, b in enumerate(bars)}
    assert r.trades
    for t in r.trades:
        if t["exit"]:
            assert 1 <= idx[t["exit"]] - idx[t["entry"]] <= 6


def test_window_metrics_rebase_and_count_drawdown():
    bars = [Bar(d, 100.0, 100.0) for d in days(200)] + \
           [Bar(d, p, p) for d, p in zip(days(206)[200:], [100, 100, 80, 90, 120, 120])]
    m = window(simulate(bars, rule_buy_hold), bars[200].day, bars[-1].day)
    assert m["max_dd"] == pytest.approx(1 - 0.8 * (1 - COST))          # the entry cost counts
    assert m["total"] == pytest.approx(1.2 * (1 - COST) - 1)


def test_trend_verdict_needs_spy_and_one_other():
    good = ({"cagr": 0.09, "max_dd": 0.10}, {"cagr": 0.10, "max_dd": 0.25})
    bad = ({"cagr": 0.05, "max_dd": 0.10}, {"cagr": 0.10, "max_dd": 0.25})        # gives up 5 points a year
    assert judge_trend({"SPY": good, "QQQ": bad, "IWM": good})["passed"]
    assert not judge_trend({"SPY": bad, "QQQ": good, "IWM": good})["passed"]
    assert not judge_trend({"SPY": good, "QQQ": bad, "IWM": bad})["passed"]


def test_pullback_verdict_checks_every_rule():
    win = [{"entry": f"{2022 + i % 4}-01-02", "exit": f"{2022 + i % 4}-01-05", "ret": 0.004 + (i % 3) * 0.001}
           for i in range(120)]
    assert judge_pullback(win, 0.0003, 400)["passed"]
    assert not judge_pullback(win[:50], 0.0003, 200)["passed"]                     # too few trades
    assert not judge_pullback(win, 0.01, 400)["passed"]                            # holding the index is better
    lose = [{**x, "ret": -x["ret"]} for x in win]
    assert not judge_pullback(lose, 0.0003, 400)["passed"]


def test_alpaca_daily_bars_pages_and_keeps_exact_prices():
    calls = []
    pages = [{"bars": [{"t": "2016-01-04T05:00:00Z", "o": 200.49, "h": 201.03, "l": 198.59, "c": 201.0192,
                        "v": 222353500}], "next_page_token": "abc"},
             {"bars": [{"t": "2016-01-05T05:00:00Z", "o": 201.4, "h": 201.9, "l": 200.05, "c": 201.36, "v": 1e8}],
              "next_page_token": None}]

    def transport(method, url, headers, body, timeout):
        calls.append(url)
        import json
        return 200, {}, json.loads(json.dumps(pages[len(calls) - 1]), parse_float=str)
    c = AlpacaClient("kid", "secret", transport=transport)
    bars = c.daily_bars("SPY", "2016-01-01", "2016-01-06")
    assert [b["date"] for b in bars] == ["2016-01-04", "2016-01-05"] and bars[0]["c"] == "201.0192"
    assert "adjustment=all" in calls[0] and "feed=sip" in calls[0] and "timeframe=1Day" in calls[0]
    assert "page_token=abc" in calls[1] and "page_token" not in calls[0]


def test_report_runs_end_to_end_offline(tmp_path, monkeypatch):
    import csv
    import research.stocks_backtest as sb
    monkeypatch.setattr(sb, "DATA", tmp_path)
    for i, sym in enumerate(sb.SYMBOLS):
        with open(tmp_path / f"{sym}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "close"])
            for b in walk(2900, seed=i + 3):
                w.writerow([b.day, b.o, b.c])
    text = sb.report(sb.SYMBOLS)
    assert "## SPY" in text and "S1 trend filter:" in text and "S2 pullback:" in text and "## Outcome" in text
    with open(tmp_path / "SPY.csv", "a", newline="") as fh:
        csv.writer(fh).writerow(["2015-03-02", "1", "1"])                    # out-of-order row is refused
    with pytest.raises(SystemExit):
        sb.load("SPY")
