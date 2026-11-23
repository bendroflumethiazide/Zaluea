"""Sample data for the ATLAS dashboard preview.

This module defines the *shape* of the dashboard's state (the contract the
real engine, gateway and risk managers will fill in later phases) and
fills it with clearly labelled sample values.

Nothing here talks to an exchange or broker. All money is integer cents.
The story the sample tells is consistent with today's real status: live
trading is NOT authorized, so the real account shows no activity, while
shadow trading (simulated) keeps running.
"""
from __future__ import annotations

import itertools
import random
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FEED_MAX = 60

# Hard limits from the ATLAS spec (cents / counts).
LIMITS = {
    "daily_spend_c": 2000, "max_order_c": 200, "max_market_c": 300,
    "max_daily_loss_c": 300, "max_positions": 3, "max_open_orders": 3,
    "max_consec_losses": 3,
    "category_c": {"kalshi": 1000, "stocks": 1000, "etfs": 1000},
}

SCANNER = [
    # market, category, side, price (¢), market prob, model prob, spread ¢, depth $, verdict, reason
    ("BTC up at 12:45 AM?", "kalshi", "YES", 96.0, 96.0, 97.1, 0.1, 4210, "no_trade",
     "Price 96¢ is above the 97¢ cap after fees: edge +0.4¢ is below the 1¢ minimum."),
    ("BTC up at 1:00 AM?", "kalshi", "NO", 54.0, 54.0, 52.8, 1.0, 1830, "no_trade",
     "Minute 3 of 15: checks only run in minutes 10–13."),
    ("ETH up at 12:45 AM?", "kalshi", "YES", 88.0, 88.0, 91.9, 1.0, 960, "shadow",
     "All checks pass in simulation. Live trading not authorized today."),
    ("CS2: Vitality vs FaZe", "esports", "YES", 63.0, 63.0, None, 3.0, 412, "no_trade",
     "No reliable live game-state feed: esports trading disabled."),
    ("LoL: T1 vs Gen.G (map 2)", "esports", "NO", 41.0, 41.0, None, 6.0, 95, "no_trade",
     "Spread 6¢ exceeds the 4¢ maximum; depth below $150."),
    ("SPY", "etfs", "BUY", 591.42, None, None, 0.01, 250000, "no_trade",
     "Market closed (opens 9:30 AM ET)."),
    ("QQQ", "etfs", "BUY", 512.88, None, None, 0.01, 180000, "no_trade",
     "Overlaps 48% with your tech group; module disabled."),
    ("AAPL", "stocks", "BUY", 231.10, None, None, 0.02, 90000, "no_trade",
     "Momentum signal is not out-of-sample validated yet (shadow only)."),
    ("NVDA", "stocks", "BUY", 142.37, None, None, 0.03, 120000, "no_trade",
     "Up 6.2% today: chasing filter rejects sudden moves."),
]

FEED_TEMPLATES = [
    ("scan", "Scan complete", "Checked {n} markets in {ms} ms. 0 live opportunities."),
    ("reject", "NO TRADE: BTC 15-min", "Favorite at {p}¢ is outside the 80–97¢ range."),
    ("shadow", "Shadow fill (simulated)", "Bought {q} ETH YES at {p}¢ with simulated slippage +0.3¢."),
    ("risk", "Risk check", "Primary and shadow risk managers agree: budget ${b} remaining."),
    ("recon", "Reconciliation OK", "Kalshi positions, orders and balance match the ledger."),
    ("reject", "NO TRADE: E-Sports", "Live score feed unavailable: event disabled."),
    ("health", "Watchdog heartbeat", "All 6 processes healthy; audit chain verified."),
    ("shadow", "Shadow settle (simulated)", "ETH YES settled WON: +${w} simulated."),
]


def _curve(seed=7, n=48, start=10000):
    rnd = random.Random(seed)
    v, out = start, []
    for _ in range(n):
        v += rnd.choice([3, 4, 5, 6, -2, -1, 8, -180 if rnd.random() < 0.04 else 4])
        out.append(v)
    return out


