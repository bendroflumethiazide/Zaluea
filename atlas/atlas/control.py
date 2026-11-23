"""Owner controls: daily authorization, per-trade approvals, modules, locks.

ControlStore (control.db, owned by the gateway):
  * Daily authorization ("AUTHORIZE TODAY'S TRADING BUDGET"). Default OFF.
    Valid only for the verified accounting day it was granted on AND only for
    the gateway run (boot) it was granted in, so every restart turns it off.
  * "REQUIRE MY APPROVAL FOR EVERY LIVE TRADE". Default ON, and forced back
    ON at every boot (autonomous trading never resumes by itself).
  * Per-trade approvals: bound to an exact order fingerprint, single use,
    expire after APPROVAL_SECONDS.
  * Module switches (Kalshi, E-Sports, Stocks, ETFs).

LockStore (locks.db, writable by gateway, watchdog and dashboard):
  * Any active lock blocks ALL new live entries: emergency stop, daily loss,
    consecutive losses, drawdown, integrity, watchdog, risk disagreement...
  * Locks are cleared only by the owner, with typed confirmation. Clearing a
    lock never re-authorizes trading. Integrity-type locks also require a
    successful reconciliation before they can be cleared.
  * If locks.db cannot be read, callers must treat trading as locked.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from .audit import AuditLog
from .money import Money
from .storage import StorageError, connect, immediate

APPROVAL_SECONDS = 60
AUTHORIZE_CONFIRMATION = "AUTHORIZE"
AUTONOMOUS_CONFIRMATION = "AUTONOMOUS"
RESUME_CONFIRMATION = "RESUME"
MODULES = ("kalshi", "esports", "stocks", "etfs")

LOCK_KINDS = {
    "emergency_stop": "STOP ALL NEW TRADING",
    "daily_loss": "Daily loss limit reached",
    "consecutive_losses": "Consecutive losing trades limit reached",
    "drawdown": "Emergency drawdown threshold reached",
    "integrity": "System integrity problem",
    "watchdog": "Watchdog detected a failure",
    "risk_disagreement": "Risk managers disagreed",
    "reconciliation": "Account reconciliation failed",
    "audit": "Audit log verification failed",
    "security": "Credential or security problem",
    "clock": "Clock could not be verified",
}
# Locks that may only be cleared after reconciliation succeeds.
NEEDS_RECONCILIATION = {"integrity", "watchdog", "risk_disagreement", "reconciliation", "audit", "security"}


class ControlError(ValueError):
    pass


def order_fingerprint(module: str, market: str, side: str, action: str, qty: int,
                      limit_price: Money, max_cost: Money) -> str:
    """Identity of an exact order. Any material change gives a new fingerprint."""
    payload = json.dumps([module, market, side, action, int(qty), limit_price.units, max_cost.units])
    return hashlib.sha256(payload.encode()).hexdigest()


CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS boots (boot_id TEXT PRIMARY KEY, started_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS authorizations (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, boot_id TEXT NOT NULL,
    granted_ts REAL NOT NULL, revoked_ts REAL, revoked_reason TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL, used_ts REAL, summary TEXT);
"""

LOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS locks (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, reason TEXT NOT NULL, set_by TEXT NOT NULL,
    set_ts REAL NOT NULL, cleared_ts REAL, cleared_by TEXT);
