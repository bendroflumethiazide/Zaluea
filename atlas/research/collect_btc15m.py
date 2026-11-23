"""Download BTC 15-minute market history for research (public data, no keys).

    python research/collect_btc15m.py --days 120

Writes research/data/ (git-ignored):
  btc15m_markets.json   settled markets: ticker, open/close, strike, result
  btc15m_quotes.json    per market: 1-minute [end_ts, yes_bid, yes_ask, volume] rows (strings, exact)
  ref_coinbase.json     {minute_start_ts: close}  BTC-USD 1-minute closes
  ref_bitstamp.json     {minute_start_ts: close}

Markets settled before Kalshi's historical cutoff come from /historical/*
(one candlestick request per market); newer ones from the live endpoints
(batched). Prices are kept as strings so nothing is rounded.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.elections.kalshi.com/trade-api/v2"
OUT = Path(__file__).parent / "data"
SERIES = "KXBTC15M"


class Http:
    def __init__(self, min_interval: float = 0.25):
        self.min_interval, self.last = min_interval, 0.0

    def get(self, url: str, tries: int = 8):
        for i in range(tries):
            wait = self.min_interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "atlas-research/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read(), parse_float=str)   # exact text, never floats
                self.min_interval = max(0.25, self.min_interval * 0.97)   # recover after rate limiting
                return data
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    self.min_interval = min(2.0, self.min_interval * 1.3)
                    time.sleep(1 + i)
                    continue
                if e.code == 404:
                    return None
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 + i)
        raise RuntimeError(f"gave up: {url}")


def iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def keep(m: dict) -> dict:
    return {"ticker": m["ticker"], "event": m["event_ticker"], "open_ts": iso(m["open_time"]),
            "close_ts": iso(m["close_time"]), "strike": m.get("floor_strike"), "result": m.get("result"),
            "expiration_value": m.get("expiration_value")}


def rows(candles: list) -> list:
    out = []
    for c in candles:
        out.append([c["end_period_ts"], (c.get("yes_bid") or {}).get("close_dollars"),
                    (c.get("yes_ask") or {}).get("close_dollars"), c.get("volume_fp")])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    http = Http()
    since = int(time.time()) - a.days * 86_400
    cutoff = iso(http.get(f"{BASE}/historical/cutoff")["market_settled_ts"])
    log(f"historical cutoff {datetime.fromtimestamp(cutoff, timezone.utc):%Y-%m-%d %H:%M}Z")

    markets, cursor = [], None
    while True:                                     # recent (live) settled markets
        q = {"series_ticker": SERIES, "status": "settled", "min_close_ts": since, "limit": 1000, "cursor": cursor}
        d = http.get(f"{BASE}/markets?" + urllib.parse.urlencode({k: v for k, v in q.items() if v}))
        markets += [keep(m) | {"archived": False} for m in d["markets"] if m.get("result") in ("yes", "no")]
        cursor = d.get("cursor")
        if not cursor:
            break
    log(f"{len(markets)} live settled markets")
    cursor, hist = None, []
    while True:                                     # archived markets
        q = {"series_ticker": SERIES, "limit": 1000, "cursor": cursor}
        d = http.get(f"{BASE}/historical/markets?" + urllib.parse.urlencode({k: v for k, v in q.items() if v}))
        batch = [keep(m) | {"archived": True} for m in d.get("markets", []) if m.get("result") in ("yes", "no")]
        hist += [m for m in batch if m["close_ts"] >= since]
        cursor = d.get("cursor")
        if not cursor:
            break
    log(f"{len(hist)} archived settled markets in range")
    seen = {m["ticker"] for m in markets}
    markets += [m for m in hist if m["ticker"] not in seen]
    markets.sort(key=lambda m: m["open_ts"])
    (OUT / "btc15m_markets.json").write_text(json.dumps(markets))

    quotes = {}
    qpath = OUT / "btc15m_quotes.json"
    if qpath.exists():
        quotes = json.loads(qpath.read_text())
    live = [m for m in markets if not m["archived"] and m["ticker"] not in quotes]
    for i in range(0, len(live), 10):
        grp = live[i:i + 10]
        url = f"{BASE}/markets/candlesticks?" + urllib.parse.urlencode({
            "market_tickers": ",".join(m["ticker"] for m in grp), "start_ts": grp[0]["open_ts"],
            "end_ts": grp[-1]["close_ts"], "period_interval": 1})
        d = http.get(url) or {}
        for mk in d.get("markets", []):
            quotes[mk["market_ticker"]] = rows(mk.get("candlesticks", []))
        if (i // 10) % 100 == 0:
            log(f"live quotes {i + len(grp)}/{len(live)}")
            qpath.write_text(json.dumps(quotes))
    archived = [m for m in markets if m["archived"] and m["ticker"] not in quotes]
    for i, m in enumerate(archived):
        url = (f"{BASE}/historical/markets/{m['ticker']}/candlesticks?start_ts={m['open_ts']}"
               f"&end_ts={m['close_ts']}&period_interval=1")
        d = http.get(url) or {}
        quotes[m["ticker"]] = rows(d.get("candlesticks", []))
        if i % 250 == 0:
            log(f"archived quotes {i}/{len(archived)} (pace {http.min_interval:.2f} s)")
            qpath.write_text(json.dumps(quotes))
    qpath.write_text(json.dumps(quotes))
    log(f"quotes for {len(quotes)} markets")

    start, end = markets[0]["open_ts"] - 7200, markets[-1]["close_ts"] + 120
    cb, t = {}, start
    while t < end:
        t2 = min(end, t + 300 * 60)
        url = ("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60&start="
               + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) + "&end="
               + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t2)))
        for row in http.get(url) or []:
            cb[int(row[0])] = str(row[4])
        t = t2
    (OUT / "ref_coinbase.json").write_text(json.dumps(cb))
    log(f"{len(cb)} coinbase minutes")
    bs, t = {}, start
    while t < end:
        url = f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=60&limit=1000&start={t}"
        d = http.get(url) or {}
        data = (d.get("data") or {}).get("ohlc") or []
        if not data:
            t += 60_000
            continue
        for row in data:
            bs[int(row["timestamp"])] = row["close"]
        t = max(int(r["timestamp"]) for r in data) + 60
    (OUT / "ref_bitstamp.json").write_text(json.dumps(bs))
    log(f"{len(bs)} bitstamp minutes. done.")


if __name__ == "__main__":
    sys.exit(main())
