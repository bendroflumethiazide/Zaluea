"""Real Kalshi Trade API v2 client, written against Kalshi's OpenAPI spec 3.37.0
(fetched 2026-11-23 from docs.kalshi.com/openapi.yaml).

It exposes the same interface as the fake exchange, so the gateway, the
shadow risk manager and the watchdog work unchanged:

    server_time, data_as_of, orderbook, market_info, balance, positions,
    resting_orders, fills, settlements, get_order, place_order, cancel

Rules this client follows (from the docs):
  * Requests are signed: timestamp(ms) + METHOD + full path without query,
    RSA-PSS/SHA-256 for RSA keys, plain Ed25519 for Ed25519 keys.
  * Orders use the V2 endpoint POST /portfolio/events/orders; the legacy
    /portfolio/orders write endpoints return 410 Gone.
  * V2 quotes everything on the YES leg: side "bid" = buy YES, "ask" = sell
    YES (= buy NO at 1 - price). ATLAS's (side, action) is translated here.
  * A 408, 5xx, timeout or dropped connection on a write means the outcome
    is UNKNOWN. POSTs are never retried. 409 order_already_exists means
    "look it up". 429 on a write means it was not processed.
  * Order, fill and position reads lag writes slightly; data_as_of()
    exposes GET /exchange/user_data_timestamp so callers can wait.
  * Only whole contracts. A fractional fill raises DataError.
  * No money-moving endpoints (transfers, withdrawals, subaccount
    transfers, API-key management) exist in this client. A test enforces it.
"""
from __future__ import annotations

import base64
import email.utils
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .errors import AuthError, DataError, DuplicateOrder, ExchangeError, OrderRejected

PROD = "https://external-api.kalshi.com/trade-api/v2"
DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
API_ROOT = "/trade-api/v2"
ESPORTS_TAGS = {"esports", "video games"}
LOOKBACK_S = 3 * 86_400          # how far back to search orders/fills by default
REJECT_CODES = {
    "invalid_order", "invalid_parameters", "invalid_price", "invalid_order_size", "insufficient_balance",
    "too_many_open_orders", "market_not_found", "insufficient_shard_balance", "fill_or_kill_insufficient_resting_volume",
    "market_inactive", "trading_is_paused", "exchange_is_paused", "TRADING_BLOCKED", "too_many_requests",
    "missing_parameters", "bad_request", "Cannot_specify_both_IoC_and_expiration_timestamp",
}


# --------------------------------------------------------------------------- helpers
def _units6(s: str) -> int:
    """Fixed-point dollar string (up to 6 decimals) -> integer millionths. No floats."""
    s = str(s).strip()
    neg = s.startswith("-")
    whole, _, frac = s.lstrip("-").partition(".")
    if len(frac) > 6 or not (whole or "0").isdigit() or (frac and not frac.isdigit()):
        raise DataError(f"Unexpected money format {s!r}")
    u = int(whole or "0") * 1_000_000 + int((frac + "000000")[:6])
    return -u if neg else u


def _s6(u: int) -> str:
    return f"{'-' if u < 0 else ''}{abs(u) // 1_000_000}.{abs(u) % 1_000_000:06d}"


def _whole(count_fp: str) -> int:
    """'3.00' -> 3. Fractional counts are refused: ATLAS only trades whole contracts."""
    s = str(count_fp).strip()
    neg = s.startswith("-")
    whole, _, frac = s.lstrip("-").partition(".")
    if frac.strip("0"):
        raise DataError(f"Fractional contract count {s!r}: ATLAS only accounts for whole contracts")
    n = int(whole or "0")
    return -n if neg else n


def _iso_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _s4_request(u: int) -> str:
    """Order REQUEST prices accept 2-4 decimals (responses use up to 6).
    Found by the live demo test: "invalid dollar precision: -6". A price that
    needs more than 4 decimals is refused here, never rounded."""
    if u % 100:
        raise OrderRejected(f"Price {_s6(u)} needs more than 4 decimals", "atlas_price_precision")
    return f"{u // 1_000_000}.{(u % 1_000_000) // 100:04d}"


