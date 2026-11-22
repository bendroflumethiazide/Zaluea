"""Download history for Kalshi's "BTC 15 min" markets (series KXBTC15M).

Saves three files to kalshi/data/:
  btc15m_markets.json   settled markets (strike, settle value, result)
  btc15m_candles.json   1-minute Kalshi quote/trade candles per market
  coinbase_btc_1m.json  Coinbase BTC-USD 1-minute candles (BRTI proxy)

Usage:  python collect_btc15m.py --days 30
"""
from __future__ import annotations

import argparse
import calendar
import json
import time
from pathlib import Path

import requests

from kalshi_client import KalshiPublic

DATA = Path(__file__).parent / "data"
SERIES = "KXBTC15M"


def coinbase_1m(start: int, end: int) -> list[list[float]]:
    """Coinbase Exchange public candles, 300 per request."""
    out: dict[int, list[float]] = {}
    s = requests.Session()
    t = start
    while t < end:
        t2 = min(end, t + 300 * 60)
        for attempt in range(6):
            r = s.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                      params={"granularity": 60,
                              "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                              "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t2))},
                      timeout=30)
            if r.status_code == 200:
                for row in r.json():
                    out[int(row[0])] = row
                break
            time.sleep(1.5 * 2 ** attempt)
        time.sleep(0.15)
        t = t2
    return [out[k] for k in sorted(out)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    k = KalshiPublic()

    now = int(time.time())
    since = now - args.days * 86400
    keep = ("ticker", "event_ticker", "open_time", "close_time", "floor_strike",
            "expiration_value", "result", "volume_fp", "open_interest_fp")
    markets = [
        {f: m.get(f) for f in keep}
        for m in k.markets(series_ticker=SERIES, status="settled", min_close_ts=since)
    ]
    markets = [m for m in markets if m["result"] in ("yes", "no")]
    for m in markets:
        m["open_ts"] = calendar.timegm(time.strptime(m["open_time"][:19], "%Y-%m-%dT%H:%M:%S"))
        m["close_ts"] = calendar.timegm(time.strptime(m["close_time"][:19], "%Y-%m-%dT%H:%M:%S"))
    markets.sort(key=lambda m: m["open_ts"])
    print(f"{len(markets)} settled markets")
    (DATA / "btc15m_markets.json").write_text(json.dumps(markets))

    candles: dict[str, list] = {}
    B = 20
    for i in range(0, len(markets), B):
        grp = markets[i:i + B]
        got = k.candlesticks([m["ticker"] for m in grp],
                             grp[0]["open_ts"], grp[-1]["close_ts"], period=1)
        candles.update(got)
        if (i // B) % 20 == 0:
            print(f"  candles {i + len(grp)}/{len(markets)}")
    (DATA / "btc15m_candles.json").write_text(json.dumps(candles))

    cb = coinbase_1m(markets[0]["open_ts"] - 3 * 3600, markets[-1]["close_ts"] + 120)
    print(f"{len(cb)} coinbase candles")
    (DATA / "coinbase_btc_1m.json").write_text(json.dumps(cb))


if __name__ == "__main__":
    main()
