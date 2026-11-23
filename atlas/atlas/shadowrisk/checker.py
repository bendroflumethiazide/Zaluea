"""SHADOW RISK MANAGER - an independent second opinion on every live order.

Independence rules (enforced by tests/test_shadow_independence.py):
  * This package imports NOTHING from the rest of ATLAS. It has its own
    money arithmetic (decimal.Decimal from strings, never floats), its own
    fee formula, its own limit decoding and its own accounting-day logic.
  * It does not trust the gateway's ledger. It rebuilds spending, exposure,
    realized P&L and loss streaks from the EXCHANGE's own records (fills,
    settlements, positions, resting orders, balance), plus its own journal
    of orders it has approved that the exchange may not show yet.
  * It reads limits, authorization, module switches and locks directly from
    their databases in READ-ONLY mode. It can never change any of them.
  * It signs each decision with its own Ed25519 key. The gateway executes
    only with a valid, fresh, matching signature.

It compares its own view with the primary risk manager's claims and flags
any discrepancy as an integrity problem.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
ONE = Decimal("1")

# Codes that indicate the system itself may be wrong (not just the market).
INTEGRITY_CODES = {"SPEND_MISMATCH", "STALE_PRIMARY", "POSITION_MISMATCH", "AUTH_MISMATCH",
                   "COST_MISMATCH", "STATE_UNREADABLE", "DAY_MISMATCH", "EXCHANGE_UNAVAILABLE"}

JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    client_order_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
    event TEXT NOT NULL, max_cost TEXT NOT NULL, day TEXT NOT NULL, approved_ts REAL NOT NULL,
    closed_ts REAL, close_reason TEXT);
"""


def D(s) -> Decimal:
    if isinstance(s, float):
        raise TypeError("shadow risk manager refuses floats for money")
    return Decimal(str(s))


def shadow_fee(module: str, qty: int, price: Decimal, multiplier: Decimal = ONE) -> Decimal:
    """Independent worst-case fee: Kalshi 0.07 x C x P x (1-P) x multiplier rounded UP to the
    cent, plus one cent of balance-rounding fee for every possible fill (at most one fill per
    contract; fee_rounding docs)."""
    if module in ("kalshi", "esports"):
        raw = Decimal("0.07") * qty * price * (ONE - price) * multiplier
        return raw.quantize(CENT, rounding=ROUND_CEILING) + CENT * qty
    if module in ("stocks", "etfs"):
        return Decimal("0")
    raise ValueError(module)


def ny_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(NY).strftime("%Y-%m-%d")


def _decode_limit(v: str):
    kind, val = v.split(":", 1)
    if kind == "D":
        return D(val)
    if kind == "I":
        return int(val)
    raise ValueError(f"Unknown limit format {kind}")


@dataclass
class ShadowDecision:
    approved: bool
    problems: list[tuple[str, str]]          # (code, message)
    payload: dict
    signature: str

    @property
    def integrity_problem(self) -> bool:
        return any(code in INTEGRITY_CODES for code, _ in self.problems)


