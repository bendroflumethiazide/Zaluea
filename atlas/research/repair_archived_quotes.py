"""Re-download quotes for archived markets whose saved quotes are all empty.

The first collection read the archive's candles with the live API's field names
(`close_dollars`), but the archive calls them `close`, so every archived bid/ask
was stored as empty. rows() now reads both; this re-fetches only those markets.

    python research/repair_archived_quotes.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_btc15m import BASE, OUT, Http, log, rows  # noqa: E402


def empty(r: list) -> bool:
    return not r or all(x[1] is None and x[2] is None for x in r)


def main():
    markets = {m["ticker"]: m for m in json.loads((OUT / "btc15m_markets.json").read_text())}
    qpath = OUT / "btc15m_quotes.json"
    quotes = json.loads(qpath.read_text())
    todo = [t for t, r in quotes.items() if empty(r) and markets.get(t, {}).get("archived")]
    log(f"repair: {len(todo)} archived markets with empty quotes")
    http = Http()
    fixed = 0
    for i, t in enumerate(todo):
        m = markets[t]
        d = http.get(f"{BASE}/historical/markets/{t}/candlesticks?start_ts={m['open_ts']}"
                     f"&end_ts={m['close_ts']}&period_interval=1") or {}
        r = rows(d.get("candlesticks", []))
        quotes[t] = r
        fixed += not empty(r)
        if i % 250 == 0:
            log(f"repair {i}/{len(todo)} ({fixed} now have quotes; pace {http.min_interval:.2f} s)")
            qpath.write_text(json.dumps(quotes))
    qpath.write_text(json.dumps(quotes))
    log(f"repair done: {fixed}/{len(todo)} markets now have quotes")


if __name__ == "__main__":
    main()
