"""SHADOW TRADING runner: strategies trade SIMULATED money against live PUBLIC Kalshi data.

    python -m atlas.shadow --data <ATLAS data folder>            (loops every 3 s)
    python -m atlas.shadow --data <folder> --report              (print the results so far)

It needs no API keys and cannot trade: it builds a Kalshi client WITHOUT a signer (every
private or order call fails before leaving the machine), has no gateway, and keeps all its
money in `<folder>/sim/`, physically separate from the real ledger.

Each strategy runs in two simulated accounts:
  as_live    exactly the real limits ($2 a purchase, $20 a day, stops after $3 of losses or
             3 losses in a row), so you see what live trading would really have done;
  research   the same per-purchase and per-market caps but no daily stops, so evidence keeps
             accumulating after a bad day.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ..clock import accounting_day
from ..exchange.kalshi import KalshiExchange
from ..limits import DEFAULTS, Limits
from ..marketdata.btc import BtcReference, recent_history
from ..money import Money, kalshi_worst_case_fee
from ..proposal import Proposal, Quote
from ..risk.primary import fee_multiplier_pct
from ..strategies.base import MarketView, Series, Signal
from ..strategies.crypto_flip import CryptoFlipFavorite
from ..strategies.crypto_maker import CryptoMaker
from .account import SIM_DIRNAME, SeparationError, SimAccount, research_limits
from .journal import Journal
from .report import build_state
from .sim import Book, PublicTrade

SERIES = "KXBTC15M"
PUBLIC_API = "https://api.elections.kalshi.com/trade-api/v2"     # public market data, same as the scanner
HISTORY_S = 2 * 3600
MARKET_REFRESH_S = 30
SETTLE_POLL_S = 30
SETTLE_GRACE_S = 60
TAKER_TTL_S = 2.0


class ShadowRunner:
    def __init__(self, data_dir: str | Path, kalshi=None, reference=None, history=recent_history,
                 strategies=None, clock=time.time, latency_s: float = 1.0, maker_fee_pct: int = 25,
                 limits: Limits = DEFAULTS):
        self.root = Path(data_dir) / SIM_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir)
        self.k = kalshi or KalshiExchange(PUBLIC_API, signer=None)
        if getattr(self.k, "signer", None) is not None:
            raise SeparationError("Shadow trading must use a Kalshi client WITHOUT an API key")
        self.btc = reference or BtcReference()
        self.history, self.clock = history, clock
        self.maker_fee_pct = maker_fee_pct
        self.strategies = strategies or [CryptoMaker(), CryptoFlipFavorite()]
        self.journal = Journal(self.root / "journal.db")
        self.accounts: list[tuple[object, SimAccount]] = []
        for s in self.strategies:
            kind = getattr(s, "kind", "taker")
            for profile, lim in (("as_live", limits), ("research", research_limits(limits))):
                acct = SimAccount(self.root / s.name / profile, f"{s.name}/{profile}", s.name, kind, lim,
                                  self.journal, latency_s=latency_s, maker_fee_pct=maker_fee_pct, clock=clock)
                self.accounts.append((s, acct))
        self.ref_points: list[tuple[float, Money]] = []
        self.bootstrapped = False
        self.ref_ok, self.ref_reason = False, "not checked yet"
        self.quote_hist: dict[str, dict[str, list]] = {}
        self.info: dict[str, dict] = {}
        self.last_trade_ts: dict[str, float] = {}
        self.feed_notes: dict[str, str] = {}
        self.latest: dict[str, dict] = {}
        self._markets_at = 0.0
        self._settle_at: dict[str, float] = {}
        self._recover()

    # ------------------------------------------------------------------ startup
    def _recover(self):
        """Simulated orders live in memory. After a restart their fate is unknown, so any order the
        ledger still shows as open is cancelled (nothing real is involved)."""
        now = self.clock()
        for s, acct in self.accounts:
            for o in acct.ledger.open_orders():
                acct.ledger.finalize(o["client_order_id"], "cancelled", note="simulator restarted")
                self.journal.add(ts=now, day=accounting_day(now), account=acct.name, strategy=s.name,
                                 market=o["market"], side=o["side"], kind="note", coid=o["client_order_id"],
                                 detail="Simulator restarted: open simulated order cancelled")

    # ------------------------------------------------------------------ data
    def _refresh_reference(self):
        if not self.bootstrapped:
            try:
                self.ref_points = list(self.history(100))
                self.bootstrapped = True
            except Exception as e:
                self.ref_ok, self.ref_reason = False, f"Could not load recent BTC history: {e}"
                return
        r = self.btc.get()
        if r.ok:
            self.ref_points.append((r.ts, r.price))
        cutoff = self.clock() - HISTORY_S
        self.ref_points = sorted((p for p in self.ref_points if p[0] >= cutoff), key=lambda p: p[0])
        self.ref_ok, self.ref_reason = r.ok, r.reason

    def _refresh_markets(self, now: float):
        if now - self._markets_at < MARKET_REFRESH_S:
            return
        self._markets_at = now
        for m in self.k.open_markets(SERIES, limit=5):
            if m["ticker"] not in self.info:
                self.info[m["ticker"]] = self.k.market_info(m["ticker"])

    def _market_data(self, market: str, now: float) -> list[PublicTrade]:
        b = self.k.book(market)
        h = self.quote_hist.setdefault(market, {"yes": [], "no": []})
        P = lambda x: Money.parse(x) if x else None
        for side in ("yes", "no"):
            s = b[side]
            bid, ask = P(s["bid"]), P(s["ask"])
            h[side].append((float(b["ts"]), (bid, ask, s["ask_size"])))
            h[side] = [p for p in h[side] if p[0] >= now - 900]
            for _, acct in self.accounts:       # each account gets its own copy (takers consume liquidity)
                acct.sim.update_book(market, side, Book(bid, ask, s["bid_size"], s["ask_size"], float(b["ts"])))
        since = self.last_trade_ts.get(market, now - 10)
        raw, complete = self.k.recent_trades(market, since - 2)
        trades = [PublicTrade(t["trade_id"], Money.parse(t["yes_price"]), t["count"], t["taker_side"], t["ts"])
                  for t in raw]
        if trades:
            self.last_trade_ts[market] = max(t.ts for t in trades)
        self.feed_notes[market] = "" if complete else \
            "Trade feed was incomplete this tick: some resting fills may be missed (pessimistic)"
        return trades

    # ------------------------------------------------------------------ one tick
    def tick(self) -> dict:
        now = self.clock()
        day = accounting_day(now)
        notes = []
        self._refresh_reference()
        try:
            self._refresh_markets(now)
        except Exception as e:
            notes.append(f"Market list unavailable: {e}")
        active = [m for m, i in self.info.items() if (i.get("open_ts") or i["close_ts"] - 900) <= now < i["close_ts"]]
        trades: dict[str, list[PublicTrade]] = {}
        for m in active:
            try:
                trades[m] = self._market_data(m, now)
            except Exception as e:
                notes.append(f"{m}: market data error: {e}")
        now = self.clock()                  # the simulator and the strategies act AFTER the data arrived
        for s, acct in self.accounts:
            for ev in acct.step(now, day, trades):
                self._journal_event(s, acct, ev, day)
        for m in active:
            if m in trades:
                self._decide(m, now, day)
        self._settle(now, day, notes)
        state = build_state(self, now, notes)
        tmp = self.root / "shadow.json.tmp"
        tmp.write_text(json.dumps(state, default=str))
        tmp.replace(self.root / "shadow.json")
        return state

    def _journal_event(self, s, acct: SimAccount, ev: dict, day: str):
        o = acct.sim.orders[ev["coid"]]
        self.journal.add(ts=ev["ts"], day=day, account=acct.name, strategy=s.name, market=o.market, side=o.side,
                         kind=ev["kind"], qty=ev["qty"], price=ev["price"], fee=ev["fee"], coid=ev["coid"])
        if ev["kind"] in ("cancelled", "expired") and ev["qty"] > 0:
            fee = acct.sim.maker_fee(ev["qty"], o.limit, o.fee_multiplier_pct) if o.kind == "maker" else \
                kalshi_worst_case_fee(ev["qty"], o.limit, o.fee_multiplier_pct)
            self.journal.add(ts=ev["ts"], day=day, account=acct.name, strategy=s.name, market=o.market,
                             side=o.side, kind="missed", qty=ev["qty"], price=o.limit, fee=fee, coid=ev["coid"],
                             detail=f"{ev['qty']} of {o.qty} not filled ({ev['kind']})")

    def _view(self, m: str, now: float) -> MarketView | None:
        info = self.info[m]
        pct = fee_multiplier_pct(info.get("fee_type"), info.get("fee_multiplier"))
        if pct is None:
            return None
        strike = Money.parse(str(info["strike"])) if info.get("strike") not in (None, "") else None
        h = self.quote_hist[m]
        quotes = {side: Series(sorted(pts, key=lambda p: p[0])).cut(now) for side, pts in h.items()}
        return MarketView(now=now, market=m, event=info["event"], module="kalshi",
                          open_ts=info.get("open_ts") or info["close_ts"] - 900, close_ts=info["close_ts"],
                          strike=strike, fee_multiplier_pct=pct, quotes=quotes,
                          reference=Series(self.ref_points).cut(now))

    def _decide(self, m: str, now: float, day: str):
        v = self._view(m, now)
        if v is None:
            return
        for s in self.strategies:
            r = s.evaluate(v)
            self.latest[s.name] = {"market": m, "ts": now, "signal": isinstance(r, Signal),
                                   "reasons": r.reasons}
            for strat, acct in self.accounts:
                if strat is not s:
                    continue
                resting = [o for o in acct.sim.open_orders() if o.market == m]
                if resting:
                    if acct.kind == "maker":
                        for o in resting:
                            if not isinstance(r, Signal) or r.side != o.side or r.limit_price < o.limit:
                                acct.cancel(o.coid, now)
                    continue
                if not isinstance(r, Signal) or m in acct.held_markets(day):
                    continue
                self._enter(s, acct, r, v, now, day)

    def _enter(self, s, acct: SimAccount, r: Signal, v: MarketView, now: float, day: str):
        q = v.quotes[r.side].at(now)
        bid, ask, ask_size = q[1]
        quote = Quote(v.market, r.side, bid, ask, ask_size or 0, q[0], "kalshi:orderbook")
        p = Proposal("kalshi", v.market, v.event, s.name, s.version, r.side, r.qty, r.limit_price, quote,
                     r.prob_bp, r.expected_value, "; ".join(r.reasons), r.exit_plan, v.fee_multiplier_pct)
        if acct.kind == "maker":
            expires = v.open_ts + s.config.cancel_min * 60
        else:
            expires = now + TAKER_TTL_S
        coid, why = acct.submit(p, now=now, day=day, expires_at=expires, kind=acct.kind)
        kind = "submitted" if coid else "rejected"
        self.journal.add(ts=now, day=day, account=acct.name, strategy=s.name, market=v.market, side=r.side,
                         kind=kind, qty=r.qty, price=r.limit_price, fee=r.fee, coid=coid,
                         detail={"reason": why, "prob_bp": r.prob_bp, "market_bp": r.market_prob_bp,
                                 "ev": str(r.expected_value), "order": acct.kind})

    def _settle(self, now: float, day: str, notes: list):
        pending = set(self.journal.unresolved_markets())
        for _, acct in self.accounts:
            pending |= acct.held_markets(day)
        for m in sorted(pending):
            try:
                info = self.info.get(m) or self.k.market_info(m)
            except Exception as e:
                notes.append(f"{m}: cannot look up market: {e}")
                continue
            if now < info["close_ts"] + SETTLE_GRACE_S or now - self._settle_at.get(m, 0) < SETTLE_POLL_S:
                continue
            self._settle_at[m] = now
            try:
                result = self.k.market_result(m)
            except Exception as e:
                notes.append(f"{m}: result unavailable: {e}")
                continue
            if result is None:
                continue
            for s, acct in self.accounts:
                for ev in acct.settle(m, result, day):
                    self.journal.add(ts=now, day=day, account=acct.name, strategy=s.name, market=m,
                                     side=ev["side"], kind="settled", qty=ev["qty"], price=ev["price"],
                                     fee=Money.ZERO, detail={"result": result, "pnl": str(ev["pnl"])})
            self.journal.resolve(m, result)
            self.info.pop(m, None)
            self.quote_hist.pop(m, None)

    def close(self):
        for _, acct in self.accounts:
            acct.close()
        self.journal.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true", help="print the shadow-trading report and exit")
    a = ap.parse_args(argv)
    if a.report:
        from .report import report_markdown
        print(report_markdown(a.data))
        return 0
    r = ShadowRunner(a.data)
    print("SHADOW TRADING: simulated money only, live public data, no API keys.", flush=True)
    while True:
        started = time.monotonic()
        try:
            st = r.tick()
            line = " | ".join(f"{x['account']}: {x['pnl_total']} ({x['settled']} settled, "
                              f"{x['open_orders']} open)" for x in st["accounts"])
            print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
        except Exception as e:                   # keep simulating; a data hiccup must not end the run
            print(f"{time.strftime('%H:%M:%S')} tick failed: {type(e).__name__}: {e}", flush=True)
        if a.once:
            return 0
        time.sleep(max(0.0, a.interval - (time.monotonic() - started)))
