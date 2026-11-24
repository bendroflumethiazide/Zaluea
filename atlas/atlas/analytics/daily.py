"""Daily-bar backtests for the stock/ETF rules fixed in docs/09-stocks-plan.md.

Research only: returns are floats (they never become money). Execution model:
  * a rule decides at the CLOSE of day t using bars[0..t] only, and the trade happens at the OPEN of day t+1;
  * every buy and every sell costs COST (0.05%);
  * cash earns nothing; long only; fully in one ETF or fully in cash.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

COST = 0.0005
WARMUP = 200


@dataclass(frozen=True)
class Bar:
    day: str          # YYYY-MM-DD
    o: float
    c: float


def sma(closes: list[float], t: int, n: int) -> float | None:
    return None if t + 1 < n else sum(closes[t + 1 - n:t + 1]) / n


def rsi(closes: list[float], t: int, n: int = 2) -> float | None:
    """Wilder-style RSI over the last n changes, simple average (the common short-period form)."""
    if t < n:
        return None
    gains = losses = 0.0
    for i in range(t - n + 1, t + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


# ----------------------------------------------------------------------------- rules: (closes, t, holding, days_held) -> hold next day?
def rule_buy_hold(closes, t, holding, held):
    return True


def rule_trend(closes, t, holding, held):
    m = sma(closes, t, 200)
    return m is not None and closes[t] > m


def rule_pullback(closes, t, holding, held):
    if holding:
        m5 = sma(closes, t, 5)
        return not ((m5 is not None and closes[t] > m5) or held >= 5)
    m200, r = sma(closes, t, 200), rsi(closes, t, 2)
    return m200 is not None and r is not None and closes[t] > m200 and r < 10


RULES = {"S0 buy and hold": rule_buy_hold, "S1 trend filter": rule_trend, "S2 pullback": rule_pullback}


@dataclass
class Result:
    equity: list[tuple[str, float]] = field(default_factory=list)   # (day, equity at close)
    trades: list[dict] = field(default_factory=list)                # entry, exit, net return
    in_market: set[str] = field(default_factory=set)                # days held at the close


def simulate(bars: list[Bar], rule, start: int = WARMUP, cost: float = COST) -> Result:
    """Run `rule` from bar `start` on. The decision made at close t is applied at open t+1."""
    closes = [b.c for b in bars]
    res = Result()
    eq, holding, held, entry_eq, entry_day = 1.0, False, 0, 1.0, None
    want = False
    for t in range(start, len(bars)):
        b = bars[t]
        if t > start:                                   # act on yesterday's decision at today's open
            prev_c = bars[t - 1].c
            if holding and not want:                    # sell at the open
                eq *= b.o / prev_c * (1 - cost)
                res.trades.append({"entry": entry_day, "exit": b.day, "ret": eq / entry_eq - 1})
                holding, held = False, 0
            elif holding:
                eq *= b.c / prev_c
            if not holding and want:                    # buy at the open
                entry_eq = eq
                eq *= (1 - cost) * b.c / b.o
                holding, held, entry_day = True, 0, b.day
            if holding:
                held += 1
                res.in_market.add(b.day)
        res.equity.append((b.day, eq))
        want = rule(closes, t, holding, held)
    if holding:                                         # mark the open trade at the last close (no exit cost)
        res.trades.append({"entry": entry_day, "exit": None, "ret": eq / entry_eq - 1})
    return res


# ----------------------------------------------------------------------------- metrics
def _years(d0: str, d1: str) -> float:
    return max((date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25, 1e-9)


def window(res: Result, d0: str, d1: str) -> dict:
    """Metrics over [d0, d1], rebased to the equity at the first day in the window."""
    pts = [(d, e) for d, e in res.equity if d0 <= d <= d1]
    if len(pts) < 2:
        return {"cagr": None, "max_dd": None, "trades": [], "days_in": 0, "days": 0}
    base, peak, dd = pts[0][1], pts[0][1], 0.0
    for _, e in pts:
        peak = max(peak, e)
        dd = max(dd, 1 - e / peak)
    total = pts[-1][1] / base
    trades = [x for x in res.trades if x["exit"] and d0 <= x["entry"] and x["exit"] <= d1]
    in_days = sum(1 for d, _ in pts if d in res.in_market)
    years = {}
    prev = base
    for d, e in pts:
        y = d[:4]
        years.setdefault(y, [prev, e])
        years[y][1] = e
        prev = e
    return {"cagr": total ** (1 / _years(pts[0][0], pts[-1][0])) - 1, "max_dd": dd, "total": total - 1,
            "trades": trades, "days": len(pts) - 1, "days_in": in_days,
            "years": {y: v[1] / v[0] - 1 for y, v in years.items()}}


def t_stat(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1)
    return None if v == 0 else m / math.sqrt(v / n)


# ----------------------------------------------------------------------------- decision rules (docs/09)
def judge_trend(per_symbol: dict[str, tuple[dict, dict]]) -> dict:
    """per_symbol: symbol -> (S1 recent metrics, S0 recent metrics)."""
    ok = {}
    for sym, (s1, s0) in per_symbol.items():
        if s1["cagr"] is None or s0["cagr"] is None:
            ok[sym] = (False, "not enough data")
            continue
        dd_ok = s1["max_dd"] <= 0.70 * s0["max_dd"]
        ret_ok = s1["cagr"] >= s0["cagr"] - 0.02
        ok[sym] = (dd_ok and ret_ok,
                   f"drawdown {s1['max_dd']:.1%} vs {s0['max_dd']:.1%} (needs ≤ {0.70 * s0['max_dd']:.1%}); "
                   f"annual return {s1['cagr']:.1%} vs {s0['cagr']:.1%} (needs ≥ {s0['cagr'] - 0.02:.1%})")
    others = [s for s in ok if s != "SPY"]
    passed = ok.get("SPY", (False,))[0] and any(ok[s][0] for s in others)
    return {"passed": passed, "detail": {s: v[1] for s, v in ok.items()},
            "per_symbol_pass": {s: v[0] for s, v in ok.items()}}


def judge_pullback(trades: list[dict], buy_hold_daily: float, invested_days: int) -> dict:
    rets = [x["ret"] for x in trades]
    n, t = len(rets), t_stat(rets)
    by_year: dict[str, float] = {}
    for x in trades:
        by_year[x["exit"][:4]] = by_year.get(x["exit"][:4], 0.0) + x["ret"]
    good_years = sum(1 for v in by_year.values() if v > 0)
    growth = math.prod(1 + r for r in rets) if rets else 1.0
    per_day = growth ** (1 / invested_days) - 1 if invested_days else 0.0
    checks = [
        (n >= 100, f"{n} trades (needs ≥ 100)"),
        (n > 0 and sum(rets) > 0 and t is not None and t >= 2,
         f"mean {(sum(rets) / n if n else 0):.3%} per trade, t = {'–' if t is None else f'{t:.2f}'} (needs > 0 and t ≥ 2)"),
        (bool(by_year) and good_years / len(by_year) >= 0.6, f"{good_years} of {len(by_year)} years positive (needs ≥ 60%)"),
        (per_day > buy_hold_daily, f"{per_day:.4%} per invested day vs buy-and-hold {buy_hold_daily:.4%}"),
    ]
    return {"passed": all(c for c, _ in checks), "checks": checks}
