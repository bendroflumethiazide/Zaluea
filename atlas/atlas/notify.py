"""Desktop notifications.

On Windows a native toast is shown through Windows PowerShell's built-in WinRT bridge, so nothing has to be
installed. Everywhere else, or if the toast fails, the message is printed (and so lands in the log).

Safety: the title and body are passed to PowerShell through environment variables and inserted with
CreateTextNode, never pasted into the script text, so a market name or an error message cannot inject
PowerShell commands or toast XML.
"""
from __future__ import annotations

import base64
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$kind = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($kind)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode($env:ATLAS_TOAST_TITLE)) | Out-Null
$t.Item(1).AppendChild($xml.CreateTextNode($env:ATLAS_TOAST_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$n = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($n)
"""
ENCODED = base64.b64encode(TOAST_PS.encode("utf-16-le")).decode("ascii")
MAX_TITLE, MAX_BODY = 80, 400


def windows_toast(title: str, body: str, run=subprocess.run) -> bool:
    env = {**os.environ, "ATLAS_TOAST_TITLE": title[:MAX_TITLE], "ATLAS_TOAST_BODY": body[:MAX_BODY]}
    try:
        r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", ENCODED], env=env, capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Notifier:
    """Every notification is written to the log first; the log is the record. The toast is a best-effort
    extra that is never retried.

    send() never raises: a notification problem must not stop the thing that is reporting a problem.
    With background=True, toasts are shown on a worker thread, so a slow or hanging PowerShell can never
    delay the caller (the supervisor keeps restarting and freeze-checking meanwhile). If more than
    QUEUE_MAX toasts are waiting, further ones are skipped (they are still logged)."""

    QUEUE_MAX = 20

    def __init__(self, toast=None, log=print, clock=time.time, background: bool = False):
        self.toast = toast if toast is not None else (windows_toast if os.name == "nt" else None)
        self.log, self.clock = log, clock
        self._last: dict[str, float] = {}
        self._queue: queue.Queue | None = None
        if background and self.toast is not None:
            self._queue = queue.Queue(maxsize=self.QUEUE_MAX)
            threading.Thread(target=self._worker, daemon=True, name="atlas-toasts").start()

    def send(self, level: str, title: str, body: str, *, key: str | None = None, every_s: float = 0) -> bool:
        """Log and show a notification. Returns False only when suppressed: the same key at most once per
        every_s seconds."""
        now = self.clock()
        if key is not None and now - self._last.get(key, float("-inf")) < every_s:
            return False
        if key is not None:
            self._last[key] = now
        self._log(f"[{level.upper()}] {title}: {body}")
        if self.toast is None:
            return True
        if self._queue is not None:
            try:
                self._queue.put_nowait((title, body))
            except queue.Full:
                self._log("(toast skipped: too many waiting)")
        else:
            self._show(title, body)
        return True

    def _show(self, title: str, body: str) -> None:
        try:
            ok = bool(self.toast(title, body))
        except Exception:
            ok = False
        if not ok:
            self._log(f"(Windows notification could not be shown: {title})")

    def _worker(self) -> None:
        while True:
            title, body = self._queue.get()
            self._show(title, body)

    def _log(self, line: str) -> None:
        try:
            self.log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}")
        except Exception:
            pass


def file_log(path: Path):
    """A log function for Notifier: prints the line and appends it to `path`."""
    def write(line: str) -> None:
        print(line, flush=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return write


OUTBOX_SHOW_MAX = 3


def deliver_outbox(health_db: Path, notifier: Notifier) -> int:
    """Deliver every queued notification (e.g. from the watchdog) once, and mark it delivered.

    Each one is logged. If many have piled up (for example from before the supervisor existed), only the
    newest OUTBOX_SHOW_MAX are shown individually, plus one notification saying how many older ones went
    to the log, instead of a flood of pop-ups."""
    if not Path(health_db).exists():
        return 0
    from .health import HealthBoard
    hb = HealthBoard(health_db)
    try:
        pending = hb.pending_notifications()
        older, newest = pending[:-OUTBOX_SHOW_MAX], pending[-OUTBOX_SHOW_MAX:]
        if older:
            for note in older:
                notifier._log(f"[{note['level'].upper()}] (earlier) {note['title']}: {note['body']}")
                hb.mark_delivered(note["id"])
            notifier.send("warning", f"{len(older)} earlier ATLAS notifications",
                          "They were written to the notifications log in the logs folder.")
        for note in newest:
            notifier.send(note["level"], note["title"], note["body"])
            hb.mark_delivered(note["id"])
        return len(pending)
    finally:
        hb.con.close()
