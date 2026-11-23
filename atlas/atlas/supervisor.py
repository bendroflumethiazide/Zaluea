"""Keeps the research stack running for weeks: the market scanner, the shadow-trading test and the dashboard.

    python -m atlas.supervisor --data "%USERPROFILE%\\.atlas\\data" --open

What it does:
  * starts each process, and restarts it if it exits (waiting 2 s, then 4, 8 ... up to 5 minutes between
    attempts; back to 2 s once a process has run for 10 minutes);
  * restarts a process that is still alive but has stopped producing results (frozen);
  * writes each process's output to <data>/logs/<name>.log (5 MB per file, 3 old files kept);
  * shows Windows notifications: a crash or freeze (at most once per 30 minutes per process), any
    notification queued by the watchdog, and a once-a-day summary of the shadow test;
  * writes <data>/logs/supervisor.json so the dashboard can show what is running.

It holds no API keys and places no orders. It starts only the research processes (scanner, shadow trading,
dashboard); the execution gateway is never started by it. Only one supervisor can run per data folder.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .notify import Notifier, deliver_outbox
from .storage import InstanceLock, StorageError

PACKAGE_ROOT = Path(__file__).resolve().parents[1]       # the folder that holds atlas/ and dashboard/
MIN_BACKOFF_S, MAX_BACKOFF_S, HEALTHY_AFTER_S = 2.0, 300.0, 600.0
ALERT_EVERY_S = 1800.0
LOG_BYTES, LOG_KEEP = 5_000_000, 3


@dataclass
class Child:
    name: str
    argv: list[str]
    label: str = ""
    stale_after_s: float | None = None              # restart if alive but its heartbeat is older than this
    heartbeat: Callable[[], float | None] | None = None
    proc: subprocess.Popen | None = None
    started: float = 0.0
    next_start: float = 0.0
    backoff: float = MIN_BACKOFF_S
    restarts: int = 0
    last_exit: str = ""
    logger: logging.Logger | None = field(default=None, repr=False)


def json_ts(path: Path) -> Callable[[], float | None]:
    """Heartbeat = the 'ts' field a process writes into its JSON output file."""
    def read() -> float | None:
        try:
            return float(json.loads(path.read_text())["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return read


def default_children(data: Path, port: int) -> list[Child]:
    py = sys.executable
    return [
        Child("shadow", [py, "-m", "atlas.shadow", "--data", str(data)], "Shadow trading test",
              stale_after_s=180, heartbeat=json_ts(data / "sim" / "shadow.json")),
        Child("scanner", [py, "-m", "atlas.scanner", "--data", str(data)], "Market scanner",
              stale_after_s=180, heartbeat=json_ts(data / "scanner.json")),
        Child("dashboard", [py, str(PACKAGE_ROOT / "dashboard" / "server.py"), "--data", str(data),
                            "--port", str(port)], "Hysilens dashboard"),
    ]


def daily_summary(data: Path, now: float) -> tuple[str, str, str]:
    """(level, title, body) describing the shadow test, from sim/shadow.json."""
    path = data / "sim" / "shadow.json"
    try:
        sd = json.loads(path.read_text())
    except (OSError, ValueError):
        return "warning", "Shadow test: no results yet", "The shadow test has not written any results. Is it running?"
    accts = sd.get("accounts") or []
    bad = [a["account"] for a in accts if not a.get("reconciled", True)]
    if bad:
        return ("critical", "Shadow test: books do not match",
                f"The journal and simulated ledger disagree for {', '.join(bad)}. Results are not reliable.")
    age = now - float(sd.get("ts", 0))
    stale = f" Results are {int(age // 60)} minutes old." if age > 300 else ""
    ev = next(iter((sd.get("evidence") or {}).values()), None)
    research = next((a for a in accts if a["account"].endswith("/research") and a["order_type"] == "maker"), None)
    if ev is None or research is None:
        return "info", "Shadow test running", f"No resting-order results yet.{stale}"
    body = (f"{ev['rules'][0]['value']} settled fills. Simulated P&L {research['pnl_total']}. "
            f"Verdict: {ev['verdict']}.{stale}")
    return ("warning" if stale else "info"), "Shadow test daily summary (simulated)", body


class Supervisor:
    def __init__(self, data: str | Path, children: list[Child], notifier: Notifier | None = None,
                 clock=time.time, summary_at: str = "21:00", local_now=datetime.now, spawn=subprocess.Popen):
        self.data = Path(data)
        self.logs = self.data / "logs"
        self.children = children
        self.notifier = notifier or Notifier()
        self.clock, self.local_now, self.spawn = clock, local_now, spawn
        h, m = (int(x) for x in summary_at.split(":"))
        self.summary_at = (h, m)
        self.lock = InstanceLock(self.data / "supervisor.lock")
        self.state_path = self.logs / "supervisor_state.json"
        self._last_outbox = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock.acquire()                     # raises StorageError if another supervisor runs here
        self.logs.mkdir(exist_ok=True)
        for c in self.children:
            c.logger = self._logger(c.name)
            c.next_start = self.clock()

    def stop(self) -> None:
        for c in self.children:
            if c.proc and c.proc.poll() is None:
                c.proc.terminate()
        deadline = time.monotonic() + 10
        for c in self.children:
            if c.proc:
                try:
                    c.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    c.proc.kill()
        self._write_status()
        self.lock.release()

    def _logger(self, name: str) -> logging.Logger:
        lg = logging.getLogger(f"atlas.supervisor.{name}.{id(self)}")
        lg.propagate = False
        lg.setLevel(logging.INFO)
        h = logging.handlers.RotatingFileHandler(self.logs / f"{name}.log", maxBytes=LOG_BYTES,
                                                 backupCount=LOG_KEEP, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h)
        return lg

    # ------------------------------------------------------------------ one pass
    def poll(self) -> None:
        now = self.clock()
        for c in self.children:
            if c.proc is None:
                if now >= c.next_start:
                    self._launch(c, now)
                continue
            code = c.proc.poll()
            if code is not None:
                self._ended(c, now, now - c.started, f"exited with code {code}", "stopped")
            elif self._frozen(c, now):
                c.proc.kill()
                try:
                    c.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                self._ended(c, now, now - c.started, f"stopped producing results for over {int(c.stale_after_s)} s",
                            "froze")
        if now - self._last_outbox >= 10:
            self._last_outbox = now
            try:
                deliver_outbox(self.data / "health.db", self.notifier)
            except Exception as e:                       # a damaged health.db must not stop supervision
                self.notifier.send("warning", "Could not read queued notifications", str(e),
                                   key="outbox-error", every_s=ALERT_EVERY_S)
        self._maybe_summary(now)
        self._write_status()

    def _launch(self, c: Child, now: float) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            c.proc = self.spawn(c.argv, cwd=str(PACKAGE_ROOT), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace")
        except OSError as e:
            c.proc = None
            self._ended(c, now, 0.0, f"could not start: {e}", "could not start")
            return
        c.started = now
        c.logger.info(f"--- {datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S} started (pid {c.proc.pid}) ---")
        if c.proc.stdout is not None:
            threading.Thread(target=self._pump, args=(c.proc, c.logger), daemon=True).start()

    @staticmethod
    def _pump(proc, logger) -> None:
        for line in proc.stdout:
            logger.info(line.rstrip("\n"))

    def _frozen(self, c: Child, now: float) -> bool:
        if c.stale_after_s is None or c.heartbeat is None or now - c.started < c.stale_after_s:
            return False
        hb = c.heartbeat()
        return hb is None or now - hb > c.stale_after_s

    def _ended(self, c: Child, now: float, ran: float, why: str, verb: str) -> None:
        c.backoff = MIN_BACKOFF_S if ran >= HEALTHY_AFTER_S else min(MAX_BACKOFF_S, c.backoff * 2)
        c.proc, c.restarts, c.last_exit = None, c.restarts + 1, why
        c.next_start = now + c.backoff
        if c.logger:
            c.logger.info(f"--- {datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S} {why}; "
                          f"restarting in {c.backoff:.0f} s ---")
        self.notifier.send("warning", f"{c.label or c.name} {verb}",
                           f"It {why}. Restarting automatically in {c.backoff:.0f} s. Log: logs/{c.name}.log",
                           key=f"down-{c.name}", every_s=ALERT_EVERY_S)

    # ------------------------------------------------------------------ daily summary
    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _maybe_summary(self, now: float) -> None:
        local = self.local_now()
        if (local.hour, local.minute) < self.summary_at:
            return
        today = local.strftime("%Y-%m-%d")
        st = self._state()
        if st.get("last_summary") == today:
            return
        level, title, body = daily_summary(self.data, now)
        self.notifier.send(level, title, body)
        st["last_summary"] = today
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(self.state_path)

    # ------------------------------------------------------------------ status for the dashboard
    def _write_status(self) -> None:
        now = self.clock()
        out = {"ts": now, "pid": os.getpid(), "children": [
            {"name": c.name, "label": c.label or c.name, "running": c.proc is not None and c.proc.poll() is None,
             "pid": c.proc.pid if c.proc else None, "uptime_s": int(now - c.started) if c.proc else 0,
             "restarts": c.restarts, "last_exit": c.last_exit,
             "next_start_in_s": None if c.proc else max(0, int(c.next_start - now))} for c in self.children]}
        tmp = self.logs / "supervisor.json.tmp"
        tmp.write_text(json.dumps(out))
        tmp.replace(self.logs / "supervisor.json")

    def run(self, interval: float = 1.0) -> None:
        self.start()
        try:
            while True:
                self.poll()
                time.sleep(interval)
        finally:
            self.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="ATLAS data folder")
    ap.add_argument("--port", type=int, default=8770, help="dashboard port (default 8770)")
    ap.add_argument("--summary-at", default="21:00", help="local time for the daily summary, HH:MM (default 21:00)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser once it is up")
    ap.add_argument("--only", nargs="+", choices=["shadow", "scanner", "dashboard"], help="run only these")
    a = ap.parse_args(argv)
    data = Path(a.data).expanduser()
    children = [c for c in default_children(data, a.port) if not a.only or c.name in a.only]
    sup = Supervisor(data, children, summary_at=a.summary_at)
    try:
        sup.start()
    except StorageError:
        print(f"ATLAS is already running for {data} (another supervisor holds it). Nothing was started.")
        return 1
    print(f"ATLAS research stack: {', '.join(c.label for c in children)}.")
    print(f"Logs: {sup.logs}   Stop with Ctrl+C (or close this window).", flush=True)
    if a.open and any(c.name == "dashboard" for c in children):
        threading.Timer(4.0, webbrowser.open, args=(f"http://127.0.0.1:{a.port}",)).start()
    try:
        while True:
            sup.poll()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Stopping…", flush=True)
    finally:
        sup.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
