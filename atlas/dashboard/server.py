"""Hysilens: the dashboard for the ATLAS trading engine.

    python server.py        then open http://127.0.0.1:8770

Serves the dashboard on this computer only (127.0.0.1). In this preview it
shows SAMPLE data from sample_state.py and holds no credentials: it cannot
reach any exchange or broker, and its controls only change sample state.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sample_state import SampleState

STATIC = Path(__file__).parent / "static"
STATE = SampleState()
REAL = None          # set by --data: read-only view of a real ATLAS data folder
CSP = ("default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Handler(BaseHTTPRequestHandler):
    server_version = "Hysilens"

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            snap = STATE.snapshot()
            if REAL is not None:
                snap = REAL.overlay(snap)
            return self._json(200, snap)
        rel = "index.html" if path == "/" else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"Not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self):
        origin, host = self.headers.get("Origin"), self.headers.get("Host", "")
        if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                or (origin and origin.split("://", 1)[-1] != host)):
            return self._json(403, {"error": "Request refused."})
        if self.path != "/api/control":
            return self._json(404, {"error": "Not found"})
        if REAL is not None:
            return self._json(409, {"ok": False, "message": "Controls are read-only here: owner controls go "
                                    "through the execution gateway, which is connected in Phase 7."})
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            ok, msg = STATE.control(str(body.get("action", "")), body.get("value"))
        except (ValueError, TypeError):
            return self._json(400, {"error": "Could not read the request."})
        self._json(200 if ok else 400, {"ok": ok, "message": msg})


def ticker():
    while True:
        time.sleep(6)
        STATE.tick()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--data", help="show real safety data from this ATLAS data folder (read-only)")
    a = ap.parse_args()
    global REAL
    if a.data:
        from real_state import RealState
        REAL = RealState(a.data)
    mimetypes.add_type("image/webp", ".webp")
    mimetypes.add_type("text/javascript", ".js")
    threading.Thread(target=ticker, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"Port {a.port} is already in use (is another dashboard running?): {e}", flush=True)
        raise SystemExit(75)          # same code as atlas.storage.ALREADY_RUNNING_EXIT
    mode = f"safety data from {a.data} (read-only)" if a.data else "preview, sample data"
    print(f"Hysilens dashboard ({mode}): http://127.0.0.1:{a.port}  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
