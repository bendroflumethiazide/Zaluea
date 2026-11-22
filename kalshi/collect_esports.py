"""Download history for Kalshi esports match-winner and map-winner markets.

Saves to kalshi/data/:
  esports_markets.json   finalized markets with parsed scheduled start time
  esports_candles.json   1-minute [end_ts, yes_bid, yes_ask, volume] rows,
                         from 3h before scheduled start to close

Usage:  python collect_esports.py --days 120 [--reuse]
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from kalshi_client import KalshiPublic, fnum

DATA = Path(__file__).parent / "data"
ET = ZoneInfo("America/New_York")

# series -> (game, kind). "match" = whole series winner, "map" = one map/game.
SERIES = {
    "KXCS2GAME": ("CS2", "match"),
    "KXLOLGAME": ("LoL", "match"),
    "KXVALORANTGAME": ("Valorant", "match"),
    "KXDOTA2GAME": ("Dota 2", "match"),
    "KXR6GAME": ("Rainbow Six", "match"),
    "KXCODGAME": ("Call of Duty", "match"),
    "KXOWGAME": ("Overwatch", "match"),
    "KXRLGAME": ("Rocket League", "match"),
    "KXCS2MAP": ("CS2", "map"),
    "KXLOLMAP": ("LoL", "map"),
    "KXVALORANTMAP": ("Valorant", "map"),
    "KXDOTA2MAP": ("Dota 2", "map"),
}

SCHED = re.compile(r"originally scheduled for (\w{3} \d{1,2}, \d{4}) at (\d{1,2}:\d{2} [AP]M)")
PREGAME_LOOKBACK = 3 * 3600


def iso_ts(s: str) -> int:
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def scheduled_start(rules: str) -> int | None:
    m = SCHED.search(rules or "")
    if not m:
        return None
    dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %d, %Y %I:%M %p")
    return int(dt.replace(tzinfo=ET).timestamp())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--reuse", action="store_true",
                    help="reuse data/esports_markets.json instead of re-listing markets")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    k = KalshiPublic()
    since = int(time.time()) - args.days * 86400

    cached = DATA / "esports_markets.json"
    if args.reuse and cached.exists():
        markets = json.loads(cached.read_text())
        print(f"reusing {len(markets)} cached markets")
    else:
        markets = fetch_markets(k, since)
        cached.write_text(json.dumps(markets))
    fetch_candles(k, markets)


def fetch_markets(k: KalshiPublic, since: int) -> list[dict]:
    keep = ("ticker", "event_ticker", "title", "yes_sub_title", "open_time",
            "close_time", "result", "volume_fp", "rules_primary")
    markets = []
    for series, (game, kind) in SERIES.items():
        n = 0
        for m in k.markets(series_ticker=series, status="settled", min_close_ts=since):
            if m.get("result") not in ("yes", "no"):
                continue  # voided / fair-value settlements
            row = {f: m.get(f) for f in keep}
            row.update(series=series, game=game, kind=kind,
                       open_ts=iso_ts(m["open_time"]), close_ts=iso_ts(m["close_time"]),
                       start_ts=scheduled_start(m.get("rules_primary")))
            markets.append(row)
            n += 1
        print(f"{series:16s} {n:5d} markets")
    markets.sort(key=lambda m: m["close_ts"])
    return markets


def compact(m: dict, cs: list[dict]) -> list[list]:
    """Keep only this market's own window, as [end_ts, bid, ask, volume] rows.

    Batch requests share one time window, so without this trim each market
    carries hours of other markets' time and the file grows to gigabytes.
    """
    lo = max(m["open_ts"], m["start_ts"] - PREGAME_LOOKBACK)
    hi = m["close_ts"] + 60
    out = []
    for c in cs:
        t = c["end_period_ts"]
        if lo <= t <= hi:
            out.append([t, fnum(c["yes_bid"].get("close_dollars")),
                        fnum(c["yes_ask"].get("close_dollars")), fnum(c.get("volume_fp")) or 0.0])
    return out


def fetch_group(k: KalshiPublic, grp: list[dict]) -> dict[str, list]:
    """Candles for a group of markets; halves the group if the response would be too big."""
    lo = min(max(m["open_ts"], m["start_ts"] - PREGAME_LOOKBACK) for m in grp)
    hi = max(m["close_ts"] for m in grp) + 60
    lo = max(lo, hi - 4800 * 60)  # API caps candles per market (~5000)
    try:
        got = k.candlesticks([m["ticker"] for m in grp], lo, hi, period=1)
        return {m["ticker"]: compact(m, got.get(m["ticker"], [])) for m in grp}
    except (RuntimeError, requests.HTTPError) as e:
        if len(grp) == 1:
            print("  skip", grp[0]["ticker"], str(e)[:100], flush=True)
            return {}
        h = len(grp) // 2
        return {**fetch_group(k, grp[:h]), **fetch_group(k, grp[h:])}


def fetch_candles(k: KalshiPublic, markets: list[dict]) -> None:
    # The batch-candles endpoint is limited to ~4 requests/s, but accepts many
    # tickers per request, so fetch 10 markets with neighbouring times at once.
    k.min_interval = k.base_interval = 0.25
    want = sorted((m for m in markets if m["start_ts"]),
                  key=lambda m: m["start_ts"] - PREGAME_LOOKBACK)
    candles: dict[str, list] = {}
    B = 10
    for i in range(0, len(want), B):
        grp = want[i:i + B]
        candles.update(fetch_group(k, grp))
        if (i // B) % 250 == 0:
            print(f"  candles {i + len(grp)}/{len(want)} markets", flush=True)
    (DATA / "esports_candles.json").write_text(json.dumps(candles))


if __name__ == "__main__":
    main()
