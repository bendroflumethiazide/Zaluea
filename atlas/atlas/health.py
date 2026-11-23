"""Heartbeats and component status (health.db), plus the notification outbox.

health.db is deliberately separate from the ledger: the watchdog must be able
to read heartbeats even if the ledger is locked, slow or broken.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .storage import connect, immediate

SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY, pid INTEGER NOT NULL, ts REAL NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL, delivered_ts REAL);
"""


class HealthBoard:
    def __init__(self, path: str | Path, create: bool = False, clock=time.time, readonly: bool = False):
        self.con = connect(path, must_exist=not create, readonly=readonly)
        if not readonly:
            self.con.executescript(SCHEMA)
        self.clock = clock

    def beat(self, component: str, status: dict | None = None) -> None:
        with immediate(self.con):
            self.con.execute("INSERT INTO heartbeats VALUES (?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
                             "pid=excluded.pid, ts=excluded.ts, status=excluded.status",
                             (component, os.getpid(), self.clock(), json.dumps(status or {})))

    def read(self) -> dict[str, dict]:
        return {r["component"]: {"pid": r["pid"], "ts": r["ts"], "status": json.loads(r["status"])}
                for r in self.con.execute("SELECT * FROM heartbeats")}

    def notify(self, level: str, title: str, body: str) -> None:
        """Durable outbox. The supervisor (python -m atlas.supervisor) delivers it as Windows notifications."""
        with immediate(self.con):
            self.con.execute("INSERT INTO notifications (ts, level, title, body) VALUES (?,?,?,?)",
                             (self.clock(), level, title, body))

    def pending_notifications(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM notifications WHERE delivered_ts IS NULL ORDER BY id")]

    def mark_delivered(self, note_id: int) -> None:
        with immediate(self.con):
            self.con.execute("UPDATE notifications SET delivered_ts=? WHERE id=? AND delivered_ts IS NULL",
                             (self.clock(), note_id))
