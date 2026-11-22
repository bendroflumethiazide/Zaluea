"""Read-only client for Kalshi's public market-data API.

No API key is needed for anything in this file: it only reads markets,
quotes, candlesticks and trades. Trading endpoints (which need a signed
API key) are deliberately not included here.
"""
from __future__ import annotations

import time
from typing import Iterator

import requests

PROD = "https://api.elections.kalshi.com/trade-api/v2"
DEMO = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiPublic:
    def __init__(self, base_url: str = PROD, min_interval: float = 0.06):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.min_interval = min_interval  # stay well under the read rate limit
        self.base_interval = min_interval
        self._last = 0.0

    # -- low level -----------------------------------------------------
    def get(self, path: str, params: dict | None = None, retries: int = 10) -> dict:
        url = f"{self.base}{path}"
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                data = r.json()
                if "error" not in data:
                    # Throttled earlier? Drift back toward the base pace.
                    self.min_interval = max(self.base_interval, self.min_interval * 0.98)
                    return data
                raise RuntimeError(f"GET {path}: {data['error']}")
            if r.status_code == 429:
                # Adaptive: slow the steady pace down, then back off briefly.
                self.min_interval = min(2.0, self.min_interval * 1.5)
                time.sleep(1.0 + attempt)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(min(30, 1.5 * 2 ** attempt))
                continue
            r.raise_for_status()
        raise RuntimeError(f"GET {path} failed after {retries} attempts")

    # -- markets -------------------------------------------------------
    def series(self, ticker: str) -> dict:
        return self.get(f"/series/{ticker}")["series"]

    def markets(self, **params) -> Iterator[dict]:
        """Yield every market matching the filters, following the cursor."""
        params = {"limit": 1000, **params}
        while True:
            data = self.get("/markets", params)
            yield from data.get("markets", [])
            cur = data.get("cursor")
            if not cur:
                return
            params["cursor"] = cur

    def candlesticks(self, tickers: list[str], start_ts: int, end_ts: int,
                     period: int = 1) -> dict[str, list[dict]]:
        """Batch candlesticks (period in minutes: 1, 60 or 1440).

        Kalshi only emits a candle for periods where something changed,
        so callers must forward-fill.
        """
        data = self.get("/markets/candlesticks", {
            "market_tickers": ",".join(tickers),
            "start_ts": int(start_ts), "end_ts": int(end_ts),
            "period_interval": period,
        })
        return {m["market_ticker"]: m.get("candlesticks", [])
                for m in data.get("markets", [])}

    def trades(self, ticker: str, **params) -> Iterator[dict]:
        params = {"ticker": ticker, "limit": 1000, **params}
        while True:
            data = self.get("/markets/trades", params)
            yield from data.get("trades", [])
            cur = data.get("cursor")
            if not cur:
                return
            params["cursor"] = cur


def fnum(x) -> float | None:
    """Parse Kalshi's string-encoded numbers; '' / None -> None."""
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def taker_fee(price: float, mult: float = 1.0) -> float:
    """Kalshi 'quadratic' taker fee per contract: 0.07 * P * (1 - P).

    Kalshi rounds the fee for a whole order up to the next cent, so tiny
    orders pay a bit more than this per-contract figure.
    """
    return 0.07 * mult * price * (1.0 - price)
