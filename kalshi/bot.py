"""Kalshi BTC 15-minute favorite bot with flip protection.

Strategy (from the backtest in reports/btc15m.md, section 5):
  * Only in minutes 10-13 of each 15-minute market.
  * Buy the favorite side when its price is 0.80-0.97 ("best odds").
  * Flip guards - all must pass:
      1. BTC is at least Z_MIN standard deviations away from the strike,
         measured with Coinbase's recent 1-minute volatility.
      2. The model's probability is >= price + fee (we are not overpaying).
      3. The side's price has not fallen more than 5 cents over ~2 minutes
         (a flip already under way).
      4. BTC has not jumped toward the strike in the last ~30 seconds.
  * Stop-loss: if the side we hold can only be sold at <= 0.50, sell.
  * Each trade risks about $STAKE (default $5).

Modes:
  paper  (default) live prices, simulated fills, no API key needed
  demo   real orders on Kalshi's demo exchange (fake money)
  live   real orders with real money; needs --i-understand-real-money

Keys (demo/live): set KALSHI_KEY_ID and KALSHI_KEY_FILE (path to the RSA
private key .pem). Never commit the key.

Safety: daily loss cap, max trades per day, one position at a time, and a
kill switch - create a file named STOP next to this script to halt it.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from kalshi_client import DEMO, PROD, fnum, taker_fee

HERE = Path(__file__).parent
DATA = HERE / "data"
LOG = DATA / "bot_trades.jsonl"
STATE = DATA / "bot_state.json"        # what the bot is doing now (read by dashboard.py)
SETTINGS = DATA / "bot_settings.json"  # stake and limits (written by dashboard.py)
LIMITS = {"stake": (1.0, 25.0), "max_daily_loss": (1.0, 100.0), "max_trades": (1, 50)}
STOP_FILE = HERE / "STOP"
SERIES = "KXBTC15M"
CB = "https://api.exchange.coinbase.com/products/BTC-USD"

# Strategy parameters (chosen on the first half of the data, checked on the second)
ENTRY_MINUTES = (10.0, 14.0)
MIN_PRICE, MAX_PRICE = 0.80, 0.97
Z_MIN = 2.0
VOL_SCALE = 1.05
MOMENTUM_DROP = 0.05
STOP_PRICE = 0.50
JUMP_SIGMAS = 1.0


def now() -> float:
    return time.time()


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {msg}", flush=True)


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def iso_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# Kalshi access
# ---------------------------------------------------------------------------
class Kalshi:
    def __init__(self, base: str, key_id: str | None = None, key_file: str | None = None):
        self.base = base
        self.s = requests.Session()
        self.key_id = key_id
        self.key = None
        if key_file:
            from cryptography.hazmat.primitives import serialization
            self.key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)

    def _headers(self, method: str, path: str) -> dict:
        if not self.key:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(now() * 1000))
        sign_path = "/trade-api/v2" + path.split("?")[0]
        sig = self.key.sign((ts + method + sign_path).encode(),
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def req(self, method: str, path: str, params=None, body=None) -> dict:
        for attempt in range(5):
            r = self.s.request(method, self.base + path, params=params, json=body,
                               headers=self._headers(method, path), timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"{method} {path} kept failing")

    def current_market(self) -> dict | None:
        ms = self.req("GET", "/markets", {"series_ticker": SERIES, "status": "open", "limit": 10})
        ms = [m for m in ms.get("markets", []) if iso_ts(m["open_time"]) <= now() < iso_ts(m["close_time"])]
        return min(ms, key=lambda m: iso_ts(m["close_time"])) if ms else None

    def market(self, ticker: str) -> dict:
        return self.req("GET", f"/markets/{ticker}")["market"]

    def balance(self) -> float:
        return self.req("GET", "/portfolio/balance")["balance"] / 100.0

    def order(self, ticker: str, side: str, action: str, count: int, price: float) -> dict:
        """Immediate-or-cancel limit order. price is the price of `side` in dollars."""
        body = {"ticker": ticker, "side": side, "action": action, "count": int(count),
                "type": "limit", "time_in_force": "immediate_or_cancel",
                "client_order_id": str(uuid.uuid4()),
                f"{side}_price_dollars": f"{price:.4f}"}
        return self.req("POST", "/portfolio/orders", body=body)["order"]


# ---------------------------------------------------------------------------
# BTC reference price (Coinbase, a proxy for the CF Benchmarks index)
# ---------------------------------------------------------------------------
class Btc:
    def __init__(self):
        self.s = requests.Session()
        self.hist: list[tuple[float, float]] = []   # (ts, price) recent ticks
        self._cand: list = []
        self._cand_ts = 0.0

    def spot(self) -> float:
        p = float(self.s.get(f"{CB}/ticker", timeout=10).json()["price"])
        t = now()
        self.hist = [(a, b) for a, b in self.hist if t - a < 120] + [(t, p)]
        return p

    def candles(self) -> list:
        if now() - self._cand_ts > 30:
            self._cand = sorted(self.s.get(f"{CB}/candles", params={"granularity": 60}, timeout=10).json())
            self._cand_ts = now()
        return self._cand

    def sigma_1m(self) -> float:
        closes = [c[4] for c in self.candles()[-61:]]
        r = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        mu = sum(r) / len(r)
        return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1))

    def strike_proxy(self, open_ts: float) -> float | None:
        for c in self.candles():
            if int(c[0]) == int(open_ts) - 60:
                return (c[3] + c[4]) / 2
        return None

    def move_since(self, seconds: float) -> float | None:
        t = now()
        old = [p for a, p in self.hist if t - a >= seconds]
        return self.hist[-1][1] - old[-1] if old else None


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self, mode: str, stake: float, max_daily_loss: float, max_trades: int):
        self.mode = mode
        base = DEMO if mode == "demo" else PROD
        if mode == "paper":
            self.k = Kalshi(base)
        else:
            self.k = Kalshi(base, os.environ["KALSHI_KEY_ID"], os.environ["KALSHI_KEY_FILE"])
        self.btc = Btc()
        self.stake, self.max_daily_loss, self.max_trades = stake, max_daily_loss, max_trades
        self.pos: dict | None = None
        self.mid_hist: dict[str, list[tuple[float, float]]] = {}
        self.pending: list[dict] = []   # positions waiting for settlement
        self.check: dict = {}            # latest flip-guard results, for the dashboard
        self.market_view: dict = {}
        self.paused = ""
        self.started = now()
        self.last_error = ""
        DATA.mkdir(exist_ok=True)
        self.recover()

    def recover(self) -> None:
        """Pick up trades left open by a previous run (crash or restart)."""
        if not LOG.exists():
            return
        rows = [json.loads(x) for x in LOG.read_text().splitlines() if x.strip()]
        done = {r["ticker"] for r in rows if r["mode"] == self.mode and r["kind"] in ("settle", "stop")}
        for r in rows:
            if r["mode"] != self.mode or r["kind"] != "entry" or r["ticker"] in done:
                continue
            p = {k: r[k] for k in ("ticker", "side", "price", "p_model", "z", "minute", "spot",
                                   "strike", "close_ts", "count", "cost") if k in r}
            if now() >= p["close_ts"]:
                self.pending.append(p)
            else:
                self.pos = p
            log(f"recovered open trade {p['ticker']} {p['side'].upper()} x{p['count']}")

    # ---- dashboard link ----
    def load_settings(self) -> None:
        """Apply stake/limit changes saved by the dashboard, clamped to safe ranges."""
        try:
            cfg = json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            return
        for key, (lo, hi) in LIMITS.items():
            if key in cfg:
                try:
                    v = min(max(type(lo)(cfg[key]), lo), hi)
                except (TypeError, ValueError):
                    continue
                if v != getattr(self, key):
                    log(f"setting {key} -> {v}")
                    setattr(self, key, v)

    def write_state(self) -> None:
        rows = self.today_rows()
        closed = [r for r in rows if r["kind"] in ("settle", "stop")]
        state = dict(
            updated=now(), started=self.started, mode=self.mode, paused=self.paused,
            stopped_by_user=STOP_FILE.exists(), error=self.last_error,
            settings=dict(stake=self.stake, max_daily_loss=self.max_daily_loss,
                          max_trades=self.max_trades),
            limits={k: list(v) for k, v in LIMITS.items()},
            today=dict(trades=sum(r["kind"] == "entry" for r in rows),
                       wins=sum(r["pnl"] > 0 for r in closed),
                       losses=sum(r["pnl"] <= 0 for r in closed),
                       pnl=sum(r["pnl"] for r in closed)),
            market=self.market_view, check=self.check,
            position=({k: self.pos[k] for k in ("ticker", "side", "price", "count", "close_ts")}
                      if self.pos else None),
            waiting_settlement=len(self.pending),
        )
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE)

    # ---- bookkeeping ----
    def today_rows(self) -> list[dict]:
        if not LOG.exists():
            return []
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return [r for r in map(json.loads, LOG.read_text().splitlines())
                if r["mode"] == self.mode and r["day"] == day]

    def record(self, row: dict) -> None:
        row.update(mode=self.mode, day=datetime.now(timezone.utc).strftime("%Y-%m-%d"), ts=now())
        with LOG.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def can_trade(self) -> tuple[bool, str]:
        if STOP_FILE.exists():
            return False, "Paused by you"
        rows = self.today_rows()
        pnl = sum(r.get("pnl", 0.0) for r in rows if r["kind"] in ("settle", "stop"))
        entries = sum(1 for r in rows if r["kind"] == "entry")
        if pnl <= -self.max_daily_loss:
            return False, f"Daily loss limit reached ({pnl:+.2f} dollars). Resumes tomorrow (UTC)."
        if entries >= self.max_trades:
            return False, "Trade limit for today reached. Resumes tomorrow (UTC)."
        return True, ""

    # ---- the decision ----
    def evaluate(self, m: dict) -> dict | None:
        o, c = iso_ts(m["open_time"]), iso_ts(m["close_time"])
        minute = (now() - o) / 60
        bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        if bid is None or ask is None or bid <= 0 or ask >= 1:
            return None
        mid = (bid + ask) / 2
        h = self.mid_hist.setdefault(m["ticker"], [])
        h.append((now(), mid))

        if not (ENTRY_MINUTES[0] <= minute < ENTRY_MINUTES[1]):
            wait = (ENTRY_MINUTES[0] - minute) if minute < ENTRY_MINUTES[0] else (15 - minute)
            self.check = dict(stage="waiting", ticker=m["ticker"], minute=minute,
                              note=(f"Checks start at minute 10 (in {wait:.0f} min)"
                                    if minute < ENTRY_MINUTES[0] else "Too late to enter this window"))
            return None
        spot, k_cb, sig = self.btc.spot(), self.btc.strike_proxy(o), self.btc.sigma_1m()
        if k_cb is None or sig <= 0:
            self.check = dict(stage="waiting", ticker=m["ticker"], minute=minute,
                              note="Waiting for BTC price data")
            return None
        t_eff = max((c - now()) / 60 - 2 / 3, 1 / 3)
        z = math.log(spot / k_cb) / (VOL_SCALE * sig * math.sqrt(t_eff))
        p_yes = phi(z)

        fav_side, fav_px = ("yes", ask) if mid >= 0.5 else ("no", 1 - bid)
        self.check = dict(stage="checking", ticker=m["ticker"], minute=minute, side=fav_side,
                          price=fav_px, guards=[dict(
                              name="Favorite priced 80-97 cents",
                              ok=MIN_PRICE <= fav_px <= MAX_PRICE,
                              detail=f"{fav_side.upper()} costs {fav_px * 100:.1f} cents")])
        for side, px, p, zs in (("yes", ask, p_yes, z), ("no", 1 - bid, 1 - p_yes, -z)):
            if not (MIN_PRICE <= px <= MAX_PRICE):
                continue
            g = self.check["guards"]
            g.append(dict(name="BTC far from the strike", ok=zs >= Z_MIN,
                          detail=f"{zs:.1f} standard deviations away (needs {Z_MIN:.0f})"))
            g.append(dict(name="Price is fair", ok=p >= px + taker_fee(px),
                          detail=f"model {p * 100:.1f}% vs cost {100 * (px + taker_fee(px)):.1f}% with fee"))
            why = []
            if zs < Z_MIN:
                why.append(f"BTC only {zs:.2f} sd from strike (need {Z_MIN})")
            if p < px + taker_fee(px):
                why.append(f"model {p:.3f} < price+fee {px + taker_fee(px):.3f}")
            old = [v for t, v in h if now() - t >= 110]
            if old:
                drop = (old[-1] - mid) if side == "yes" else (mid - old[-1])
                if drop > MOMENTUM_DROP:
                    why.append(f"price fell {drop * 100:.0f}c in 2 min (flip under way)")
                g.append(dict(name="No flip under way", ok=drop <= MOMENTUM_DROP,
                              detail=f"price moved {-drop * 100:+.1f} cents in 2 minutes"))
            else:
                g.append(dict(name="No flip under way", ok=None, detail="collecting 2 minutes of prices"))
            mv = self.btc.move_since(30)
            if mv is not None:
                toward = -mv if side == "yes" else mv
                if toward > JUMP_SIGMAS * sig * spot:
                    why.append(f"BTC jumped ${toward:,.0f} toward strike in 30s")
                g.append(dict(name="No sudden BTC jump", ok=toward <= JUMP_SIGMAS * sig * spot,
                              detail=f"BTC moved ${toward:+,.0f} toward the strike in 30 s"))
            else:
                g.append(dict(name="No sudden BTC jump", ok=None, detail="collecting 30 seconds of prices"))
            if why:
                log(f"skip {side.upper()} @ {px:.3f} min {minute:.1f}: " + "; ".join(why))
                continue
            return dict(ticker=m["ticker"], side=side, price=px, p_model=p, z=zs,
                        minute=minute, spot=spot, strike=fnum(m.get("floor_strike")),
                        close_ts=c)
        return None

    # ---- execution ----
    def enter(self, sig: dict) -> None:
        px = sig["price"]
        count = int(self.stake // (px + taker_fee(px)))
        if count < 1:
            return
        if self.mode == "paper":
            filled, cost = count, px
        else:
            o = self.k.order(sig["ticker"], sig["side"], "buy", count, px)
            filled = int(fnum(o.get("fill_count_fp")) or o.get("fill_count") or 0)
            cost = px
            if filled == 0:
                log(f"order not filled ({o.get('status')})")
                return
        self.pos = dict(sig, count=filled, cost=cost)
        self.record(dict(kind="entry", **self.pos))
        log(f"BUY {filled} {sig['side'].upper()} {sig['ticker']} @ {px:.3f} "
            f"(model {sig['p_model']:.3f}, {sig['z']:.1f} sd, risk ${filled * (px + taker_fee(px)):.2f})")

    def manage(self) -> None:
        """Stop-loss check while holding; hand off to settlement after close."""
        p = self.pos
        if now() >= p["close_ts"]:
            self.pending.append(p)
            self.pos = None
            return
        m = self.k.market(p["ticker"])
        bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        self.market_view.update(yes_bid=bid, yes_ask=ask)
        if bid is None or ask is None:
            return
        sell_px = bid if p["side"] == "yes" else 1 - ask
        self.check = dict(stage="holding", ticker=p["ticker"], side=p["side"], price=sell_px,
                          note=(f"Holding {p['count']} {p['side'].upper()} contracts. "
                                f"They would sell for {sell_px * 100:.1f} cents now; "
                                f"stop-loss triggers at {STOP_PRICE * 100:.0f} cents."))
        if sell_px <= STOP_PRICE:
            log(f"FLIP: {p['side'].upper()} now sells at {sell_px:.3f} - stopping out")
            if self.mode != "paper":
                self.k.order(p["ticker"], p["side"], "sell", p["count"], max(sell_px - 0.02, 0.01))
            pnl = p["count"] * (sell_px - p["cost"] - taker_fee(p["cost"]) - taker_fee(sell_px))
            self.record(dict(kind="stop", ticker=p["ticker"], side=p["side"], count=p["count"],
                             exit=sell_px, pnl=pnl))
            self.pos = None

    def settle(self) -> None:
        still = []
        for p in self.pending:
            m = self.k.market(p["ticker"])
            if m.get("result") not in ("yes", "no"):
                still.append(p)
                continue
            won = m["result"] == p["side"]
            pnl = p["count"] * ((1.0 if won else 0.0) - p["cost"] - taker_fee(p["cost"]))
            self.record(dict(kind="settle", ticker=p["ticker"], side=p["side"], count=p["count"],
                             result=m["result"], pnl=pnl))
            log(f"SETTLED {p['ticker']} {'WON' if won else 'LOST'} {pnl:+.2f}")
            write_report()
        self.pending = still

    def run(self) -> None:
        log(f"mode={self.mode} stake=${self.stake} max daily loss=${self.max_daily_loss} "
            f"max trades/day={self.max_trades}")
        if self.mode != "paper":
            log(f"balance ${self.k.balance():,.2f}")
        while True:
            try:
                if self.pending:
                    self.settle()
                if self.pos:
                    self.manage()
                else:
                    ok, why = self.can_trade()
                    m = self.k.current_market()
                    if m:
                        self.market_view = dict(
                            ticker=m["ticker"], title=m.get("title"),
                            strike=fnum(m.get("floor_strike")),
                            open_ts=iso_ts(m["open_time"]), close_ts=iso_ts(m["close_time"]),
                            yes_bid=fnum(m.get("yes_bid_dollars")), yes_ask=fnum(m.get("yes_ask_dollars")))
                    if m and int(now()) // 60 != getattr(self, "_hb", None):
                        self._hb = int(now()) // 60
                        mi = (now() - iso_ts(m["open_time"])) / 60
                        log(f"watching {m['ticker']} min {mi:.1f} yes bid/ask "
                            f"{m.get('yes_bid_dollars')}/{m.get('yes_ask_dollars')}")
                    if m and ok:
                        if not any(r["ticker"] == m["ticker"] for r in self.today_rows() if r["kind"] == "entry"):
                            sig = self.evaluate(m)
                            if sig:
                                self.enter(sig)
                    if not ok and why != self.paused:
                        log(f"paused: {why}")
                    self.paused = "" if ok else why
                self.last_error = ""
            except Exception as e:  # keep running through transient errors
                log(f"error: {e}")
                self.last_error = str(e)[:200]
            try:
                self.load_settings()
                self.write_state()
            except Exception as e:
                log(f"state error: {e}")
            time.sleep(5)


def write_report() -> None:
    if not LOG.exists():
        return
    rows = [json.loads(x) for x in LOG.read_text().splitlines()]
    L = ["# Bot performance\n", "| Mode | Trades | Wins | Losses | Stop-outs | Net P&L |",
         "|---|---:|---:|---:|---:|---:|"]
    for mode in ("paper", "demo", "live"):
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue
        st = [r for r in rs if r["kind"] == "settle"]
        so = [r for r in rs if r["kind"] == "stop"]
        L.append(f"| {mode} | {sum(r['kind'] == 'entry' for r in rs)} | {sum(r['pnl'] > 0 for r in st)} | "
                 f"{sum(r['pnl'] <= 0 for r in st)} | {len(so)} | ${sum(r['pnl'] for r in st + so):+.2f} |")
    L += ["", "## Recent closed trades", "", "| Time (UTC) | Mode | Market | Side | Contracts | Outcome | P&L |",
          "|---|---|---|---|---:|---|---:|"]
    for r in [r for r in rows if r["kind"] in ("settle", "stop")][-30:][::-1]:
        t = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%m-%d %H:%M")
        out = "stop-loss" if r["kind"] == "stop" else ("won" if r["pnl"] > 0 else "lost")
        L.append(f"| {t} | {r['mode']} | {r['ticker']} | {r['side'].upper()} | {r['count']} | {out} | ${r['pnl']:+.2f} |")
    (HERE / "reports").mkdir(exist_ok=True)
    (HERE / "reports" / "bot_performance.md").write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("paper", "demo", "live"), default="paper")
    ap.add_argument("--stake", type=float, default=5.0, help="dollars risked per trade")
    ap.add_argument("--max-daily-loss", type=float, default=15.0)
    ap.add_argument("--max-trades", type=int, default=20, help="per UTC day")
    ap.add_argument("--i-understand-real-money", action="store_true")
    ap.add_argument("--report", action="store_true", help="only rebuild reports/bot_performance.md")
    a = ap.parse_args()
    if a.report:
        write_report()
        return
    if a.mode == "live" and not a.i_understand_real_money:
        raise SystemExit("live mode trades real money: add --i-understand-real-money")
    Bot(a.mode, a.stake, a.max_daily_loss, a.max_trades).run()


if __name__ == "__main__":
    main()
