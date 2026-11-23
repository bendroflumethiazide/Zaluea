"""Time for ATLAS: accounting days and clock verification.

The accounting day runs midnight to midnight America/New_York. Daily limits
depend on it, so the local clock must be *verified* against trusted
references (exchange server time) before ATLAS will open positions or roll
over to a new day.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

ACCOUNTING_TZ = ZoneInfo("America/New_York")
MAX_SKEW_SECONDS = 2.0
VERIFY_EVERY_SECONDS = 300.0


def accounting_day(ts: float, tz: ZoneInfo = ACCOUNTING_TZ) -> str:
    """'YYYY-MM-DD' of the accounting day containing unix time ts."""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).strftime("%Y-%m-%d")


@dataclass
class ClockStatus:
    verified: bool
    skew_seconds: float | None
    checked_at: float | None
    reason: str


class ClockGuard:
    """Verifies the local clock against reference sources.

    `references` is a list of callables that each return a trusted unix time
    (for example the Date header of an exchange response) or raise. The
    clock counts as verified only if at least one reference answered within
    the last VERIFY_EVERY_SECONDS and every answering reference agrees with
    the local clock within MAX_SKEW_SECONDS.
    """

    def __init__(self, references: Iterable[Callable[[], float]],
                 local: Callable[[], float] = time.time):
        self.references = list(references)
        self.local = local
        self.status = ClockStatus(False, None, None, "Clock has not been verified yet")

    def check(self) -> ClockStatus:
        skews = []
        for ref in self.references:
            try:
                before = self.local()
                theirs = float(ref())
                after = self.local()
                skews.append((before + after) / 2 - theirs)
            except Exception:  # an unreachable reference simply doesn't count
                continue
        now = self.local()
        if not skews:
            self.status = ClockStatus(False, None, now, "No time reference reachable")
        else:
            worst = max(skews, key=abs)
            ok = abs(worst) <= MAX_SKEW_SECONDS
            self.status = ClockStatus(ok, worst, now,
                                      "Clock verified" if ok else
                                      f"Local clock off by {worst:+.1f} s (limit {MAX_SKEW_SECONDS} s)")
        return self.status

    def verified_now(self) -> tuple[bool, str]:
        s = self.status
        if not s.verified or s.checked_at is None:
            return False, s.reason
        if self.local() - s.checked_at > VERIFY_EVERY_SECONDS:
            return False, "Clock verification is out of date"
        return True, "Clock verified"

    def verified_day(self) -> str | None:
        """Today's accounting day, or None if the clock is not verified."""
        ok, _ = self.verified_now()
        return accounting_day(self.local()) if ok else None


def http_date_reference(url: str, timeout: float = 5.0) -> Callable[[], float]:
    """A reference that reads the Date header of an HTTPS response."""
    def ref() -> float:
        import email.utils
        import urllib.request
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return email.utils.parsedate_to_datetime(r.headers["Date"]).timestamp()
    return ref