class SampleState:
    """Thread-safe sample state with a feed that grows over time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.controls = {
            "master": True, "authorized_today": False, "require_approval": True,
            "paper": True, "shadow": True, "emergency_stop": False,
            "modules": {"kalshi": True, "esports": False, "stocks": False, "etfs": False},
        }
        self.feed: list[dict] = []
        self.ids = itertools.count(1)
        self.rnd = random.Random()
        self.started = time.time()
        self.example_approval_open = True
        for _ in range(9):
            self._add_random_event(backdate=True)

    # ---- feed ----
    def add_event(self, kind: str, title: str, detail: str, important: bool = False):
        with self.lock:
            self.feed.insert(0, dict(id=next(self.ids), t=time.time(), kind=kind,
                                     title=title, detail=detail, important=important))
            del self.feed[FEED_MAX:]

    def _add_random_event(self, backdate=False):
        kind, title, detail = self.rnd.choice(FEED_TEMPLATES)
        detail = detail.format(n=self.rnd.randint(24, 31), ms=self.rnd.randint(180, 420),
                               p=self.rnd.choice([98, 99, 76, 88, 91]), q=self.rnd.randint(1, 2),
                               b="20.00", w=f"{self.rnd.randint(8, 24) / 100:.2f}")
        self.add_event(kind, title, detail)
        if backdate:
            self.feed[0]["t"] -= self.rnd.randint(30, 900)
            self.feed.sort(key=lambda e: -e["t"])

    def tick(self):
        """Called periodically by the server to make the preview feel alive."""
        if not self.controls["emergency_stop"]:
            self._add_random_event()

    # ---- controls (preview only: they change sample state, nothing else) ----
    def control(self, action: str, value) -> tuple[bool, str]:
        c = self.controls
        names = {"master": "Master trading switch", "authorized_today": "Today's trading budget",
                 "require_approval": "Approval for every live trade", "paper": "Paper trading",
                 "shadow": "Shadow trading"}
        if action in names:
            c[action] = bool(value)
            state = "ON" if value else "OFF"
            self.add_event("control", f"{names[action]}: {state}",
                           "Preview only: no real account was affected.", important=True)
            return True, f"{names[action]} turned {state} (preview)."
        if action.startswith("module:"):
            m = action.split(":", 1)[1]
            if m not in c["modules"]:
                return False, "Unknown module."
            c["modules"][m] = bool(value)
            label = {"kalshi": "Kalshi", "esports": "E-Sports", "stocks": "Stocks", "etfs": "ETFs"}[m]
            self.add_event("control", f"{label} module: {'ON' if value else 'OFF'}",
                           "Preview only.", important=True)
            return True, f"{label} turned {'on' if value else 'off'} (preview)."
        if action == "emergency_stop":
            c["emergency_stop"] = bool(value)
            if value:
                c["authorized_today"] = False
                self.add_event("stop", "STOP ALL NEW TRADING",
                               "New entries blocked, entry orders cancelled, positions preserved. "
                               "Preview only.", important=True)
                return True, "All new trading stopped (preview)."
            self.add_event("control", "Emergency stop cleared",
                           "Trading authorization stays OFF until you turn it on.", important=True)
            return True, "Emergency stop cleared. Authorization is still off (preview)."
        if action in ("approve_example", "reject_example"):
            self.example_approval_open = False
            verb = "approved" if action == "approve_example" else "rejected"
            self.add_event("control", f"Example trade {verb}",
                           "This was a sample approval; no order exists.", important=True)
            return True, f"Example trade {verb} (preview)."
        return False, "Unknown action."

    # ---- snapshot ----
    def snapshot(self) -> dict:
        now = time.time()
        et = datetime.now(ET)
        c = self.controls
        curve = _curve()
        shadow_bal = curve[-1]
        with self.lock:
            feed = list(self.feed[:40])
        return {
            "preview": True,
            "now": now,
            "clock": {"et": et.strftime("%a %b %d, %I:%M %p ET").replace(" 0", " "),
                      "accounting_day": et.strftime("%Y-%m-%d"), "synced": True},
            "controls": c,
            "limits": LIMITS,
            # Real account: live trading not authorized, so no activity today.
            "account": {"cash_c": 5000, "portfolio_c": 5000, "exposure_c": 0, "purchases_c": 0,
                        "fees_c": 0, "realized_c": 0, "unrealized_c": 0, "reserved_c": 0,
                        "remaining_c": LIMITS["daily_spend_c"]},
            "usage": {"spend_c": {"kalshi": 0, "stocks": 0, "etfs": 0}, "daily_loss_c": 0,
                      "positions": 0, "open_orders": 0, "consec_losses": 0},
            "health": [
                {"name": "Primary risk manager", "status": "ok", "detail": "Heartbeat 2 s ago"},
                {"name": "Shadow risk manager", "status": "ok", "detail": "Agrees on all 214 checks today"},
                {"name": "Watchdog", "status": "ok", "detail": "Independent process, heartbeat 1 s ago"},
                {"name": "Kalshi API", "status": "ok", "detail": "Latency 84 ms"},
                {"name": "Alpaca API", "status": "warn", "detail": "Not configured yet (Phase 3)"},
                {"name": "Reconciliation", "status": "ok", "detail": "Ledger matches exchange"},
                {"name": "Audit log chain", "status": "ok", "detail": "Verified 2,318 records"},
                {"name": "Clock", "status": "ok", "detail": "Within 0.2 s of exchange time"},
            ],
            "approval": ({
                "example": True, "market": "ETH up at 12:45 AM?", "category": "Kalshi",
                "side": "YES", "qty": 2, "entry_c": 88, "cost_c": 176, "fee_c": 2,
                "worst_c": 178, "fair_pct": 91.9, "prob_pct": 91.9, "ev_c": 6,
                "exit": "Hold to settlement; sell if YES bid falls to 50¢.",
                "remaining_c": 2000, "exposure_c": 0,
                "reason": "BTC-style flip-aware favorite: 2.4 sd from strike, no flip under way, "
                          "price below model after fee.",
                "expires_in": 60 - int(now - self.started) % 60,
            } if self.example_approval_open else None),
            "scanner": [dict(market=m, category=cat, side=sd, price=p, market_pct=mp, model_pct=md,
                             spread=spr, depth_usd=dep, verdict=v, reason=r)
                        for (m, cat, sd, p, mp, md, spr, dep, v, r) in SCANNER],
            "positions": [],
            "correlation": {
                "sectors": [{"name": "Crypto (BTC/ETH events)", "pct": 62},
                            {"name": "Technology", "pct": 24}, {"name": "Broad market", "pct": 14}],
                "groups": [
                    {"name": "Crypto 15-min events", "members": ["BTC 15-min", "ETH 15-min"],
                     "corr": 0.81, "combined_c": 176, "limit_c": 300},
                    {"name": "US large-cap tech", "members": ["AAPL", "NVDA", "QQQ"],
                     "corr": 0.74, "combined_c": 0, "limit_c": 400},
                ],
                "overlaps": [{"a": "QQQ", "b": "NVDA", "pct": 8.9}, {"a": "QQQ", "b": "AAPL", "pct": 8.4},
                             {"a": "SPY", "b": "QQQ", "pct": 48.0}],
                "redundant": ["Buying QQQ while holding AAPL + NVDA adds mostly the same tech exposure."],
                "window": "60 trading days, daily returns",
            },
            "shadow": {
                "balance_c": shadow_bal, "start_c": 10000, "pnl_c": shadow_bal - 10000,
                "fees_c": 37, "trades": 41, "wins": 39, "losses": 2, "max_dd_c": 181,
                "curve_c": curve,
                "positions": [{"market": "ETH up at 12:45 AM?", "side": "YES", "qty": 2,
                               "entry_c": 88, "mark_c": 93}],
                "vs_live": "No live trades yet to compare.",
            },
            "summary": {"purchases_c": 0, "fees_c": 0, "realized_c": 0, "unrealized_c": 0,
                        "trades": 0, "wins": 0, "losses": 0, "interventions": 3,
                        "best": "Shadow: crypto flip-aware (+$0.84 simulated)",
                        "worst": "Shadow: stock momentum (-$0.12 simulated)"},
            "feed": feed,
        }
