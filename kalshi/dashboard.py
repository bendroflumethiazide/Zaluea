"""Local dashboard for bot.py.

    python dashboard.py            then open http://127.0.0.1:8765

It only listens on this computer (127.0.0.1). It reads the bot's status
file and trade log, and can pause/resume the bot (via the STOP file) and
change the stake and daily limits (via data/bot_settings.json). It cannot
switch the bot into live trading - that stays a deliberate command-line step.
"""
from __future__ import annotations

import argparse
import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bot import LIMITS, LOG, SETTINGS, STATE, STOP_FILE

UI = Path(__file__).parent / "ui"
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8")}
STALE_AFTER = 30  # seconds without a status update => bot is not running


def read_state() -> dict:
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = {}
    st["running"] = bool(st) and time.time() - st.get("updated", 0) < STALE_AFTER
    st["stopped_by_user"] = STOP_FILE.exists()
    st["now"] = time.time()
    trades = []
    if LOG.exists():
        rows = [json.loads(x) for x in LOG.read_text().splitlines() if x.strip()]
        entries = {(r["ticker"], r["mode"]): r for r in rows if r["kind"] == "entry"}
        for r in rows:
            if r["kind"] in ("settle", "stop"):
                e = entries.get((r["ticker"], r["mode"]), {})
                trades.append(dict(ts=r["ts"], mode=r["mode"], ticker=r["ticker"],
                                   side=r["side"], count=r["count"], price=e.get("price"),
                                   outcome="stop" if r["kind"] == "stop" else ("won" if r["pnl"] > 0 else "lost"),
                                   pnl=r["pnl"]))
    st["trades"] = trades[-25:][::-1]
    return st


def clean_settings(body: dict) -> tuple[dict, dict]:
    """Return (valid settings, errors by field)."""
    out, errors = {}, {}
    names = {"stake": "Amount per trade", "max_daily_loss": "Daily loss limit",
             "max_trades": "Trades per day"}
    for key, (lo, hi) in LIMITS.items():
        if key not in body:
            continue
        try:
            v = float(body[key])
            if isinstance(lo, int):
                if v != int(v):
                    raise ValueError
                v = int(v)
        except (TypeError, ValueError):
            errors[key] = f"{names[key]} must be a {'whole ' if isinstance(lo, int) else ''}number."
            continue
        if not lo <= v <= hi:
            errors[key] = f"{names[key]} must be between {lo:g} and {hi:g}."
            continue
        out[key] = v
    return out, errors


class Handler(BaseHTTPRequestHandler):
    server_version = "KalshiBotDashboard"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            return self._json(200, read_state())
        if path in FILES:
            name, ctype = FILES[path]
            return self._send(200, (UI / name).read_bytes(), ctype)
        self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        # Only accept JSON from this page (blocks other websites posting forms here).
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                or (origin and origin.split("://", 1)[-1] != host)):
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Request refused."})
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 10_000)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": "Could not read the request."})

        if self.path == "/api/pause":
            STOP_FILE.write_text("Paused from the dashboard\n")
            return self._json(200, {"ok": True})
        if self.path == "/api/resume":
            STOP_FILE.unlink(missing_ok=True)
            return self._json(200, {"ok": True})
        if self.path == "/api/settings":
            vals, errors = clean_settings(body if isinstance(body, dict) else {})
            if errors:
                return self._json(400, {"errors": errors})
            try:
                cur = json.loads(SETTINGS.read_text())
            except (OSError, ValueError):
                cur = {}
            cur.update(vals)
            SETTINGS.parent.mkdir(exist_ok=True)
            SETTINGS.write_text(json.dumps(cur))
            return self._json(200, {"ok": True, "settings": cur})
        self._json(404, {"error": "Not found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Dashboard: http://127.0.0.1:{a.port}  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
