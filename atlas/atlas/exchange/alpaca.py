"""Alpaca client for stocks and ETFs, written against docs.alpaca.markets
(fetched 2026-11-23).

Scope in Phase 3:
  * READS: account, account configuration, market clock, assets, positions,
    latest quotes (IEX feed, free for paper accounts), orders by client id.
  * SAFETY CHECK: ATLAS refuses to trade unless the account is ACTIVE, not
    blocked, long-only (no_shorting), with no margin (max_margin_multiplier
    "1") and options disabled. Your spec forbids margin, shorting and options.
  * ORDER BUILDING: limit orders only, time_in_force "day" (required for
    fractional shares), no extended hours. Execution of stock/ETF orders is
    held back by the gateway until the ledger supports exact fractional
    quantities (Phase 6).

Alpaca returns prices as JSON numbers (e.g. "ap": 387.7). This client parses
every JSON number as text, so a float never reaches ATLAS.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .errors import AuthError, DuplicateOrder, ExchangeError, OrderRejected

PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"
DATA = "https://data.alpaca.markets"
SAFE_SETTINGS = {"no_shorting": True, "max_margin_multiplier": "1", "max_options_trading_level": 0}


def _loads(raw: bytes):
    """JSON with every non-integer number kept as its exact text."""
    return json.loads(raw, parse_float=str) if raw else {}


def _iso(s: str) -> float:
    s = s.replace("Z", "+00:00")
    if "." in s:                                # Alpaca sends nanoseconds; Python keeps microseconds
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


class AlpacaTransport:
    def __call__(self, method, url, headers, body, timeout):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), _loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = _loads(raw)
            except ValueError:
                payload = {"message": raw[:300].decode(errors="replace")}
            return e.code, dict(e.headers or {}), payload
        except (socket.timeout, TimeoutError) as e:
            raise TimeoutError(f"{method} {url}: timed out") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError(f"{method} {url}: timed out") from e
            raise ConnectionError(f"{method} {url}: {e.reason}") from e


class AlpacaClient:
    def __init__(self, key_id: str, secret: str, paper: bool = True, transport=None, clock=time.time,
                 timeout: float = 10.0):
        self.trading = PAPER if paper else LIVE
        self.paper = paper
        self._auth = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self.transport = transport or AlpacaTransport()
        self.clock = clock
        self.timeout = timeout

    def _request(self, method: str, base: str, path: str, params: dict | None = None, body: dict | None = None):
        q = {k: v for k, v in (params or {}).items() if v is not None}
        url = base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        headers = {"Accept": "application/json", **self._auth}
        if body is not None:
            headers["Content-Type"] = "application/json"
        attempts = 3 if method == "GET" else 1                       # writes are never retried
        for attempt in range(attempts):
            status, _, payload = self.transport(method, url, headers, body, self.timeout)
            if 200 <= status < 300:
                return payload
            msg = payload.get("message", "") if isinstance(payload, dict) else str(payload)
            if status in (401,):
                raise AuthError(f"{method} {path}: HTTP 401 {msg}")
            if method == "GET" and status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(0.5 * 2 ** attempt)
                continue
            if method == "GET" and status == 404:
                raise LookupError(msg or "not found")
            if method != "GET":
                if status == 422 and "client_order_id" in msg.lower() and "unique" in msg.lower():
                    raise DuplicateOrder(msg)
                if status in (400, 403, 404, 422, 429):
                    raise OrderRejected(f"HTTP {status}: {msg}", str(payload.get("code", "")))
                raise ExchangeError(f"HTTP {status}: outcome unknown ({msg})")
            if status == 403:
                raise AuthError(f"{method} {path}: HTTP 403 {msg}")
            raise ExchangeError(f"{method} {path}: HTTP {status} {msg}")
        raise ExchangeError(f"{method} {path}: gave up after retries")

    # ------------------------------------------------------------------ reads
    def account(self) -> dict:
        return self._request("GET", self.trading, "/v2/account")

    def account_config(self) -> dict:
        return self._request("GET", self.trading, "/v2/account/configurations")

    def market_clock(self) -> dict:
        c = self._request("GET", self.trading, "/v2/clock")
        return {"is_open": bool(c["is_open"]), "ts": _iso(c["timestamp"]),
                "next_open": _iso(c["next_open"]), "next_close": _iso(c["next_close"])}

    def asset(self, symbol: str) -> dict:
        return self._request("GET", self.trading, f"/v2/assets/{urllib.parse.quote(symbol)}")

    def positions(self) -> list[dict]:
        out = []
        for p in self._request("GET", self.trading, "/v2/positions"):
            out.append({"symbol": p["symbol"], "side": p["side"], "qty": p["qty"],
                        "cost_basis": p["cost_basis"], "avg_entry_price": p["avg_entry_price"]})
        return out

    def latest_quote(self, symbol: str, feed: str = "iex") -> dict:
        d = self._request("GET", DATA, f"/v2/stocks/{urllib.parse.quote(symbol)}/quotes/latest", {"feed": feed})
        q = d["quote"]
        bid, ask = str(q["bp"]), str(q["ap"])
        return {"symbol": symbol, "bid": None if bid in ("0", "0.0") else bid,
                "ask": None if ask in ("0", "0.0") else ask, "bid_size": int(q["bs"]), "ask_size": int(q["as"]),
                "ts": _iso(q["t"]), "received_ts": self.clock(), "source": f"alpaca:{feed}"}

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            return self._request("GET", self.trading, "/v2/orders:by_client_order_id",
                                 {"client_order_id": client_order_id})
        except LookupError:
            return None

    # ------------------------------------------------------------------ safety
    def safety_problems(self) -> list[str]:
        """Everything that must be true before ATLAS may trade this account."""
        problems = []
        a = self.account()
        c = self.account_config()
        if a.get("status") != "ACTIVE":
            problems.append(f"Account status is {a.get('status')}")
        for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
            if a.get(flag):
                problems.append(f"Account has {flag}")
        if str(a.get("multiplier")) != "1":
            problems.append(f"Account margin multiplier is {a.get('multiplier')} (ATLAS requires 1: no margin)")
        if a.get("shorting_enabled"):
            problems.append("Short selling is enabled (ATLAS requires long-only)")
        if c.get("no_shorting") is not True:
            problems.append("Configuration no_shorting is not true")
        if str(c.get("max_margin_multiplier")) != "1":
            problems.append(f"Configuration max_margin_multiplier is {c.get('max_margin_multiplier')} (needs 1)")
        if int(c.get("max_options_trading_level") or 0) != 0:
            problems.append("Options trading is enabled (ATLAS requires it off)")
        if c.get("suspend_trade"):
            problems.append("Trading is suspended in the account configuration")
        return problems

    def apply_safe_settings(self) -> dict:
        """OWNER ACTION ONLY (connection-check script, with confirmation):
        long-only, no margin, no options. ATLAS never calls this by itself."""
        return self._request("PATCH", self.trading, "/v2/account/configurations", body=dict(SAFE_SETTINGS))

    # ------------------------------------------------------------------ orders
    @staticmethod
    def order_body(symbol: str, side: str, qty: str, limit_price: str, client_order_id: str) -> dict:
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if len(client_order_id) > 128:
            raise ValueError("client_order_id too long")
        return {"symbol": symbol, "qty": qty, "side": side, "type": "limit", "time_in_force": "day",
                "limit_price": limit_price, "client_order_id": client_order_id, "extended_hours": False,
                "position_intent": "buy_to_open" if side == "buy" else "sell_to_close"}

    def place_order(self, body: dict) -> dict:
        return self._request("POST", self.trading, "/v2/orders", body=body)

    def cancel(self, order_id: str) -> None:
        self._request("DELETE", self.trading, f"/v2/orders/{urllib.parse.quote(order_id)}")
