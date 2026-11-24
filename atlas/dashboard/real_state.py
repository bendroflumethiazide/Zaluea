"""Read-only bridge from a real ATLAS data folder to the dashboard.

Opens every store in SQLite read-only mode: the dashboard can display the
real ledger, limits, locks, heartbeats and audit log but cannot change them.
Panels that have no real source yet (markets, correlation, shadow trading)
keep their sample data and stay labelled as samples.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.audit import AuditLog                     # noqa: E402
from atlas.clock import accounting_day               # noqa: E402
from atlas.control import LOCK_KINDS, ControlStore, LockStore  # noqa: E402
from atlas.health import HealthBoard                 # noqa: E402
from atlas.money import Money                        # noqa: E402
from atlas.ledger import Ledger                      # noqa: E402
from atlas.limits import LimitStore                  # noqa: E402
from atlas.paths import DataPaths                    # noqa: E402

KIND_BY_CATEGORY = {
    "trade_rejection": "reject", "risk_decision": "risk", "shadow_risk_decision": "risk",
    "risk_disagreement": "stop", "reconciliation": "recon", "watchdog_alert": "stop", "emergency_stop": "stop",
    "safety_lock": "stop", "authorization_change": "control", "config_change": "control",
    "trade_approval": "control", "fill": "risk", "settlement": "risk", "order_submission": "risk",
    "budget_change": "risk", "restart": "health", "setup": "health", "ledger": "health",
}
COMPONENTS = [("gateway", "Execution gateway"), ("primary_risk", "Primary risk manager"),
              ("shadow_risk", "Shadow risk manager"), ("watchdog", "Watchdog")]


def cents(m) -> int:
    return m.units // 10_000


class RealState:
    def __init__(self, data_dir: str | Path):
        self.p = DataPaths(Path(data_dir))

    def overlay(self, snap: dict) -> dict:
        """Replace sample values with real ones wherever a real source exists."""
        now = time.time()
        day = accounting_day(now)
        snap["preview"] = "partial"
        try:
            audit = AuditLog(self.p.audit, readonly=True)
            ledger = Ledger(self.p.ledger, None, readonly=True)
            limits = LimitStore(self.p.control, None, readonly=True).current(day)
            control = ControlStore(self.p.control, None, readonly=True)
            locks = LockStore(self.p.locks, None, readonly=True)
            health = HealthBoard(self.p.health, readonly=True)
        except Exception as e:
            snap["real_error"] = f"Cannot open ATLAS data: {e}"
            snap["controls"]["emergency_stop"] = True       # unknown state is shown as stopped
            return snap

        s = ledger.snapshot(day)
        active_locks = locks.active()
        snap["limits"] = {
            "daily_spend_c": cents(limits.daily_spend), "max_order_c": cents(limits.max_order),
            "max_market_c": cents(limits.max_market_exposure), "max_daily_loss_c": cents(limits.max_daily_loss),
            "max_positions": limits.max_positions, "max_open_orders": limits.max_open_orders,
            "max_consec_losses": limits.max_consec_losses,
            "category_c": {"kalshi": cents(limits.cat_kalshi), "stocks": cents(limits.cat_stocks),
                           "etfs": cents(limits.cat_etfs)},
        }
        snap["account"] = {
            "cash_c": None, "portfolio_c": None, "exposure_c": cents(s.exposure_total),
            "purchases_c": cents(s.purchases), "fees_c": cents(s.fees), "realized_c": cents(s.realized_today),
            "unrealized_c": None, "reserved_c": cents(s.reserved),
            "remaining_c": max(0, cents(limits.daily_spend) - cents(s.spent)),
        }
        snap["usage"] = {
            "spend_c": {k: cents(s.spent_by_category.get(k, type(s.spent)(0))) for k in ("kalshi", "stocks", "etfs")},
            "daily_loss_c": max(0, -cents(s.realized_today)), "positions": len(s.positions),
            "open_orders": s.open_entry_orders, "consec_losses": s.consecutive_losses,
        }
        c = snap["controls"]
        c["authorized_today"] = control.authorized_in_latest_boot(day)
        c["require_approval"] = control.require_approval()
        c["emergency_stop"] = any(l["kind"] == "emergency_stop" for l in active_locks)
        c["modules"] = {m: control.module_enabled(m) for m in ("kalshi", "esports", "stocks", "etfs")}
        c["locks"] = [f"{LOCK_KINDS.get(l['kind'], l['kind'])}: {l['reason']}" for l in active_locks]

        beats = health.read()
        rows = []
        for comp, name in COMPONENTS:
            b = beats.get(comp)
            if b is None:
                rows.append({"name": name, "status": "fail", "detail": "Not running"})
            else:
                age = now - b["ts"]
                rows.append({"name": name, "status": "ok" if age < 15 else "fail",
                             "detail": f"Heartbeat {age:.0f} s ago" if age < 15 else f"Silent for {age:.0f} s"})
        v = audit.verify()
        rows.append({"name": "Audit log chain", "status": "ok" if v.ok else "fail",
                     "detail": f"Verified {v.count:,} records" if v.ok else v.reason})
        ok, why = ledger.integrity_check()
        rows.append({"name": "Spending ledger", "status": "ok" if ok else "fail", "detail": why})
        rows.append({"name": "Safety locks", "status": "warn" if active_locks else "ok",
                     "detail": "; ".join(c["locks"]) if active_locks else "None active"})
        snap["health"] = rows

        feed = []
        for e in audit.export()[-40:][::-1]:
            ts = datetime.fromisoformat(e["ts"]).timestamp()
            feed.append({"id": 10_000_000 + e["seq"], "t": ts, "kind": KIND_BY_CATEGORY.get(e["category"], "scan"),
                         "title": e["category"].replace("_", " ").capitalize() + (f": {e['decision']}" if e["decision"] else ""),
                         "detail": (e["reason"] or e["asset"] or "")[:200], "important": False})
        snap["feed"] = feed or snap["feed"]
        snap["positions"] = [{"market": p.market, "side": p.side, "qty": p.qty, "basis_c": p.basis // 100}
                             for p in s.positions]
        snap["summary"].update({"purchases_c": cents(s.purchases), "fees_c": cents(s.fees),
                                "realized_c": cents(s.realized_today), "unrealized_c": 0,
                                "trades": len([t for t in s.closed_trades if t.day == day]),
                                "wins": len([t for t in s.closed_trades if t.day == day and t.pnl > 0]),
                                "losses": len([t for t in s.closed_trades if t.day == day and t.pnl < 0])})
        snap["approval"] = None
        scan = self.p.root / "scanner.json"
        try:
            sd = json.loads(scan.read_text()) if scan.exists() else None
        except (OSError, ValueError):
            sd = None
        if sd and now - sd.get("ts", 0) < 60:
            snap["scanner"] = sd["rows"]
            snap["scanner_note"] = (f"Live scan {int(now - sd['ts'])} s ago. Model {sd['model']['version']}: "
                                    f"{sd['model']['scorecard']}")
        else:
            snap["scanner_note"] = "Scanner not running (start everything with Start-ATLAS.cmd); sample rows shown"
        try:
            snap["supervisor"] = self._supervisor(now)     # shown even when shadow results are missing
            snap["shadow"] = self._shadow(now) or snap["shadow"]
        except (KeyError, TypeError, ValueError):     # an older or damaged shadow.json: show the sample
            snap["shadow"]["vs_live"] = "shadow.json could not be read; sample shown"
        snap["audit_head"] = v.head_hash[:16]
        return snap

    def _supervisor(self, now: float) -> dict | None:
        """What python -m atlas.supervisor reports it is running (logs/supervisor.json), or None if never run."""
        try:
            sv = json.loads((self.p.root / "logs" / "supervisor.json").read_text())
            age = int(now - float(sv["ts"]))
            return {"running": age < 30, "age_s": age,
                    "children": [{"label": c["label"], "running": bool(c["running"]), "restarts": int(c["restarts"]),
                                  "last_exit": c.get("last_exit", "")} for c in sv["children"]]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _shadow(self, now: float) -> dict | None:
        """Shadow-trading results from sim/shadow.json (written by python -m atlas.shadow)."""
        path = self.p.root / "sim" / "shadow.json"
        try:
            sd = json.loads(path.read_text()) if path.exists() else None
        except (OSError, ValueError):
            return None
        if not sd or not sd.get("simulated"):
            return None
        c = lambda m: int(Money.parse(m).units // 10_000)        # display only
        accts = sd["accounts"]
        main = next((a for a in accts if a["account"].endswith("/as_live") and a["order_type"] == "maker"),
                    accts[0] if accts else None)
        if main is None:
            return None
        curve = [0] + main.get("curve_c", [])
        peak, dd = 0, 0
        for x in curve:
            peak, dd = max(peak, x), max(dd, peak - x)
        age = int(now - sd["ts"])
        real = sd.get("real") or {}
        return {
            "real": True, "label": sd["label"], "account": main["account"],
            "running": age < 60, "age_s": age,
            "pnl_c": c(main["pnl_total"]), "curve_c": curve, "trades": main["settled"], "wins": main["wins"],
            "losses": main["settled"] - main["wins"], "fees_c": c(main["fees_paid"]), "max_dd_c": dd,
            "fill_rate": main["fill_rate_pct"], "missed_win_rate": main["missed_win_rate_pct"],
            "win_rate": main["win_rate_pct"],
            "positions": [{"market": x["market"], "side": x["side"].upper(), "qty": x["qty"],
                           "entry_c": x["basis"] // 10_000 // max(x["qty"], 1), "mark_c": None}
                          for x in main.get("positions", [])],
            "accounts": [{"name": a["account"], "type": a["order_type"], "orders": a["submitted"],
                          "fill_rate": a["fill_rate_pct"], "settled": a["settled"], "win_rate": a["win_rate_pct"],
                          "pnl": a["pnl_total"], "pnl_no_maker_fee": a.get("pnl_if_no_maker_fee"),
                          "missed": a["missed_resolved"], "missed_win_rate": a["missed_win_rate_pct"],
                          "rejected": a["rejected"], "open": a["open_orders"],
                          "reconciled": a.get("reconciled", True)} for a in accts],
            "assumptions": sd.get("assumptions", {}),
            "evidence": next(({**ev, "strategy": k} for k, ev in (sd.get("evidence") or {}).items()), None),
            "vs_live": f"real account {real.get('pnl_total', '$0.00')} realized, kept separate. "
                       + real.get("note", ""),
        }

