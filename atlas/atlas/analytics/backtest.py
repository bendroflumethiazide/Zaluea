"""Walk-forward backtest for crypto 15-minute strategies.

Honesty rules built in:
  * Point-in-time data only: the strategy receives Series cut at the decision
    time (LookAheadError on any future access).
  * Walk-forward: for each test week, parameters are chosen using ONLY the
    weeks before it, then applied unchanged to that week. Reported results are
    the concatenation of those out-of-sample weeks.
  * Success criteria are fixed in advance (CRITERIA) and applied mechanically.
  * Costs: exact Kalshi fee, plus slippage per contract (default $0.01).
  * Known optimism (stated in every report): historical order-book depth is
    not available, so fills at the quoted ask are assumed to succeed.
"""
from __future__ import annotations

import itertools
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..money import Money
from ..strategies.base import MarketView, Series, Signal
from ..strategies.crypto_flip import CryptoFlipFavorite, FlipConfig
from .probability import brier, calibration

ET = ZoneInfo("America/New_York")
ONE = Money.dollars(1)
CRITERIA = {"min_oos_trades": 200, "min_t_stat": 2.0, "min_share_weeks_non_negative": 0.60}
DECISION_MINUTES = (10, 11, 12, 13)


def _m(s) -> Money | None:
    if s in (None, ""):
        return None
    return Money.parse(str(s))


@dataclass
class MarketData:
    ticker: str
    event: str
    open_ts: float
    close_ts: float
    strike: Money | None
    result: str
    quotes: dict


@dataclass
class Trade:
    ticker: str
    week: str
    minute: float
    side: str
    qty: int
    price: Money
    fee: Money
    slippage: Money
    prob_bp: int
    pnl: Money
    won: bool


