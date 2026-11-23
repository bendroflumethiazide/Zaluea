"""Tamper-evident, append-only audit log.

Every record stores the SHA-256 of (its own content + the previous record's
hash), forming a chain. Changing, removing or reordering any record breaks
the chain from that point on, which `verify()` detects.

Defences, in layers:
  * SQLite triggers reject UPDATE and DELETE on the events table, so ordinary
    ATLAS code cannot modify history even by mistake.
  * The hash chain detects edits made by going around the triggers.
  * Truncation (cutting records off the end) keeps a valid chain, so other
    components store ANCHORS (seq + hash) in their own databases and the
    daily summary carries the head hash; `check_anchor()` catches truncation.

Limit (documented): on a computer an attacker fully controls, the chain makes
tampering evident, not impossible.

Secrets never enter the log: values under secret-looking keys and anything
that looks like a PEM private key are replaced with "[REDACTED]".
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .money import Money
from .storage import StorageError, connect, immediate

GENESIS = "0" * 64
SECRET_KEY_PARTS = ("secret", "password", "passwd", "token", "api_key", "apikey", "private",
                    "signature", "authorization", "key_id", "pem", "credential", "cookie")
REDACTED = "[REDACTED]"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    asset TEXT,
    strategy TEXT,
    model_version TEXT,
    decision TEXT,
    reason TEXT,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


class AuditError(RuntimeError):
    pass


def _redact(obj, key: str = ""):
    if any(part in key.lower() for part in SECRET_KEY_PARTS):
        return REDACTED
    if isinstance(obj, dict):
        return {str(k): _redact(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v, key) for v in obj]
    if isinstance(obj, Money):
        return obj.to_str()
    if isinstance(obj, str) and ("-----BEGIN" in obj and "PRIVATE KEY" in obj):
        return REDACTED
    if isinstance(obj, float):
        # Floats are allowed in descriptive data (e.g. a probability) but are
        # stored as text so the hash never depends on float formatting.
        return repr(obj)
    return obj


def _canonical(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(record: dict) -> str:
    return hashlib.sha256(_canonical(record).encode()).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    count: int
    head_seq: int
    head_hash: str
    first_bad_seq: int | None = None
    reason: str = "Chain intact"


class AuditLog:
    def __init__(self, path: str | Path, create: bool = False, readonly: bool = False):
        self.path = Path(path)
        self.con = connect(self.path, must_exist=not create, readonly=readonly)
        if not readonly:
            self.con.executescript(SCHEMA)

    def append(self, category: str, *, asset: str | None = None, strategy: str | None = None,
               model_version: str | None = None, decision: str | None = None,
               reason: str | None = None, data: dict | None = None) -> str:
        safe_reason = _redact(reason or "") if reason else reason
        body = _redact(data or {})
        event_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with immediate(self.con):
            row = self.con.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (row["seq"] + 1) if row else 1
            prev = row["hash"] if row else GENESIS
            rec = dict(seq=seq, event_id=event_id, ts=ts, category=category, asset=asset,
                       strategy=strategy, model_version=model_version, decision=decision,
                       reason=safe_reason, data=_canonical(body), prev_hash=prev)
            h = _hash(rec)
            self.con.execute(
                "INSERT INTO events (seq,event_id,ts,category,asset,strategy,model_version,"
                "decision,reason,data,prev_hash,hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (seq, event_id, ts, category, asset, strategy, model_version, decision,
                 safe_reason, rec["data"], prev, h))
        return event_id

    def head(self) -> tuple[int, str]:
        row = self.con.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (row["seq"], row["hash"]) if row else (0, GENESIS)

    def verify(self) -> VerifyResult:
        prev, expected_seq, n = GENESIS, 1, 0
        try:
            rows = self.con.execute("SELECT * FROM events ORDER BY seq").fetchall()
        except Exception as e:
            return VerifyResult(False, 0, 0, GENESIS, None, f"Cannot read audit log: {e}")
        for r in rows:
            if r["seq"] != expected_seq:
                return VerifyResult(False, n, r["seq"], prev, r["seq"], f"Gap before record {r['seq']}")
            if r["prev_hash"] != prev:
                return VerifyResult(False, n, r["seq"], prev, r["seq"], f"Record {r['seq']} does not link to the previous record")
            rec = {k: r[k] for k in ("seq", "event_id", "ts", "category", "asset", "strategy",
                                     "model_version", "decision", "reason", "data", "prev_hash")}
            if _hash(rec) != r["hash"]:
                return VerifyResult(False, n, r["seq"], prev, r["seq"], f"Record {r['seq']} was modified")
            prev, expected_seq, n = r["hash"], expected_seq + 1, n + 1
        return VerifyResult(True, n, n, prev)

    def check_anchor(self, seq: int, h: str) -> tuple[bool, str]:
        """Does the log still contain record `seq` with hash `h`? Catches truncation
        and rewrites of history that an anchor holder saw earlier."""
        if seq == 0:
            return True, "Empty anchor"
        row = self.con.execute("SELECT hash FROM events WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            return False, f"Audit log is shorter than anchor (record {seq} missing)"
        if row["hash"] != h:
            return False, f"Record {seq} differs from the anchored copy"
        return True, "Anchor matches"

    def export(self, since_seq: int = 0) -> list[dict]:
        rows = self.con.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (since_seq,)).fetchall()
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows]

    def close(self):
        self.con.close()
