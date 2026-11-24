"""Market scanner: runs strategies on live PUBLIC data and explains every verdict.

    python -m atlas.scanner --data <ATLAS data folder>          (loops every 10 s)
    python -m atlas.scanner --data <folder> --once

It needs no API keys and cannot trade: it has no gateway and no signing key.
Each scan it:
  * reads open BTC 15-minute markets, their order books and the multi-exchange
    BTC reference, and runs the crypto flip-favorite strategy;
  * records one prediction per market (the model's probability, the market's
    own price, sources) in predictions.db, and scores it once the market
    settles, so the model's real calibration builds up over time;
  * checks open esports markets' identities (no trading without a live feed);
  * writes scanner.json for the Hysilens dashboard.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .analytics.probability import BP, PredictionLog
from .exchange.kalshi import PROD, KalshiExchange
from .marketdata.btc import BtcReference, recent_history
from .marketdata.esports import identify
from .money import Money
from .risk.primary import fee_multiplier_pct
from .storage import ALREADY_RUNNING_EXIT, InstanceLock, StorageError, write_atomic
from .strategies.base import MarketView, Series, Signal
from .strategies.crypto_flip import CryptoFlipFavorite

ONE = Money.dollars(1)
CRYPTO_SERIES = ("KXBTC15M",)
ESPORTS_SERIES = ("KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME", "KXDOTA2GAME")
EQUITY_WATCHLIST = (("SPY", "etfs"), ("QQQ", "etfs"), ("AAPL", "stocks"), ("MSFT", "stocks"), ("NVDA", "stocks"))
SAFETY_RECHECK_S = 300
HISTORY_S = 2 * 3600


def cents(m: Money | None) -> float | None:
    return None if m is None else m.units / 10_000      # display only


class Scanner:
    def __init__(self, data_dir: str | Path, kalshi=None, reference=None, history=recent_history,
                 strategy=None, clock=time.time, alpaca=None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.k = kalshi or KalshiExchange("https://api.elections.kalshi.com/trade-api/v2", signer=None)
        self.btc = reference or BtcReference()
        self.history = history
        self.strategy = strategy or CryptoFlipFavorite()
        self.clock = clock
        self.log = PredictionLog(self.dir / "predictions.db", clock=clock)
        self.ref_points: list[tuple[float, Money]] = []
        self.quote_hist: dict[str, dict[str, list]] = {}
        self.predicted: set[str] = set()
        self.bootstrapped = False
        self.alpaca = alpaca                 # READ-ONLY use: quotes, clock, account safety check
        self._alpaca_safety: tuple[float, list[str]] | None = None
        self._clock_cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------------ data
    def _refresh_reference(self) -> tuple[bool, str]:
        if not self.bootstrapped:
            try:
                self.ref_points = list(self.history(100))
                self.bootstrapped = True
            except Exception as e:
                return False, f"Could not load recent BTC history: {e}"
        r = self.btc.get()
        if r.ok:
            self.ref_points.append((r.ts, r.price))
        cutoff = self.clock() - HISTORY_S
        self.ref_points = sorted((p for p in self.ref_points if p[0] >= cutoff), key=lambda p: p[0])
        return r.ok, r.reason

    def _quotes(self, market: str, now: float) -> dict:
        h = self.quote_hist.setdefault(market, {"yes": [], "no": []})
        for side in ("yes", "no"):
            b = self.k.orderbook(market, side)
            bid = Money.parse(b["bid"]) if b["bid"] else None
            ask = Money.parse(b["ask"]) if b["ask"] else None
            h[side].append((float(b["ts"]), (bid, ask, b["ask_size"])))
            h[side] = [p for p in h[side] if p[0] >= now - 900]
        return {side: Series(sorted(pts, key=lambda p: p[0])).cut(now) for side, pts in h.items()}

    # ------------------------------------------------------------------ scan
    def scan_once(self) -> dict:
        now = self.clock()
        rows, notes = [], []
        ref_ok, ref_reason = self._refresh_reference()
        reference = Series(self.ref_points).cut(now)
        for series in CRYPTO_SERIES:
            try:
                markets = self.k.open_markets(series, limit=5)
            except Exception as e:
                notes.append(f"{series}: {e}")
                continue
            for m in markets[:2]:
                rows.append(self._crypto_row(m["ticker"], now, reference, ref_ok, ref_reason))
        for series in ESPORTS_SERIES:
            try:
                for m in self.k.open_markets(series, limit=2)[:1]:
                    ok, why = identify(m["ticker"], m.get("rules_primary", "")).tradable
                    rows.append(self._row(m["ticker"], "esports", "-", None, None, None, None, None, "no_trade", why))
            except Exception as e:
                notes.append(f"{series}: {e}")
        rows += self._equity_rows(notes)
        self._resolve_predictions()
        sc = self.log.scorecard(strategy=self.strategy.name)
        out = {"ts": now, "rows": rows, "notes": notes, "reference_ok": ref_ok,
               "reference": cents(self.ref_points[-1][1]) / 100 if self.ref_points else None,
               "model": {"name": self.strategy.name, "version": self.strategy.version,
                         "scorecard": sc.summary(), "validated": sc.validated}}
        write_atomic(self.dir / "scanner.json", json.dumps(out))
        return out

    def _crypto_row(self, ticker: str, now: float, reference: Series, ref_ok: bool, ref_reason: str) -> dict:
        try:
            info = self.k.market_info(ticker)
            now = self.clock()                              # decide AFTER the quotes we just fetched...
            quotes = self._quotes(ticker, now + 5)          # ...collect them,
            now = max(now, max((s.at(now + 5) or (now,))[0] for s in quotes.values()))
            quotes = {k: s.cut(now) for k, s in quotes.items()}   # ...then cut everything at the decision time
            reference = Series(self.ref_points).cut(now)
        except Exception as e:
            return self._row(ticker, "kalshi", "-", None, None, None, None, None, "no_trade", f"Market data error: {e}")
        pct = fee_multiplier_pct(info.get("fee_type"), info.get("fee_multiplier"))
        if pct is None:
            return self._row(ticker, "kalshi", "-", None, None, None, None, None, "no_trade",
                             f"Fee type {info.get('fee_type')} cannot be computed exactly")
        strike = Money.parse(str(info["strike"])) if info.get("strike") not in (None, "") else None
        v = MarketView(now=now, market=ticker, event=info["event"], module="kalshi",
                       open_ts=info.get("open_ts") or info["close_ts"] - 900, close_ts=info["close_ts"],
                       strike=strike, fee_multiplier_pct=pct, quotes=quotes, reference=reference)
        r = self.strategy.evaluate(v)
        yq = quotes["yes"].at(now)
        bid, ask, size = yq[1] if yq else (None, None, 0)
        fav = "yes" if bid is not None and ask is not None and bid + ask >= ONE else "no"
        sq = quotes[fav].at(now)
        sb, sa, ss = sq[1] if sq else (None, None, 0)
        prob = r.prob_bp if isinstance(r, Signal) else r.details.get("prob_bp")
        if prob is not None and ticker not in self.predicted and (now - v.open_ts) / 60 >= 10:
            self.log.record(strategy=self.strategy.name, model_version=self.strategy.version, market=ticker,
                            market_type="crypto15m", side=fav, prob_bp=prob,
                            market_prob_bp=(sb + sa).units * BP // (2 * ONE.units) if sb and sa else None,
                            sources=["kalshi:orderbook", "btc:multi-exchange-median"])
            self.predicted.add(ticker)
        if isinstance(r, Signal):
            verdict, reason = "shadow", ("Signal: " + "; ".join(r.reasons) +
                                         ". Not sent anywhere: the scanner cannot trade.")
        else:
            verdict, reason = "no_trade", "; ".join(r.reasons)
        depth = round((cents(sa) or 0) / 100 * (ss or 0), 2) if sa else None
        return self._row(ticker, "kalshi", fav.upper(), cents(sa), cents(sb) and cents(sa) and (cents(sb) + cents(sa)) / 2,
                         prob / 100 if prob is not None else None,
                         cents(sa - sb) if sa is not None and sb is not None else None, depth, verdict, reason)

    def _equity_rows(self, notes: list) -> list[dict]:
        if self.alpaca is None:
            return [self._row("Stocks and ETFs", "stocks", "-", None, None, None, None, None, "no_trade",
                              "Not scanned: start the scanner with your Alpaca paper key (see README)")]
        from .limits import DEFAULTS
        now = self.clock()
        try:
            if self._alpaca_safety is None or now - self._alpaca_safety[0] > SAFETY_RECHECK_S:
                self._alpaca_safety = (now, self.alpaca.safety_problems())
            if self._clock_cache is None or now - self._clock_cache[0] > 60:
                self._clock_cache = (now, self.alpaca.market_clock())
        except Exception as e:
            notes.append(f"alpaca: {e}")
            return [self._row("Stocks and ETFs", "stocks", "-", None, None, None, None, None, "no_trade",
                              f"Alpaca unavailable: {type(e).__name__}")]
        problems = self._alpaca_safety[1]
        is_open = self._clock_cache[1]["is_open"]
        rows = []
        for symbol, category in EQUITY_WATCHLIST:
            try:
                q = self.alpaca.latest_quote(symbol)
            except Exception as e:
                rows.append(self._row(symbol, category, "BUY", None, None, None, None, None, "no_trade",
                                      f"Quote unavailable: {type(e).__name__}"))
                continue
            bid = Money.parse(q["bid"]) if q["bid"] else None
            ask = Money.parse(q["ask"]) if q["ask"] else None
            why = []
            if problems:
                why.append("Account not safe: " + "; ".join(problems))
            if not is_open:
                why.append("Market closed")
            if bid is None or ask is None:
                why.append("No two-sided quote")
            else:
                spread_bps = (ask - bid).units * 20_000 // (ask + bid).units
                if spread_bps > DEFAULTS.stock_max_spread_bps:
                    why.append(f"Spread {spread_bps / 100:.2f}% is above the "
                               f"{DEFAULTS.stock_max_spread_bps / 100:.2f}% limit (quote may be stale or thin)")
            why.append("No stock strategy yet: analysis only; execution arrives in Phase 6")
            price = ask.units / 1_000_000 if ask else None                      # display only
            spread = (ask - bid).units / 1_000_000 if ask and bid else None
            depth = round(price * q["ask_size"], 2) if price else None
            rows.append(self._row(symbol, category, "BUY", price, None, None, spread, depth, "no_trade",
                                  "; ".join(why) + f" (quote from {q['source']})"))
        return rows

    def _resolve_predictions(self):
        for r in self.log.con.execute("SELECT DISTINCT market FROM predictions WHERE outcome IS NULL").fetchall():
            try:
                info = self.k.market_info(r["market"])
                if info["close_ts"] > self.clock():
                    continue
                result = self.k.market_result(r["market"])
                if result:
                    self.log.resolve(r["market"], result)
            except Exception:
                continue

    @staticmethod
    def _row(market, category, side, price, market_pct, model_pct, spread, depth, verdict, reason) -> dict:
        return {"market": market, "category": category, "side": side, "price": price, "market_pct": market_pct,
                "model_pct": model_pct, "spread": spread, "depth_usd": depth, "verdict": verdict, "reason": reason}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--alpaca-key-id", help="Alpaca PAPER key id (read-only use)")
    ap.add_argument("--alpaca-secret-file", help="file holding the Alpaca PAPER secret")
    a = ap.parse_args(argv)
    from .check_connection import load_config
    saved = load_config()
    key_id = a.alpaca_key_id or saved.get("alpaca_key_id")
    secret_file = a.alpaca_secret_file or saved.get("alpaca_secret_file")
    alpaca = None
    if key_id and secret_file:
        from .exchange.alpaca import AlpacaClient
        alpaca = AlpacaClient(key_id, Path(secret_file).read_text().strip(), paper=True)
        print(f"Alpaca PAPER quotes enabled (key ...{key_id[-4:]}, read-only use)", flush=True)
    lock = InstanceLock(Path(a.data) / "scanner.lock")
    try:
        lock.acquire()
    except StorageError:
        print(f"A scanner is already running for {a.data}. Not starting a second one.", flush=True)
        return ALREADY_RUNNING_EXIT
    s = Scanner(a.data, alpaca=alpaca)
    while True:
        try:
            out = s.scan_once()
            print(f"{time.strftime('%H:%M:%S')} scanned {len(out['rows'])} markets; "
                  f"model: {out['model']['scorecard']}", flush=True)
        except Exception as e:              # a data hiccup must not end the scanner; the supervisor watches it
            print(f"{time.strftime('%H:%M:%S')} scan failed: {type(e).__name__}: {e}", flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
