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
import subprocess
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
    """send() never raises: a notification problem must not stop the thing that is reporting a problem."""

    def __init__(self, toast=None, log=print, clock=time.time):
        self.toast = toast if toast is not None else (windows_toast if os.name == "nt" else None)
        self.log, self.clock = log, clock
        self._last: dict[str, float] = {}

    def send(self, level: str, title: str, body: str, *, key: str | None = None, every_s: float = 0) -> bool:
        """Show a notification. With key/every_s, the same key is shown at most once per every_s seconds."""
        now = self.clock()
        if key is not None and now - self._last.get(key, float("-inf")) < every_s:
            return False
        if key is not None:
            self._last[key] = now
        try:
            self.log(f"{time.strftime('%H:%M:%S')} [{level.upper()}] {title}: {body}")
        except Exception:
            pass
        shown = False
        if self.toast is not None:
            try:
                shown = bool(self.toast(title, body))
            except Exception:
                shown = False
        return shown or self.toast is None


def deliver_outbox(health_db: Path, notifier: Notifier) -> int:
    """Show every queued notification (e.g. from the watchdog) once, then mark it delivered."""
    if not Path(health_db).exists():
        return 0
    from .health import HealthBoard
    hb = HealthBoard(health_db)
    try:
        n = 0
        for note in hb.pending_notifications():
            if notifier.send(note["level"], note["title"], note["body"], key=f"outbox-{note['id']}", every_s=300):
                hb.mark_delivered(note["id"])
                n += 1
        return n
    finally:
        hb.con.close()
