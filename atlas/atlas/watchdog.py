"""Independent system watchdog.

Runs as its OWN process:   python -m atlas.watchdog --data <folder>

It does not rely on the gateway to say it is healthy. Every cycle it checks,
by itself:
  * gateway, primary-risk and shadow-risk heartbeats are fresh, and the
    gateway's process is alive (crashed) and responsive (frozen);
  * every database opens and answers;
  * the ledger is internally consistent, within the daily limit, and agrees
    with the spending the gateway reports;
  * reconciliation succeeded recently;
  * the exchange answers;
  * the audit chain is intact and still contains every record it anchored.

On any failure it: sets a "watchdog" lock (blocking every new entry),
revokes today's authorization, asks the exchange to cancel ATLAS's
outstanding ENTRY orders (cancel-only), records the failure in the audit
log and queues a notification.

It can only make ATLAS safer. It has no code path that clears a lock,
authorizes trading or raises a limit, and its exchange access is wrapped so
that only cancellation is possible. If the watchdog itself stops, the
gateway refuses new entries (it requires a fresh watchdog heartbeat).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .audit import AuditLog
from .clock import accounting_day
from .control import ControlStore, LockStore
from .health import HealthBoard
from .ledger import Ledger
from .limits import LimitStore
from .paths import DataPaths
from .storage import InstanceLock, StorageError, connect

HEARTBEAT_MAX_AGE_S = 15.0
RECONCILE_MAX_AGE_S = 120.0
AUDIT_VERIFY_EVERY_S = 60.0
REQUIRED_COMPONENTS = ("gateway", "primary_risk", "shadow_risk")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (exited, not yet reaped) counts as dead.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


class CancelOnlyExchange:
    """The watchdog's only view of the exchange: it can look up and cancel orders, nothing else."""

    def __init__(self, exchange):
        self._ex = exchange

    def server_time(self):
        return self._ex.server_time()

    def get_order(self, coid):
        return self._ex.get_order(coid)

    def cancel(self, coid):
        return self._ex.cancel(coid)


@dataclass
class Finding:
    check: str
    detail: str
    lock_kind: str = "watchdog"