"""


class ControlStore:
    def __init__(self, path: str | Path, audit: AuditLog | None, create: bool = False,
                 clock=time.time, readonly: bool = False):
        self.con = connect(path, must_exist=not create, readonly=readonly)
        if not readonly:
            self.con.executescript(CONTROL_SCHEMA)
        self.audit = audit
        self.clock = clock
        self.boot_id: str | None = None

    # ---- boot ----
    def boot(self) -> str:
        """Called once when the gateway starts. Revokes any authorization from
        earlier runs and forces per-trade approval back ON."""
        self.boot_id = str(uuid.uuid4())
        now = self.clock()
        with immediate(self.con):
            self.con.execute("INSERT INTO boots VALUES (?,?)", (self.boot_id, now))
            self.con.execute("UPDATE authorizations SET revoked_ts=?, revoked_reason='restart' "
                             "WHERE revoked_ts IS NULL", (now,))
            self._set("require_approval", "1", now)
            self.con.execute("UPDATE approvals SET used_ts=? WHERE used_ts IS NULL", (now,))
        self.audit.append("restart", decision="booted", reason="Gateway started: authorization OFF, "
                          "per-trade approval ON, pending approvals voided", data={"boot_id": self.boot_id})
        return self.boot_id

    def _set(self, key: str, value: str, now: float):
        self.con.execute("INSERT INTO settings VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                         "SET value=excluded.value, updated_ts=excluded.updated_ts", (key, value, now))

    def _get(self, key: str, default: str) -> str:
        row = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---- daily authorization ----
    def authorize_today(self, verified_day: str | None, confirm: str, actor: str = "owner") -> None:
        if self.boot_id is None:
            raise ControlError("Gateway has not booted")
        if verified_day is None:
            raise ControlError("Clock is not verified, so today's date is uncertain")
        if confirm != AUTHORIZE_CONFIRMATION:
            raise ControlError(f'Type "{AUTHORIZE_CONFIRMATION}" to authorize today\'s budget')
        with immediate(self.con):
            self.con.execute("INSERT INTO authorizations (day, boot_id, granted_ts) VALUES (?,?,?)",
                             (verified_day, self.boot_id, self.clock()))
        self.audit.append("authorization_change", decision="authorized", reason=f"by {actor}",
                          data={"day": verified_day})

    def revoke_authorization(self, reason: str, actor: str = "owner") -> None:
        with immediate(self.con):
            self.con.execute("UPDATE authorizations SET revoked_ts=?, revoked_reason=? WHERE revoked_ts IS NULL",
                             (self.clock(), reason))
        self.audit.append("authorization_change", decision="revoked", reason=f"{actor}: {reason}")

    def authorized_in_latest_boot(self, day: str) -> bool:
        """Read-only view for displays: is `day` authorized in the most recent gateway run?"""
        boot = self.con.execute("SELECT boot_id FROM boots ORDER BY started_ts DESC LIMIT 1").fetchone()
        return bool(boot) and self.con.execute(
            "SELECT 1 FROM authorizations WHERE day=? AND boot_id=? AND revoked_ts IS NULL",
            (day, boot["boot_id"])).fetchone() is not None

    def is_authorized(self, verified_day: str | None) -> bool:
        if self.boot_id is None or verified_day is None:
            return False
        return self.con.execute(
            "SELECT 1 FROM authorizations WHERE day=? AND boot_id=? AND revoked_ts IS NULL",
            (verified_day, self.boot_id)).fetchone() is not None

    # ---- per-trade approval ----
    def require_approval(self) -> bool:
        return self._get("require_approval", "1") == "1"

    def set_require_approval(self, on: bool, confirm: str = "", actor: str = "owner") -> None:
        if not on and confirm != AUTONOMOUS_CONFIRMATION:
            raise ControlError(f'Type "{AUTONOMOUS_CONFIRMATION}" to stop asking before each trade')
        with immediate(self.con):
            self._set("require_approval", "1" if on else "0", self.clock())
        self.audit.append("authorization_change", decision="approval_required" if on else "approval_not_required",
                          reason=f"by {actor}")

    def grant_approval(self, fingerprint: str, summary: str, actor: str = "owner") -> str:
        """The owner approves one exact order. Returns the approval id."""
        now = self.clock()
        aid = str(uuid.uuid4())
        with immediate(self.con):
            self.con.execute("INSERT INTO approvals VALUES (?,?,?,?,NULL,?)",
                             (aid, fingerprint, now, now + APPROVAL_SECONDS, summary))
        self.audit.append("trade_approval", decision="approved", reason=f"by {actor}",
                          data={"approval_id": aid, "fingerprint": fingerprint, "summary": summary})
        return aid

    def consume_approval(self, approval_id: str | None, fingerprint: str) -> tuple[bool, str]:
        """Atomically check-and-use an approval for this exact order."""
        if not approval_id:
            return False, "No approval was given for this order"
        now = self.clock()
        with immediate(self.con):
            row = self.con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                return False, "Unknown approval"
            if row["used_ts"] is not None:
                return False, "Approval was already used or voided"
            if row["fingerprint"] != fingerprint:
                return False, "Order changed since it was approved; a new approval is needed"
            if now > row["expires_ts"]:
                return False, "Approval expired"
            self.con.execute("UPDATE approvals SET used_ts=? WHERE id=?", (now, approval_id))
        return True, "Approved by owner"

    # ---- modules ----
    def module_enabled(self, module: str) -> bool:
        if module not in MODULES:
            return False
        return self._get(f"module:{module}", "0") == "1"   # default OFF

    def set_module(self, module: str, on: bool, actor: str = "owner") -> None:
        if module not in MODULES:
            raise ControlError(f"Unknown module {module}")
        with immediate(self.con):
            self._set(f"module:{module}", "1" if on else "0", self.clock())
        self.audit.append("config_change", decision=f"module_{'on' if on else 'off'}", asset=module,
                          reason=f"by {actor}")


class LockStore:
    def __init__(self, path: str | Path, audit: AuditLog | None, create: bool = False, clock=time.time,
                 readonly: bool = False):
        self.path = Path(path)
        self.con = connect(self.path, must_exist=not create, readonly=readonly)
        if not readonly:
            self.con.executescript(LOCK_SCHEMA)
        self.audit = audit
        self.clock = clock

    def set_lock(self, kind: str, reason: str, set_by: str) -> None:
        if kind not in LOCK_KINDS:
            raise ControlError(f"Unknown lock kind {kind}")
        with immediate(self.con):
            already = self.con.execute("SELECT 1 FROM locks WHERE kind=? AND cleared_ts IS NULL", (kind,)).fetchone()
            if not already:
                self.con.execute("INSERT INTO locks (kind, reason, set_by, set_ts) VALUES (?,?,?,?)",
                                 (kind, reason, set_by, self.clock()))
        if self.audit and not already:
            self.audit.append("emergency_stop" if kind == "emergency_stop" else "safety_lock",
                              decision="locked", reason=f"{LOCK_KINDS[kind]}: {reason}",
                              data={"kind": kind, "set_by": set_by})

    def active(self) -> list[dict]:
        """Active locks. Raises StorageError if unreadable: callers fail closed."""
        try:
            return [dict(r) for r in self.con.execute("SELECT * FROM locks WHERE cleared_ts IS NULL ORDER BY id")]
        except Exception as e:
            raise StorageError(f"Cannot read trading locks: {e}") from e

    def clear(self, kind: str, confirm: str, reconciled_ok: bool, actor: str = "owner") -> None:
        if confirm != RESUME_CONFIRMATION:
            raise ControlError(f'Type "{RESUME_CONFIRMATION}" to clear this lock')
        if kind in NEEDS_RECONCILIATION and not reconciled_ok:
            raise ControlError("This lock can only be cleared after a successful reconciliation")
        with immediate(self.con):
            n = self.con.execute("UPDATE locks SET cleared_ts=?, cleared_by=? WHERE kind=? AND cleared_ts IS NULL",
                                 (self.clock(), actor, kind)).rowcount
        if self.audit and n:
            self.audit.append("safety_lock", decision="cleared", reason=f"{LOCK_KINDS[kind]} cleared by {actor}",
                              data={"kind": kind})


def trading_locked(locks: LockStore) -> tuple[bool, str]:
    """True (with reason) if any lock is active OR the lock store is unreadable."""
    try:
        act = locks.active()
    except StorageError as e:
        return True, str(e)
    if act:
        return True, "; ".join(f"{LOCK_KINDS.get(l['kind'], l['kind'])} ({l['reason']})" for l in act)
    return False, ""