def week_of(ts: float) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc).astimezone(ET)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def load(data_dir: Path) -> tuple[list[MarketData], Series, dict]:
    markets = json.loads((data_dir / "btc15m_markets.json").read_text())
    quotes = json.loads((data_dir / "btc15m_quotes.json").read_text())
    cb = json.loads((data_dir / "ref_coinbase.json").read_text())
    bs = json.loads((data_dir / "ref_bitstamp.json").read_text())
    # Reference: median (= mean of two) of Coinbase and Bitstamp minute closes; a minute is
    # available at the END of its candle. Minutes where the sources disagree by > 30 bps are dropped.
    pts, dropped = [], 0
    for t in sorted(set(cb) & set(bs), key=int):
        a, b = Money.parse(cb[t]), Money.parse(bs[t])
        if abs(a.units - b.units) * 10_000 > 30 * min(a.units, b.units):
            dropped += 1
            continue
        pts.append((int(t) + 60, Money((a.units + b.units) // 2)))
    ref = Series(pts)
    out = []
    for m in markets:
        rows = quotes.get(m["ticker"]) or []
        yes, no = [], []
        for end_ts, bid, ask, _vol in rows:
            b, a = _m(bid), _m(ask)
            b = b if b is not None and b.units > 0 else None
            a = a if a is not None and a < ONE else None
            yes.append((end_ts, (b, a, None)))
            no.append((end_ts, (ONE - a if a is not None else None, ONE - b if b is not None else None, None)))
        if not yes or m["result"] not in ("yes", "no"):
            continue
        out.append(MarketData(m["ticker"], m["event"], m["open_ts"], m["close_ts"], _m(m["strike"]),
                              m["result"], {"yes": Series(yes), "no": Series(no)}))
    out.sort(key=lambda x: x.open_ts)
    meta = {"markets": len(out), "reference_minutes": len(pts), "reference_minutes_dropped": dropped,
            "first": out[0].open_ts if out else None, "last": out[-1].close_ts if out else None}
    return out, ref, meta


def run_config(strategy: CryptoFlipFavorite, markets: list[MarketData], ref: Series) -> dict[str, Trade]:
    """First signal per market at the decision minutes -> trade outcome."""
    trades = {}
    for m in markets:
        for minute in DECISION_MINUTES:
            now = m.open_ts + minute * 60
            v = MarketView(now=now, market=m.ticker, event=m.event, module="kalshi", open_ts=m.open_ts,
                           close_ts=m.close_ts, strike=m.strike, fee_multiplier_pct=100,
                           quotes={k: s.cut(now) for k, s in m.quotes.items()}, reference=ref.cut(now))
            r = strategy.evaluate(v)
            if isinstance(r, Signal):
                slip = strategy.slip * r.qty
                won = r.side == m.result
                pnl = (ONE * r.qty if won else Money.ZERO) - (r.limit_price * r.qty + r.fee + slip)
                trades[m.ticker] = Trade(m.ticker, week_of(m.open_ts), minute, r.side, r.qty, r.limit_price,
                                         r.fee, slip, r.prob_bp, pnl, won)
                break
    return trades


def stats(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "pnl": Money.ZERO, "mean": 0.0, "t": 0.0, "cost": Money.ZERO}
    pnl = sum((t.pnl for t in trades), Money.ZERO)
    xs = [t.pnl.units / 1e6 for t in trades]
    mu = sum(xs) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    cost = sum((t.price * t.qty + t.fee + t.slippage for t in trades), Money.ZERO)
    return {"trades": n, "wins": sum(t.won for t in trades), "pnl": pnl, "mean": mu,
            "t": mu / (sd / math.sqrt(n)) if sd > 0 else 0.0, "cost": cost}


@dataclass
class WalkForwardResult:
    weeks: list[dict] = field(default_factory=list)
    oos: list[Trade] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    calibration_model: tuple | None = None
    verdict: str = ""
    passed: bool = False


GRID = {"vol_scale": ("0.9", "1.0", "1.1", "1.25", "1.4"), "z_min": ("1.5", "2.0", "2.5", "3.0"),
        "min_price": ("0.80", "0.88")}


def walk_forward(markets: list[MarketData], ref: Series, base: FlipConfig = FlipConfig(slippage_per_contract="0.01"),
                 min_train_weeks: int = 3, min_train_trades: int = 30, grid: dict = GRID) -> WalkForwardResult:
    configs = [replace(base, **dict(zip(grid, combo))) for combo in itertools.product(*grid.values())]
    by_config = {}
    for cfg in configs:                                       # evaluate every config once over all markets
        by_config[cfg] = run_config(CryptoFlipFavorite(cfg), markets, ref)
    weeks = sorted({week_of(m.open_ts) for m in markets})
    res = WalkForwardResult()
    for i, wk in enumerate(weeks):
        if i < min_train_weeks:
            continue
        train_weeks = set(weeks[:i])                          # strictly earlier weeks only
        best, best_pnl = None, None
        for cfg, trades in by_config.items():
            tr = [t for t in trades.values() if t.week in train_weeks]
            if len(tr) < min_train_trades:
                continue
            p = sum((t.pnl for t in tr), Money.ZERO)
            if best_pnl is None or p > best_pnl:
                best, best_pnl = cfg, p
        if best is None:
            res.weeks.append({"week": wk, "config": None, "trades": 0, "note": "no config had enough training trades"})
            continue
        test = [t for t in by_config[best].values() if t.week == wk]
        s = stats(test)
        res.oos += test
        res.weeks.append({"week": wk, "config": {k: getattr(best, k) for k in grid}, "train_pnl": best_pnl, **s})
    s = stats(res.oos)
    tested = [w for w in res.weeks if w.get("config")]
    share_ok = (sum(1 for w in tested if w["pnl"].units >= 0) / len(tested)) if tested else 0.0
    res.meta = {"oos": s, "share_weeks_non_negative": share_ok, "configs": len(configs), "weeks": len(weeks)}
    res.passed = (s["trades"] >= CRITERIA["min_oos_trades"] and s["mean"] > 0
                  and s["t"] >= CRITERIA["min_t_stat"] and share_ok >= CRITERIA["min_share_weeks_non_negative"])
    reasons = []
    if s["trades"] < CRITERIA["min_oos_trades"]:
        reasons.append(f"only {s['trades']} out-of-sample trades (needs {CRITERIA['min_oos_trades']})")
    if not (s["mean"] > 0 and s["t"] >= CRITERIA["min_t_stat"]):
        reasons.append(f"mean P&L {s['mean']:+.4f}/trade with t = {s['t']:.2f} (needs > 0 and t >= {CRITERIA['min_t_stat']})")
    if share_ok < CRITERIA["min_share_weeks_non_negative"]:
        reasons.append(f"{share_ok:.0%} of weeks non-negative (needs {CRITERIA['min_share_weeks_non_negative']:.0%})")
    res.verdict = "RELIABLE EDGE (by the pre-set criteria)" if res.passed else "NO RELIABLE EDGE: " + "; ".join(reasons)
    return res


def model_calibration(markets: list[MarketData], ref: Series, cfg: FlipConfig, minute: int = 12):
    """Model probability vs outcome (and the market's own price vs outcome) at one decision minute."""
    from ..analytics.probability import BP
    strat = CryptoFlipFavorite(replace(cfg, z_min="-99", min_price="0.00", max_price="1.00", max_spread="1.00",
                                       min_ev="-100", momentum_drop="1.00", jump_sigmas="1000"))
    model, market = [], []
    for m in markets:
        now = m.open_ts + minute * 60
        v = MarketView(now=now, market=m.ticker, event=m.event, module="kalshi", open_ts=m.open_ts,
                       close_ts=m.close_ts, strike=m.strike, fee_multiplier_pct=100,
                       quotes={k: s.cut(now) for k, s in m.quotes.items()}, reference=ref.cut(now))
        r = strat.evaluate(v)
        prob = r.prob_bp if isinstance(r, Signal) else r.details.get("prob_bp")
        q = m.quotes["yes"].cut(now).at(now)
        if prob is None or not q or q[1][0] is None or q[1][1] is None:
            continue
        favorite_yes = q[1][0] + q[1][1] >= ONE              # same rule the strategy uses
        p_yes = prob if favorite_yes else BP - prob
        y = 1 if m.result == "yes" else 0
        model.append((p_yes, y))
        market.append(((q[1][0] + q[1][1]).units * BP // (2 * ONE.units), y))
    return model, market