@dataclass
class Watchdog:
    paths: DataPaths
    exchange: object | None = None
    clock: callable = time.time
    last_audit_verify: float = 0.0
    tripped: bool = False
    findings: list = field(default_factory=list)

    def __post_init__(self):
        self.ex = CancelOnlyExchange(self.exchange) if self.exchange is not None else None
        self.lock = InstanceLock(self.paths.watchdog_lock)
        self.anchor_db = self.paths.root / "watchdog.db"

    def start(self):
        self.lock.acquire()
        self.health = HealthBoard(self.paths.health, clock=self.clock)
        self.locks = LockStore(self.paths.locks, None, clock=self.clock)
        a = sqlite3.connect(self.anchor_db)
        a.execute("CREATE TABLE IF NOT EXISTS anchors (seq INTEGER, hash TEXT, ts REAL)")
        a.commit()
        a.close()
        self.health.beat("watchdog", {"state": "started"})

    def stop(self):
        self.lock.release()

    # ------------------------------------------------------------------ checks
    def check_once(self) -> list[Finding]:
        now = self.clock()
        out: list[Finding] = []
        # 1. heartbeats and processes
        try:
            beats = self.health.read()
        except Exception as e:
            beats = {}
            out.append(Finding("health_db", f"Cannot read heartbeats: {e}"))
        for comp in REQUIRED_COMPONENTS:
            b = beats.get(comp)
            if b is None:
                out.append(Finding("heartbeat", f"{comp} has never reported"))
            elif b["ts"] - now > 5:
                out.append(Finding("clock", f"{comp} heartbeat is {b['ts'] - now:.0f} s in the future: clocks disagree", "clock"))
            elif now - b["ts"] > HEARTBEAT_MAX_AGE_S:
                state = "crashed" if not pid_alive(b["pid"]) else "frozen (alive but not responding)"
                out.append(Finding("heartbeat", f"{comp} heartbeat {now - b['ts']:.0f} s old: {state}"))
            elif comp == "gateway" and not pid_alive(b["pid"]):
                out.append(Finding("process", f"gateway process {b['pid']} is not running"))
        gw = (beats.get("gateway") or {}).get("status", {})
        if gw:
            if not gw.get("reconciled_ok"):
                out.append(Finding("reconciliation", "Gateway reports reconciliation not OK", "reconciliation"))
            elif gw.get("reconciled_at") is None or now - gw["reconciled_at"] > RECONCILE_MAX_AGE_S:
                out.append(Finding("reconciliation", "No successful reconciliation in the last 2 minutes", "reconciliation"))

        # 2. databases
        audit = None
        for name in ("audit", "ledger", "control", "locks"):
            path = getattr(self.paths, name)
            try:
                con = connect(path, readonly=True)
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                con.close()
            except Exception as e:
                out.append(Finding("database", f"{name}.db unavailable: {e}", "integrity"))

        # 3. ledger integrity and budget
        try:
            audit = AuditLog(self.paths.audit)
            ledger = Ledger(self.paths.ledger, audit, clock=self.clock)
            ok, why = ledger.integrity_check()
            if not ok:
                out.append(Finding("ledger", why, "integrity"))
            day = accounting_day(now)
            limits = LimitStore(self.paths.control, audit).current(day)
            snap = ledger.snapshot(day)
            if snap.spent > limits.daily_spend:
                out.append(Finding("budget", f"Spending {snap.spent} exceeds the {limits.daily_spend} limit", "integrity"))
            if snap.open_entry_orders > limits.max_open_orders:
                out.append(Finding("budget", f"{snap.open_entry_orders} open entry orders exceed the limit", "integrity"))
            if snap.exposure_total > limits.max_total_exposure:
                out.append(Finding("budget", f"Exposure {snap.exposure_total} exceeds the limit", "integrity"))
            if gw.get("day") == day and "spent_units" in gw and gw["spent_units"] != snap.spent.units \
                    and now - beats["gateway"]["ts"] < 5:
                out.append(Finding("budget", f"Gateway reports spending {gw['spent_units']} units, ledger says "
                                             f"{snap.spent.units}", "integrity"))
            self._open_entries = [o for o in ledger.open_orders() if o["action"] == "buy"]
        except StorageError as e:
            out.append(Finding("ledger", f"Budget state cannot be reconstructed: {e}", "integrity"))
            self._open_entries = []
        except Exception as e:
            out.append(Finding("ledger", f"Budget check failed: {e}", "integrity"))
            self._open_entries = []

        # 4. exchange connectivity
        if self.ex is not None:
            try:
                self.ex.server_time()
            except Exception as e:
                out.append(Finding("exchange", f"Exchange unreachable: {e}"))

        # 5. audit chain and anchors
        if audit is not None and now - self.last_audit_verify >= AUDIT_VERIFY_EVERY_S:
            try:
                v = audit.verify()
                a = sqlite3.connect(self.anchor_db)
                row = a.execute("SELECT seq, hash FROM anchors ORDER BY rowid DESC LIMIT 1").fetchone()
                if not v.ok:
                    out.append(Finding("audit", v.reason, "audit"))
                elif row:
                    ok, why = audit.check_anchor(row[0], row[1])
                    if not ok:
                        out.append(Finding("audit", why, "audit"))
                if v.ok:
                    a.execute("INSERT INTO anchors VALUES (?,?,?)", (v.head_seq, v.head_hash, now))
                    a.commit()
                a.close()
                self.last_audit_verify = now
            except Exception as e:
                out.append(Finding("audit", f"Audit verification failed: {e}", "audit"))

        self.findings = out
        if out:
            self._protect(out, audit)
        try:
            self.health.beat("watchdog", {"ok": not out, "findings": [f.detail for f in out][:5]})
        except Exception:
            pass                                       # if this fails the gateway sees a stale watchdog: fail closed
        return out

    # ----------------------------------------------------- protective actions
    def _protect(self, findings: list[Finding], audit: AuditLog | None):
        summary = "; ".join(f"{f.check}: {f.detail}" for f in findings)[:500]
        for kind in {f.lock_kind for f in findings}:
            try:
                self.locks.set_lock(kind, summary, "watchdog")
            except Exception:
                pass
        try:
            ctl = ControlStore(self.paths.control, audit or AuditLog(self.paths.audit), clock=self.clock)
            ctl.revoke_authorization("watchdog: " + findings[0].detail, actor="watchdog")
        except Exception:
            pass
        cancelled = 0
        if self.ex is not None:
            for o in getattr(self, "_open_entries", []):
                try:
                    self.ex.cancel(o["client_order_id"])
                    cancelled += 1
                except Exception:
                    pass
        try:
            if audit is not None:
                audit.append("watchdog_alert", decision="trading_disabled", reason=summary,
                             data={"entry_orders_cancel_requested": cancelled})
        except Exception:
            pass
        if not self.tripped:
            try:
                self.health.notify("critical", "Watchdog stopped new trading", summary)
            except Exception:
                pass
        self.tripped = True

    def run(self, interval: float = 2.0):
        self.start()
        try:
            while True:
                self.check_once()
                time.sleep(interval)
        finally:
            self.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description="ATLAS independent watchdog")
    ap.add_argument("--data", required=True, help="ATLAS data folder")
    ap.add_argument("--once", action="store_true", help="run one check cycle and exit")
    a = ap.parse_args(argv)
    wd = Watchdog(DataPaths(Path(a.data)))
    if a.once:
        wd.start()
        findings = wd.check_once()
        wd.stop()
        for f in findings:
            print(f"FINDING {f.check}: {f.detail}")
        print("OK" if not findings else "TRADING DISABLED")
        return 0 if not findings else 2
    wd.run()


if __name__ == "__main__":
    sys.exit(main())