def to_v2(side: str, action: str, price: str) -> tuple[str, str]:
    """ATLAS (side, action, price of that side) -> V2 (book side, YES-leg price, 4 decimals)."""
    p = _units6(price)
    if side == "yes":
        return ("bid" if action == "buy" else "ask"), _s4_request(p)
    if side == "no":
        return ("ask" if action == "buy" else "bid"), _s4_request(1_000_000 - p)
    raise ValueError(side)


# --------------------------------------------------------------------------- transport
class UrllibTransport:
    """Real HTTPS. Returns (status, headers, body_dict). Network failures raise
    TimeoutError / ConnectionError so callers can tell "unknown" from "rejected"."""

    def __call__(self, method: str, url: str, headers: dict, body: dict | None, timeout: float):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, dict(r.headers), (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"raw": raw[:300].decode(errors="replace")}
            return e.code, dict(e.headers or {}), payload
        except (socket.timeout, TimeoutError) as e:
            raise TimeoutError(f"{method} {url}: timed out") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError(f"{method} {url}: timed out") from e
            raise ConnectionError(f"{method} {url}: {e.reason}") from e


class Signer:
    def __init__(self, key_id: str, private_key_pem: bytes):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        self.key_id = key_id
        self.key = load_pem_private_key(private_key_pem, password=None)

    def headers(self, method: str, path: str, now_ms: int) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        msg = f"{now_ms}{method}{path.split('?')[0]}".encode()
        if isinstance(self.key, Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": str(now_ms),
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


# --------------------------------------------------------------------------- client
class KalshiExchange:
    def __init__(self, base_url: str = DEMO, signer: Signer | None = None, subaccount: int | None = None,
                 transport=None, clock=time.time, timeout: float = 10.0, min_interval: float = 0.12):
        if not base_url.endswith(API_ROOT):
            raise ValueError("base_url must end with /trade-api/v2")
        self.base = base_url
        self.signer = signer
        self.subaccount = subaccount
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.timeout = timeout
        self.min_interval = min_interval
        self._last = 0.0
        self._market_cache: dict[str, dict] = {}
        self._order_index: dict[str, dict] = {}          # order_id -> {client_order_id, side, action, market}
        self.last_server_date: float | None = None

    # ---------------------------------------------------------------- http
    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 auth: bool = True, retry: bool = True) -> tuple[dict, dict]:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        if auth and self.subaccount is not None and "subaccount" not in query and method == "GET" \
                and path.startswith("/portfolio"):
            query["subaccount"] = self.subaccount
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        attempts = 4 if (method == "GET" and retry) else 1          # writes are NEVER retried
        for attempt in range(attempts):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if auth:
                if self.signer is None:
                    raise AuthError("This client has no API key")
                headers |= self.signer.headers(method, API_ROOT + path, int(self.clock() * 1000))
            status, rh, payload = self.transport(method, url, headers, body, self.timeout)
            date = rh.get("Date") or rh.get("date")
            if date:
                try:
                    self.last_server_date = email.utils.parsedate_to_datetime(date).timestamp()
                except (TypeError, ValueError):
                    pass
            if 200 <= status < 300:
                return payload, rh
            code = (payload.get("error") or {}).get("code") or payload.get("code") or ""
            if status in (401,) or (status == 403 and "location" not in str(payload).lower()):
                raise AuthError(f"{method} {path}: HTTP {status} {code}")
            if method == "GET" and status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(0.5 * 2 ** attempt)
                continue
            if method == "GET" and status == 404:
                raise LookupError(code or "not_found")
            if method != "GET":
                if code == "order_already_exists":
                    raise DuplicateOrder(code)
                if status in (400, 404, 409, 410, 429) or status == 403:
                    raise OrderRejected(f"HTTP {status} {code}: {(payload.get('error') or payload).get('details', '')}", code)
                raise ExchangeError(f"HTTP {status} {code}: outcome unknown")
            raise ExchangeError(f"{method} {path}: HTTP {status} {code}")
        raise ExchangeError(f"{method} {path}: gave up after retries")

    def _paged(self, path: str, key: str, params: dict) -> list[dict]:
        out, cursor = [], None
        for _ in range(50):
            payload, _ = self._request("GET", path, params | {"limit": 200, "cursor": cursor})
            out += payload.get(key, [])
            cursor = payload.get("cursor")
            if not cursor:
                return out
        raise ExchangeError(f"{path}: too many pages")

    # ---------------------------------------------------------------- reads
    def server_time(self) -> float:
        """Exchange time from the Date header of a live response (Kalshi has no clock endpoint)."""
        payload, _ = self._request("GET", "/exchange/status", auth=False)
        if self.last_server_date is None:
            raise ExchangeError("Exchange response carried no Date header")
        if not payload.get("exchange_active", False):
            raise ExchangeError("Exchange is not active")
        return self.last_server_date

    def trading_active(self, market: str) -> bool:
        payload, _ = self._request("GET", "/exchange/status", auth=False)
        idx = self.market_info(market).get("exchange_index", 0)
        for s in payload.get("exchange_index_statuses") or []:
            if s.get("exchange_index") == idx:
                return bool(s.get("trading_active"))
        return bool(payload.get("trading_active"))

    def data_as_of(self) -> float:
        payload, _ = self._request("GET", "/exchange/user_data_timestamp")
        return _iso_ts(payload["as_of_time"])

    def market_info(self, market: str) -> dict:
        if market not in self._market_cache:
            m, _ = self._request("GET", f"/markets/{urllib.parse.quote(market)}", auth=False)
            m = m["market"]
            series = m["event_ticker"].split("-")[0]
            s, _ = self._request("GET", f"/series/{urllib.parse.quote(series)}", auth=False)
            s = s["series"]
            tags = {t.lower() for t in (s.get("tags") or [])} | {str(s.get("category", "")).lower()}
            self._market_cache[market] = {
                "market": market, "event": m["event_ticker"], "series": series,
                "module": "esports" if tags & ESPORTS_TAGS else "kalshi",
                "status": m["status"], "close_ts": _iso_ts(m["close_time"]),
                "exchange_index": m.get("exchange_index", 0), "market_type": m.get("market_type"),
                "fee_type": s.get("fee_type"), "fee_multiplier": str(s.get("fee_multiplier")),
                "price_level_structure": m.get("price_level_structure"), "price_ranges": m.get("price_ranges"),
                "rules_primary": m.get("rules_primary"),
                "strike": m.get("floor_strike"), "open_ts": _iso_ts(m["open_time"]) if m.get("open_time") else None,
                "result": m.get("result"),
            }
        return self._market_cache[market]

    def open_markets(self, series: str, limit: int = 20) -> list[dict]:
        """Open markets of a series (public), soonest-closing first."""
        payload, _ = self._request("GET", "/markets", {"series_ticker": series, "status": "open", "limit": limit},
                                   auth=False)
        return sorted(payload.get("markets", []), key=lambda m: m["close_time"])

    def market_result(self, market: str) -> str | None:
        """'yes'/'no' once settled (live or archived), else None. Not cached."""
        try:
            m, _ = self._request("GET", f"/markets/{urllib.parse.quote(market)}", auth=False)
        except LookupError:
            try:
                m, _ = self._request("GET", f"/historical/markets/{urllib.parse.quote(market)}", auth=False)
            except LookupError:
                return None
        r = (m.get("market") or {}).get("result")
        return r if r in ("yes", "no") else None

    def orderbook(self, market: str, side: str) -> dict:
        """Best bid/ask for one side. Kalshi publishes only BIDS for each leg:
        the YES ask is 1 - best NO bid (and vice versa)."""
        payload, _ = self._request("GET", f"/markets/{urllib.parse.quote(market)}/orderbook",
                                   {"depth": 10}, auth=False)
        received = self.clock()
        book = payload["orderbook_fp"]
        levels = {leg: [(_units6(p), _whole_or_floor(c)) for p, c in (book.get(f"{leg}_dollars") or [])]
                  for leg in ("yes", "no")}
        other = "no" if side == "yes" else "yes"
        own_bids, other_bids = levels[side], levels[other]
        bid = max((p for p, _ in own_bids), default=None)
        best_other = max((p for p, _ in other_bids), default=None)
        ask = (1_000_000 - best_other) if best_other is not None else None
        ask_size = sum(c for p, c in other_bids if p == best_other) if best_other is not None else 0
        return {"market": market, "side": side, "bid": _s6(bid) if bid is not None else None,
                "ask": _s6(ask) if ask is not None else None, "ask_size": ask_size, "ts": received,
                "source": "kalshi:orderbook"}

    def balance(self) -> str:
        payload, _ = self._request("GET", "/portfolio/balance")
        return payload["balance_dollars"]

    def positions(self) -> list[dict]:
        out = []
        for p in self._paged("/portfolio/positions", "market_positions", {"count_filter": "position"}):
            n = _whole(p["position_fp"])
            if n:
                out.append({"market": p["ticker"], "side": "yes" if n > 0 else "no", "qty": abs(n),
                            "cost": p["market_exposure_dollars"]})
        return out

    def _orders(self, market: str | None = None, status: str | None = None, since: float | None = None) -> list[dict]:
        params = {"ticker": market, "status": status,
                  "min_ts": int(since if since is not None else self.clock() - LOOKBACK_S)}
        orders = self._paged("/portfolio/orders", "orders", params)
        for o in orders:
            self._order_index[o["order_id"]] = o
        return orders

    def _view(self, o: dict) -> dict:
        side, action = self._intent(o)
        st = {"resting": "open", "canceled": "cancelled", "executed": "filled"}.get(o["status"], o["status"])
        filled = _whole(o["fill_count_fp"])
        qty = _whole(o["initial_count_fp"])
        if st == "cancelled" and filled == qty and qty:
            st = "filled"
        return {"client_order_id": o.get("client_order_id"), "order_id": o["order_id"], "market": o["ticker"],
                "side": side, "action": action, "qty": qty, "filled": filled,
                "remaining": _whole(o["remaining_count_fp"]), "status": st,
                "limit": o["yes_price_dollars"] if side == "yes" else o["no_price_dollars"],
                "fill_cost": o.get("taker_fill_cost_dollars"), "fees": o.get("taker_fees_dollars")}

    def _intent(self, o: dict) -> tuple[str, str]:
        """Recover ATLAS's (side, action) for an order. ATLAS records its own
        intent in the client_order_id prefix: atlas-<side>-<action>-<uuid>."""
        coid = o.get("client_order_id") or ""
        parts = coid.split("-")
        if len(parts) > 3 and parts[0] == "atlas" and parts[1] in ("yes", "no") and parts[2] in ("buy", "sell"):
            return parts[1], parts[2]
        outcome = o.get("outcome_side") or ("yes" if o.get("book_side") == "bid" else "no")
        return outcome, "buy"           # not an ATLAS order: reported as a buy of its outcome side

    def resting_orders(self) -> list[dict]:
        return [self._view(o) for o in self._orders(status="resting")]

    def get_order(self, client_order_id: str, market: str | None = None) -> dict | None:
        for o in self._orders(market=market):
            if o.get("client_order_id") == client_order_id:
                return self._view(o)
        return None                     # may also mean "not visible yet": callers must allow for read delay

    def fills(self, since_ts: float = 0.0) -> list[dict]:
        since = since_ts or (self.clock() - LOOKBACK_S)
        raw = self._paged("/portfolio/fills", "fills", {"min_ts": int(since)})
        missing = {f["order_id"] for f in raw} - set(self._order_index)
        if missing:
            self._orders(since=since - 3600)
        out = []
        for f in raw:
            o = self._order_index.get(f["order_id"], {})
            side, action = self._intent(o) if o else (f.get("outcome_side"), "buy")
            if o and f.get("outcome_side") and f["outcome_side"] != (side if action == "buy" else ("no" if side == "yes" else "yes")):
                raise DataError(f"Fill {f['fill_id']} direction does not match its order")
            ts = float(f["ts"]) if f.get("ts") is not None else _iso_ts(f["created_time"])
            out.append({"fill_id": f["fill_id"], "client_order_id": o.get("client_order_id"),
                        "market": f["ticker"], "side": side, "action": action, "qty": _whole(f["count_fp"]),
                        "price": f["yes_price_dollars"] if side == "yes" else f["no_price_dollars"],
                        "fee": f["fee_cost"], "ts": ts})
        return out

    def settlements(self, since_ts: float = 0.0) -> list[dict]:
        since = since_ts or (self.clock() - LOOKBACK_S)
        out = []
        for s in self._paged("/portfolio/settlements", "settlements", {"min_ts": int(since)}):
            result = s["market_result"]
            if result not in ("yes", "no"):
                raise DataError(f"Settlement of {s['ticker']} has result {result!r}: only binary markets are supported")
            for side in ("yes", "no"):
                qty = _whole(s[f"{side}_count_fp"])
                if qty:
                    payout = qty * 1_000_000 if side == result else 0
                    out.append({"settle_id": f"{s['ticker']}:{s['settled_time']}:{side}", "market": s["ticker"],
                                "side": side, "qty": qty, "payout": _s6(payout), "ts": _iso_ts(s["settled_time"])})
        return out

    # ---------------------------------------------------------------- writes
    def place_order(self, client_order_id: str, market: str, side: str, action: str, qty: int,
                    limit_price: str, tif: str = "ioc", expiration_ts: float | None = None) -> dict:
        if not isinstance(qty, int) or qty <= 0:
            raise OrderRejected("Quantity must be a positive whole number", "atlas_invalid_qty")
        if not client_order_id.startswith(f"atlas-{side}-{action}-"):
            raise OrderRejected("client_order_id must encode ATLAS's side and action", "atlas_bad_coid")
        book_side, price = to_v2(side, action, limit_price)
        body = {"ticker": market, "client_order_id": client_order_id, "side": book_side, "count": str(qty),
                "price": price, "self_trade_prevention_type": "taker_at_cross",
                "time_in_force": {"ioc": "immediate_or_cancel", "fok": "fill_or_kill",
                                  "gtc": "good_till_canceled"}[tif],
                "cancel_order_on_pause": True}
        if action == "sell":
            body["reduce_only"] = True
        if tif == "gtc" and expiration_ts:
            body["expiration_time"] = int(expiration_ts)
        if self.subaccount is not None:
            body["subaccount"] = self.subaccount
        payload, _ = self._request("POST", "/portfolio/events/orders", body=body)
        filled, remaining = _whole(payload["fill_count"]), _whole(payload["remaining_count"])
        status = "filled" if filled == qty else ("open" if remaining else "cancelled")
        return {"client_order_id": client_order_id, "order_id": payload["order_id"], "market": market,
                "side": side, "action": action, "qty": qty, "filled": filled, "remaining": remaining,
                "status": status, "limit": limit_price}

    def cancel(self, client_order_id: str, market: str | None = None) -> dict:
        o = self.get_order(client_order_id, market=market)
        if o is None:
            raise ExchangeError("Order not visible yet; cannot cancel by id")
        if o["status"] != "open":
            return o
        params = {"market_ticker": o["market"]}
        if self.subaccount is not None:
            params["subaccount"] = self.subaccount
        path = f"/portfolio/events/orders/{urllib.parse.quote(o['order_id'])}?" + urllib.parse.urlencode(params)
        self._request("DELETE", path)
        return o | {"status": "cancel_requested"}


def _whole_or_floor(count_fp: str) -> int:
    """Order book sizes may be fractional; available WHOLE contracts are the floor."""
    whole, _, _ = str(count_fp).partition(".")
    return int(whole or "0")


def load_signer(key_id: str, key_file: str | Path) -> Signer:
    return Signer(key_id, Path(key_file).read_bytes())