class ShadowRiskManager:
    def __init__(self, exchange, control_db: str | Path, locks_db: str | Path, journal_db: str | Path,
                 signing_key_pem: bytes, clock=time.time, health_db: str | Path | None = None):
        self.ex = exchange
        self.control_db, self.locks_db = Path(control_db), Path(locks_db)
        self.journal = sqlite3.connect(str(journal_db), timeout=10, isolation_level=None, check_same_thread=False)
        self.journal.row_factory = sqlite3.Row
        self.journal.executescript(JOURNAL_SCHEMA)
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        self.key = load_pem_private_key(signing_key_pem, password=None)
        self.clock = clock
        self.health_db = Path(health_db) if health_db else None

    def heartbeat(self) -> None:
        """Report liveness to the watchdog (own SQL, no shared ATLAS code)."""
        if not self.health_db:
            return
        import os
        con = sqlite3.connect(str(self.health_db), timeout=5)
        try:
            con.execute("INSERT INTO heartbeats VALUES ('shadow_risk', ?, ?, '{}') ON CONFLICT(component) "
                        "DO UPDATE SET pid=excluded.pid, ts=excluded.ts", (os.getpid(), self.clock()))
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------- read-only
    def _ro(self, path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _limits(self, day: str) -> dict:
        con = self._ro(self.control_db)
        try:
            out = {}
            for row in con.execute("SELECT field, value FROM limit_changes WHERE effective_day<=? ORDER BY id", (day,)):
                out[row["field"]] = _decode_limit(row["value"])
            return out
        finally:
            con.close()

    def _controls(self, day: str, module: str) -> tuple[bool, bool]:
        con = self._ro(self.control_db)
        try:
            boot = con.execute("SELECT boot_id FROM boots ORDER BY started_ts DESC LIMIT 1").fetchone()
            authorized = bool(boot) and con.execute(
                "SELECT 1 FROM authorizations WHERE day=? AND boot_id=? AND revoked_ts IS NULL",
                (day, boot["boot_id"])).fetchone() is not None
            row = con.execute("SELECT value FROM settings WHERE key=?", (f"module:{module}",)).fetchone()
            return authorized, bool(row) and row["value"] == "1"
        finally:
            con.close()

    def _locks(self) -> list[str]:
        con = self._ro(self.locks_db)
        try:
            return [r["kind"] for r in con.execute("SELECT kind FROM locks WHERE cleared_ts IS NULL")]
        finally:
            con.close()

    # ------------------------------------------------- exchange-derived state
    def _exchange_state(self, day: str):
        fills = self.ex.fills(0.0)
        settles = self.ex.settlements(0.0)
        events = sorted([("f", f["ts"], f) for f in fills] + [("s", s["ts"], s) for s in settles], key=lambda e: e[1])
        held: dict[tuple, list] = {}           # (market, side) -> [qty, basis Decimal]
        spent_today = Decimal(0)
        realized_today = Decimal(0)
        results = []                           # pnl of each closed lot, in order
        spent_by_order: dict[str, Decimal] = {}
        spent_by_market: dict[str, Decimal] = {}
        for kind, ts, e in events:
            key = (e["market"], e["side"])
            pos = held.setdefault(key, [0, Decimal(0)])
            if kind == "f" and e["action"] == "buy":
                cost = D(e["price"]) * e["qty"] + D(e["fee"])
                pos[0] += e["qty"]
                pos[1] += cost
                spent_by_order[e["client_order_id"]] = spent_by_order.get(e["client_order_id"], Decimal(0)) + cost
                if ny_day(ts) == day:
                    spent_today += cost
                    spent_by_market[e["market"]] = spent_by_market.get(e["market"], Decimal(0)) + cost
            else:
                qty = e["qty"]
                proceeds = (D(e["price"]) * qty - D(e["fee"])) if kind == "f" else D(e["payout"])
                removed = pos[1] if qty == pos[0] else (pos[1] * qty / pos[0])
                pos[0] -= qty
                pos[1] -= removed
                pnl = proceeds - removed
                results.append(pnl)
                if ny_day(ts) == day:
                    realized_today += pnl
                    if kind == "f":
                        spent_today += D(e["fee"])
                        spent_by_market[e["market"]] = spent_by_market.get(e["market"], Decimal(0)) + D(e["fee"])
        streak = 0
        for pnl in reversed(results):
            if pnl < 0:
                streak += 1
            else:
                break
        return held, spent_today, realized_today, streak, spent_by_order, spent_by_market

    def _in_flight(self, spent_by_order: dict) -> list[dict]:
        """Journal entries whose order is not CONFIRMED finished on the exchange."""
        out = []
        for j in self.journal.execute("SELECT * FROM journal WHERE closed_ts IS NULL"):
            o = self.ex.get_order(j["client_order_id"], market=j["market"])
            if o is not None and o["status"] in ("filled", "cancelled", "expired", "rejected"):
                self.journal.execute("UPDATE journal SET closed_ts=?, close_reason=? WHERE client_order_id=?",
                                     (self.clock(), o["status"], j["client_order_id"]))
                continue
            if o is None and self.clock() - j["approved_ts"] > 120:
                # Approved but never submitted, and the exchange confirms no such order exists.
                self.journal.execute("UPDATE journal SET closed_ts=?, close_reason='never_submitted' "
                                     "WHERE client_order_id=?", (self.clock(), j["client_order_id"]))
                continue
            remaining = D(j["max_cost"]) - spent_by_order.get(j["client_order_id"], Decimal(0))
            out.append(dict(j) | {"remaining": max(remaining, Decimal(0))})
        return out

    def release(self, client_order_id: str) -> bool:
        """The gateway says it did not submit an order this manager approved.
        Not trusted blindly: closed only if the exchange confirms no such order."""
        row = self.journal.execute("SELECT market FROM journal WHERE client_order_id=?", (client_order_id,)).fetchone()
        if self.ex.get_order(client_order_id, market=row["market"] if row else None) is not None:
            return False
        self.journal.execute("UPDATE journal SET closed_ts=?, close_reason='not_submitted' "
                             "WHERE client_order_id=? AND closed_ts IS NULL", (self.clock(), client_order_id))
        return True

    # --------------------------------------------------------------- decision
    def review(self, order: dict, primary_view: dict) -> ShadowDecision:
        """order: module, market, side, qty, limit_price, max_cost, strategy, client_order_id, fingerprint
        primary_view: day, spent, positions [{market, side, qty}], snapshot_ts, authorized"""
        problems: list[tuple[str, str]] = []
        bad = lambda code, msg: problems.append((code, msg))
        try:
            self.heartbeat()
        except Exception:
            pass
        try:
            server_now = float(self.ex.server_time())
        except Exception as e:
            return self._sign(order, [("EXCHANGE_UNAVAILABLE", f"Cannot reach exchange: {e}")])
        local_now = self.clock()
        if abs(server_now - local_now) > 2:
            bad("CLOCK", f"Local clock differs from exchange by {local_now - server_now:+.1f} s")
        day = ny_day(server_now)
        try:
            L = self._limits(day)
            authorized, module_on = self._controls(day, order["module"])
            locks = self._locks()
            held, spent_fills, realized, streak, spent_by_order, spent_by_market = self._exchange_state(day)
            in_flight = [j for j in self._in_flight(spent_by_order) if j["client_order_id"] != order["client_order_id"]]
            balance = D(self.ex.balance())
            book = self.ex.orderbook(order["market"], order["side"])
            info = self.ex.market_info(order["market"])
            resting = self.ex.resting_orders()
            reported = {(p["market"], p["side"]): int(p["qty"]) for p in self.ex.positions()}
        except Exception as e:
            return self._sign(order, [("STATE_UNREADABLE", f"Could not read independent state: {e}")])

        required = ("daily_spend", "max_order", "max_market_exposure", "max_total_exposure", "max_daily_loss",
                    "max_positions", "max_open_orders", "max_consec_losses", "max_event_exposure",
                    "max_slippage_bps", "max_data_age_ms")
        if any(k not in L for k in required):
            return self._sign(order, [("STATE_UNREADABLE", "Limits incomplete")])

        qty = int(order["qty"])
        limit = D(order["limit_price"])
        multiplier = ONE
        if order["module"] in ("kalshi", "esports"):
            if info.get("fee_type") not in ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"):
                return self._sign(order, [("FEE_UNKNOWN", f"Fee type {info.get('fee_type')!r} not supported")])
            multiplier = D(str(info.get("fee_multiplier")))
        own_cost = limit * qty + shadow_fee(order["module"], qty, limit, multiplier)
        claimed = D(order["max_cost"])
        if own_cost != claimed:
            bad("COST_MISMATCH", f"Order cost recomputed as {own_cost}, gateway said {claimed}")
        cost = max(own_cost, claimed)

        # --- authorization and permissions
        if primary_view.get("day") != day:
            bad("DAY_MISMATCH", f"Primary uses day {primary_view.get('day')}, exchange time says {day}")
        if not authorized:
            bad("AUTH_MISMATCH" if primary_view.get("authorized") else "NOT_AUTHORIZED",
                "Today's budget is not authorized (read independently)")
        if not module_on:
            bad("MODULE_OFF", f"{order['module']} module is off")
        if locks:
            bad("LOCKED", f"Active locks: {', '.join(locks)}")

        # --- spending (exchange fills + journaled in-flight orders)
        in_flight_cost = sum((j["remaining"] for j in in_flight), Decimal(0))
        spent = spent_fills + in_flight_cost
        claimed_spent = D(primary_view.get("spent", "0"))
        if spent != claimed_spent:
            bad("SPEND_MISMATCH", f"Independent spending {spent} differs from primary {claimed_spent}")
        spent = max(spent, claimed_spent)
        if cost > L["max_order"]:
            bad("MAX_ORDER", f"Cost {cost} above {L['max_order']}")
        if spent + cost > L["daily_spend"]:
            bad("DAILY_SPEND", f"{spent} + {cost} above {L['daily_spend']}")
        category = lambda module: "kalshi" if module in ("kalshi", "esports") else module
        cat = category(order["module"])
        cat_spent = Decimal(0)
        for m, v in spent_by_market.items():
            try:
                same = category(self.ex.market_info(m)["module"]) == cat
            except Exception:
                same = True                        # unknown: count it against this category
            if same:
                cat_spent += v
        for j in in_flight:
            try:
                same = category(self.ex.market_info(j["market"])["module"]) == cat
            except Exception:
                same = True
            if same:
                cat_spent += j["remaining"]
        cat_limit = L.get("cat_" + cat)
        if cat_limit is None:
            bad("STATE_UNREADABLE", f"No category ceiling for {cat}")
        elif cat_spent + cost > cat_limit:
            bad("CATEGORY", f"{cat}: {cat_spent} + {cost} above {cat_limit}")
        if -realized >= L["max_daily_loss"]:
            bad("DAILY_LOSS", f"Realized today {realized}")
        if streak >= L["max_consec_losses"]:
            bad("LOSS_STREAK", f"{streak} losses in a row")

        # --- exposure and counts
        exposure = {k: v[1] for k, v in held.items() if v[0] > 0}
        for j in in_flight:
            exposure[(j["market"], j["side"])] = exposure.get((j["market"], j["side"]), Decimal(0)) + j["remaining"]
        total = sum(exposure.values(), Decimal(0))
        market_exp = sum((v for (m, _), v in exposure.items() if m == order["market"]), Decimal(0))
        event_exp = Decimal(0)
        for (m, _), v in exposure.items():
            try:
                if self.ex.market_info(m)["event"] == info["event"]:
                    event_exp += v
            except Exception:
                event_exp += v                     # unknown relationship: assume correlated
        if market_exp + cost > L["max_market_exposure"]:
            bad("MARKET_EXPOSURE", f"{market_exp} + {cost} above {L['max_market_exposure']}")
        if total + cost > L["max_total_exposure"]:
            bad("TOTAL_EXPOSURE", f"{total} + {cost} above {L['max_total_exposure']}")
        if event_exp + cost > L["max_event_exposure"]:
            bad("EVENT_EXPOSURE", f"Event {info['event']}: {event_exp} + {cost} above {L['max_event_exposure']}")
        other = "no" if order["side"] == "yes" else "yes"
        if (order["market"], other) in exposure or (order["market"], other) in reported:
            bad("NETTING", "Opposite side of this market is already held or in flight")
        keys = set(exposure) | {(order["market"], order["side"])}
        if len(keys) > L["max_positions"]:
            bad("POSITIONS", f"Would hold {len(keys)} positions")
        open_entries = len({r["client_order_id"] for r in resting if r["action"] == "buy"} |
                           {j["client_order_id"] for j in in_flight})
        if open_entries + 1 > L["max_open_orders"]:
            bad("OPEN_ORDERS", f"{open_entries} entry orders already open")
        if balance < cost + in_flight_cost:
            bad("CASH", f"Balance {balance} below {cost} + {in_flight_cost} in flight")

        # --- positions the primary believes in must match the exchange
        theirs = {(p["market"], p["side"]): int(p["qty"]) for p in primary_view.get("positions", [])}
        ours = {k: v[0] for k, v in held.items() if v[0] > 0}
        if reported != ours:
            bad("POSITION_MISMATCH", f"Exchange positions {reported} differ from its own fill history {ours}")
        if theirs != reported:
            bad("POSITION_MISMATCH", f"Primary positions {theirs} differ from exchange {reported}")
        if local_now - float(primary_view.get("snapshot_ts", 0)) > 5:
            bad("STALE_PRIMARY", "Primary's budget snapshot is more than 5 s old")

        # --- market quality from a fresh, independent read
        ask, bid = D(book["ask"]), D(book["bid"])
        age_ms = (server_now - float(book["ts"])) * 1000
        if age_ms > L["max_data_age_ms"]:
            bad("DATA_AGE", f"Order book is {age_ms:.0f} ms old")
        if bid > ask:
            bad("CROSSED", "Order book is crossed")
        if book["ask_size"] < qty:
            bad("DEPTH", f"Only {book['ask_size']} available")
        if limit > ask * (1 + Decimal(L["max_slippage_bps"]) / 10_000):
            bad("SLIPPAGE", f"Limit {limit} too far above ask {ask}")

        if not problems:
            self.journal.execute("INSERT INTO journal VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                                 (order["client_order_id"], order["fingerprint"], order["market"], order["side"],
                                  info["event"], str(cost), day, self.clock()))
        return self._sign(order, problems)

    def _sign(self, order: dict, problems: list[tuple[str, str]]) -> ShadowDecision:
        payload = {"approved": not problems, "fingerprint": order.get("fingerprint"),
                   "client_order_id": order.get("client_order_id"), "ts": self.clock(),
                   "codes": sorted({c for c, _ in problems})}
        msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = base64.b64encode(self.key.sign(msg)).decode()
        return ShadowDecision(not problems, problems, payload, sig)


def verify_signature(public_key_pem: bytes, payload: dict, signature: str) -> bool:
    """Used by the gateway: was this decision really signed by the shadow?"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    try:
        key = load_pem_public_key(public_key_pem)
        key.verify(base64.b64decode(signature), json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def generate_keypair() -> tuple[bytes, bytes]:
    """(private_pem, public_pem) for a new shadow signing identity."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub
