"""Connection check for the owner's PC.

    python -m atlas.check_connection --kalshi-key-id ID --kalshi-key-file PATH
    python -m atlas.check_connection --alpaca-key-id ID --alpaca-secret-file PATH

Everything is READ-ONLY by default. Two optional, explicit extras:

  --demo-order-test      Kalshi DEMO only (refuses production): places ONE
                         1-contract buy at 1 cent, immediate-or-cancel, on a
                         side whose ask is far above 1 cent so it cannot fill,
                         then reads it back by client order id. Proves the
                         whole order path with fake money and no position.
  --apply-safe-settings  Alpaca: set long-only, no margin, no options on the
                         account. Asks you to type APPLY first.

Secrets are never printed: key ids are shown as their last 4 characters.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from .exchange.alpaca import AlpacaClient
from .exchange.kalshi import DEMO, PROD, KalshiExchange, load_signer
from .marketdata.btc import BtcReference

OK, BAD, INFO = "PASS", "FAIL", "INFO"


def line(tag: str, msg: str):
    print(f"[{tag}] {msg}", flush=True)


def masked(s: str) -> str:
    return "…" + s[-4:] if s else "(none)"


def check_kalshi(a) -> bool:
    base = DEMO if a.kalshi_env == "demo" else PROD
    line(INFO, f"Kalshi {a.kalshi_env.upper()} at {base}, key {masked(a.kalshi_key_id)}"
               + (f", subaccount {a.subaccount}" if a.subaccount is not None else ""))
    ok = True
    try:
        k = KalshiExchange(base, load_signer(a.kalshi_key_id, a.kalshi_key_file), subaccount=a.subaccount)
    except Exception as e:
        line(BAD, f"Could not load the private key file: {type(e).__name__}")
        return False
    try:
        skew = time.time() - k.server_time()
        line(OK if abs(skew) <= 2 else BAD, f"Clock vs Kalshi: {skew:+.2f} s (ATLAS needs within ±2 s)")
        ok &= abs(skew) <= 2
    except Exception as e:
        line(BAD, f"Exchange status: {e}")
        return False
    try:
        payload, _ = k._request("GET", "/portfolio/balance")
        line(OK, f"Signed request works. Available balance: ${payload['balance_dollars']}")
        for b in payload.get("balance_breakdown") or []:
            line(INFO, f"  exchange shard {b.get('exchange_index')}: ${b.get('balance_dollars', b.get('balance'))}")
        line(INFO, "  BTC/ETH 15-minute markets trade on shard 2: that shard needs its own balance.")
    except Exception as e:
        line(BAD, f"Signed request failed: {e}")
        return False
    for name, fn in (("positions", k.positions), ("resting orders", k.resting_orders)):
        try:
            line(OK, f"Read {name}: {len(fn())}")
        except Exception as e:
            line(BAD, f"Read {name}: {e}")
            ok = False
    try:
        lag = time.time() - k.data_as_of()
        line(OK, f"Account data is {lag:.1f} s behind live (normal is a few seconds)")
    except Exception as e:
        line(BAD, f"User data timestamp: {e}")
        ok = False
    if a.demo_order_test:
        ok &= demo_order_test(k, a)
    return ok


def demo_order_test(k: KalshiExchange, a) -> bool:
    if a.kalshi_env != "demo":
        line(BAD, "--demo-order-test only runs against the DEMO exchange. Refusing.")
        return False
    url = k.base + "/markets?series_ticker=KXBTC15M&status=open&limit=5"
    markets = json.load(urllib.request.urlopen(url, timeout=20)).get("markets", [])
    if not markets:
        line(BAD, "No open BTC 15-minute market on demo right now; try again in a few minutes.")
        return False
    m = markets[0]["ticker"]
    side = None
    for s in ("yes", "no"):
        book = k.orderbook(m, s)
        if book["ask"] is None or book["ask"] >= "0.050000":
            side = s
            break
    if side is None:
        line(BAD, "Both sides are priced under 5 cents; skipping to avoid any chance of a fill.")
        return False
    coid = f"atlas-{side}-buy-{uuid.uuid4()}"
    line(INFO, f"Placing 1 {side.upper()} at $0.01 immediate-or-cancel on {m} (demo, cannot fill)")
    try:
        r = k.place_order(coid, m, side, "buy", 1, "0.01", tif="ioc")
        line(OK, f"Order accepted: filled {r['filled']}, status {r['status']}")
    except Exception as e:
        line(BAD, f"Order request failed: {type(e).__name__}: {e}")
        return False
    for _ in range(15):                        # reads lag writes slightly
        o = k.get_order(coid, market=m)
        if o is not None:
            line(OK if o["filled"] == 0 and o["status"] == "cancelled" else BAD,
                 f"Read back by client order id: status {o['status']}, filled {o['filled']}")
            return o["filled"] == 0
        time.sleep(1)
    line(BAD, "Order was not visible by client order id after 15 s")
    return False


def check_alpaca(a) -> bool:
    secret = Path(a.alpaca_secret_file).read_text().strip() if a.alpaca_secret_file else ""
    c = AlpacaClient(a.alpaca_key_id, secret, paper=not a.alpaca_live)
    line(INFO, f"Alpaca {'LIVE' if a.alpaca_live else 'PAPER'}, key {masked(a.alpaca_key_id)}")
    try:
        acct = c.account()
        line(OK, f"Account {acct.get('status')}, cash ${acct.get('cash')}, margin multiplier {acct.get('multiplier')}")
    except Exception as e:
        line(BAD, f"Account: {e}")
        return False
    if a.apply_safe_settings:
        answer = input("Set this Alpaca account to long-only, no margin, no options? Type APPLY: ").strip()
        if answer == "APPLY":
            c.apply_safe_settings()
            line(OK, "Safe settings applied")
        else:
            line(INFO, "Not applied")
    problems = c.safety_problems()
    for p in problems:
        line(BAD, p)
    if not problems:
        line(OK, "Account is long-only, no margin, no options, not blocked")
    try:
        clk = c.market_clock()
        line(OK, f"Market is {'OPEN' if clk['is_open'] else 'closed'}")
        q = c.latest_quote("SPY")
        line(OK, f"SPY quote {q['bid']} / {q['ask']} from {q['source']}")
    except Exception as e:
        line(BAD, f"Market data: {e}")
        return False
    return not problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kalshi-key-id")
    ap.add_argument("--kalshi-key-file")
    ap.add_argument("--kalshi-env", choices=("demo", "prod"), default="demo")
    ap.add_argument("--subaccount", type=int)
    ap.add_argument("--demo-order-test", action="store_true")
    ap.add_argument("--alpaca-key-id")
    ap.add_argument("--alpaca-secret-file")
    ap.add_argument("--alpaca-live", action="store_true")
    ap.add_argument("--apply-safe-settings", action="store_true")
    a = ap.parse_args(argv)

    results = []
    r = BtcReference().get()
    line(OK if r.ok else BAD, f"BTC reference price: {r.price} ({r.reason})")
    results.append(r.ok)
    if a.kalshi_key_id and a.kalshi_key_file:
        results.append(check_kalshi(a))
    if a.alpaca_key_id and a.alpaca_secret_file:
        results.append(check_alpaca(a))
    if len(results) == 1:
        line(INFO, "No keys given: only public data was checked. See atlas/docs/04-connecting.md.")
    print("\nALL CHECKS PASSED" if all(results) else "\nSOME CHECKS FAILED (see FAIL lines above)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
