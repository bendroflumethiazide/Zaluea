"""Download 1-minute BTC closes from Coinbase and Bitstamp for a date range.

    python research/collect_reference.py --days 126

Writes research/data/ref_coinbase.json and ref_bitstamp.json ({minute_start_ts: close}).
Numbers are kept as exact text.
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent / "data"


def get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "atlas-research/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read(), parse_float=str)
        except Exception:
            time.sleep(1 + 2 * i)
    raise RuntimeError(url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=126)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    end = int(time.time())
    start = end - a.days * 86_400
    cb, t = {}, start
    while t < end:
        t2 = min(end, t + 300 * 60)
        url = ("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60&start="
               + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) + "&end="
               + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t2)))
        for row in get(url) or []:
            cb[int(row[0])] = str(row[4])
        t = t2
        time.sleep(0.12)
    (OUT / "ref_coinbase.json").write_text(json.dumps(cb))
    print(f"coinbase minutes: {len(cb)}", flush=True)
    bs, t = {}, start
    while t < end:
        d = get(f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=60&limit=1000&start={t}") or {}
        data = (d.get("data") or {}).get("ohlc") or []
        if not data:
            t += 60_000
            continue
        for row in data:
            bs[int(row["timestamp"])] = row["close"]
        t = max(int(r["timestamp"]) for r in data) + 60
        time.sleep(0.12)
    (OUT / "ref_bitstamp.json").write_text(json.dumps(bs))
    print(f"bitstamp minutes: {len(bs)}", flush=True)


if __name__ == "__main__":
    main()
