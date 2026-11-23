"""Keeping the research stack alive: decision-rule evidence, notifications, and the supervisor."""
from __future__ import annotations

import ast
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas.health import HealthBoard
from atlas.notify import Notifier, deliver_outbox, windows_toast
from atlas.shadow.evidence import MIN_SETTLED, evaluate, t_statistic, weekly_totals
from atlas.storage import StorageError
from atlas.supervisor import Child, Supervisor, daily_summary, default_children

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
D = 1_000_000            # one dollar in money units
MON = datetime(2026, 11, 23, 12).timestamp()      # a Monday (New York)


def quiet(sent=None, toast_ok=True):
    sent = sent if sent is not None else []
    return Notifier(toast=lambda t, b: sent.append((t, b)) or toast_ok, log=lambda *_: None), sent


def summary(settled=0, wins=0, missed=0, missed_wins=0, reconciled=True):
    return {"settled": settled, "win_rate_pct": round(100 * wins / settled, 1) if settled else None,
            "missed_resolved": missed,
            "missed_win_rate_pct": round(100 * missed_wins / missed, 1) if missed else None,
            "reconciled": reconciled}


# ----------------------------------------------------------------------------- evidence
def test_t_statistic_matches_the_textbook_formula_and_is_undefined_when_it_should_be():
    assert t_statistic([]) is None and t_statistic([5]) is None and t_statistic([3, 3, 3]) is None
    # mean 2, sample sd 1, n 4 -> t = 2 / (1/2) = 4
    assert t_statistic([1, 2, 3, 2]) == pytest.approx(2 / ((2 / 3) ** 0.5 / 2))


def test_weeks_are_new_york_calendar_weeks():
    sun_late_ny = datetime(2026, 11, 30, 3, tzinfo=timezone.utc).timestamp()   # Monday in UTC, Sunday 22:00 in NY
    w = weekly_totals([1, 2, -5], [MON, MON + 86400, MON + 7 * 86400])
    assert w == {"2026-W48": 3, "2026-W49": -5}
    assert weekly_totals([1], [sun_late_ny]) == {"2026-W48": 1}


def test_verdict_is_not_yet_until_two_hundred_fills_even_if_everything_else_looks_good():
    pnls = [D // 10] * 50
    ev = evaluate({"closed": pnls, "closed_ts": [MON] * 50}, summary(50, 40), summary(reconciled=True))
    assert ev["verdict"] == "NOT YET" and "50 of 200" in ev["why"]
    assert ev["rules"][0]["progress_pct"] == 25 and not ev["rules"][0]["passed"]


def test_verdict_pass_needs_every_rule_and_fail_names_the_rule_that_failed():
    n = MIN_SETTLED
    good = [D // 10 if i % 4 else -D // 20 for i in range(n)]          # profitable, consistent
    stamps = [MON + (i % 5) * 7 * 86400 for i in range(n)]               # spread over 5 weeks
    ev = evaluate({"closed": good, "closed_ts": stamps}, summary(n, 150), summary(reconciled=True))
    assert ev["verdict"] == "PASS" and all(r["passed"] for r in ev["rules"])
    assert "live trading stays off" in ev["why"]

    bad = [-x for x in good]
    ev = evaluate({"closed": bad, "closed_ts": stamps}, summary(n, 50), summary(reconciled=True))
    assert ev["verdict"] == "FAIL" and "2, 3" in ev["why"]

    ev = evaluate({"closed": good, "closed_ts": stamps}, summary(n, 150), summary(reconciled=False))
    assert ev["verdict"] == "FAIL" and "rule 4" in ev["why"] and ev["rules"][3]["value"] == "BOOKS DO NOT MATCH"
    assert evaluate({"closed": good, "closed_ts": stamps}, summary(n, 150), None)["verdict"] == "FAIL"


def test_picked_off_diagnostic_needs_a_real_gap_and_enough_samples():
    tot = {"closed": [D] * 30, "closed_ts": [MON] * 30}
    assert evaluate(tot, summary(30, 12, 30, 21), None)["diagnostic"]["picked_off"]          # 40% vs 70%
    assert not evaluate(tot, summary(30, 18, 30, 21), None)["diagnostic"]["picked_off"]      # 60% vs 70%
    assert not evaluate(tot, summary(30, 2, 5, 5), None)["diagnostic"]["picked_off"]         # too few missed


# ----------------------------------------------------------------------------- notifications
def test_toast_text_travels_in_the_environment_never_in_the_command_line():
    calls = []
    hostile = "BTC'); Remove-Item C:\\ -Recurse; ('<toast>"
    ok = windows_toast(hostile, "body " + hostile,
                       run=lambda argv, **kw: calls.append((argv, kw)) or type("R", (), {"returncode": 0})())
    argv, kw = calls[0]
    assert ok and all(hostile not in a for a in argv) and "-EncodedCommand" in argv
    assert kw["env"]["ATLAS_TOAST_TITLE"] == hostile and kw["env"]["ATLAS_TOAST_BODY"].startswith("body ")


def test_notifier_rate_limits_by_key_and_never_raises():
    t = [0.0]
    n, sent = quiet()
    n.clock = lambda: t[0]
    assert n.send("warning", "a", "b", key="k", every_s=60)
    assert not n.send("warning", "a", "b", key="k", every_s=60)
    t[0] = 61
    assert n.send("warning", "a", "b", key="k", every_s=60) and len(sent) == 2

    def boom(*_):
        raise RuntimeError("no toast")
    assert Notifier(toast=boom, log=lambda *_: None).send("info", "x", "y") is False


def test_queued_watchdog_notifications_are_delivered_once(tmp_path):
    hb = HealthBoard(tmp_path / "health.db", create=True)
    hb.notify("critical", "Watchdog stopped new trading", "ledger mismatch")
    hb.con.close()
    failing, _ = quiet(toast_ok=False)
    assert deliver_outbox(tmp_path / "health.db", failing) == 0                  # not shown: stays queued
    n, sent = quiet()
    assert deliver_outbox(tmp_path / "health.db", n) == 1
    assert deliver_outbox(tmp_path / "health.db", n) == 0 and sent == [("Watchdog stopped new trading", "ledger mismatch")]
    assert deliver_outbox(tmp_path / "missing.db", n) == 0


# ----------------------------------------------------------------------------- supervisor
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_sup(tmp_path, children, clock=None, local=(9, 0), **kw):
    n, sent = quiet()
    clock = clock or Clock()
    n.clock = clock
    sup = Supervisor(tmp_path / "data", children, notifier=n, clock=clock,
                     local_now=lambda: datetime(2026, 11, 23, *local), **kw)
    return sup, sent, clock


def wait_exit(c: Child):
    c.proc.wait(timeout=20)


def test_a_crashing_process_is_restarted_with_growing_backoff_and_one_alert(tmp_path):
    c = Child("crashy", [PY, "-c", "print('hello'); raise SystemExit(3)"], "Crashy")
    sup, sent, clock = make_sup(tmp_path, [c])
    sup.start()
    try:
        sup.poll()                                       # launched
        wait_exit(c)
        sup.poll()                                       # noticed the exit
        assert c.proc is None and c.restarts == 1 and c.backoff == 4 and "code 3" in c.last_exit
        clock.t += 3
        sup.poll()
        assert c.proc is None                            # still waiting out the backoff
        clock.t += 1
        sup.poll()
        wait_exit(c)
        sup.poll()
        assert c.restarts == 2 and c.backoff == 8
        assert len(sent) == 1 and "Crashy stopped" in sent[0][0]          # alert rate-limited to one
        status = json.loads((tmp_path / "data" / "logs" / "supervisor.json").read_text())
        assert status["children"][0]["restarts"] == 2 and not status["children"][0]["running"]
        time.sleep(0.2)
        assert "hello" in (tmp_path / "data" / "logs" / "crashy.log").read_text()
    finally:
        sup.stop()


def test_backoff_resets_after_a_long_healthy_run(tmp_path):
    c = Child("worker", [PY, "-c", "raise SystemExit(1)"])
    sup, _, clock = make_sup(tmp_path, [c])
    sup.start()
    try:
        c.backoff = 64
        sup.poll()
        wait_exit(c)
        clock.t += 700                                    # it "ran" for over 10 minutes
        sup.poll()
        assert c.backoff == 2
    finally:
        sup.stop()


def test_a_frozen_process_is_killed_and_restarted(tmp_path):
    beat = [None]
    c = Child("stuck", [PY, "-c", "import time; time.sleep(60)"], "Stuck", stale_after_s=30,
              heartbeat=lambda: beat[0])
    sup, sent, clock = make_sup(tmp_path, [c])
    sup.start()
    try:
        sup.poll()
        proc = c.proc
        clock.t += 10
        sup.poll()
        assert c.proc is proc                             # too early to judge
        beat[0] = clock.t
        clock.t += 25
        sup.poll()
        assert c.proc is proc                             # heartbeat is fresh enough
        clock.t += 31
        sup.poll()
        assert c.proc is None and proc.poll() is not None and "stopped producing results" in c.last_exit
        assert "Stuck froze" in sent[0][0]
    finally:
        sup.stop()


def test_stop_ends_every_child_and_only_one_supervisor_per_folder(tmp_path):
    c = Child("sleeper", [PY, "-c", "import time; time.sleep(60)"])
    sup, _, _ = make_sup(tmp_path, [c])
    sup.start()
    sup.poll()
    other, _, _ = make_sup(tmp_path, [])
    with pytest.raises(StorageError):
        other.start()
    proc = c.proc
    sup.stop()
    assert proc.poll() is not None
    other.start()                                         # the lock is free again
    other.stop()


def write_shadow(data: Path, ts: float, reconciled=True):
    (data / "sim").mkdir(parents=True, exist_ok=True)
    ev = evaluate({"closed": [D // 10] * 6, "closed_ts": [MON] * 6}, summary(6, 5), summary(reconciled=True))
    (data / "sim" / "shadow.json").write_text(json.dumps({
        "ts": ts, "accounts": [{"account": "crypto_maker/research", "order_type": "maker", "pnl_total": "$0.60",
                                "reconciled": reconciled}],
        "evidence": {"crypto_maker": ev}}))


def test_daily_summary_is_sent_once_a_day_and_survives_a_restart(tmp_path):
    clock = Clock(MON)
    write_shadow(tmp_path / "data", MON)
    sup, sent, _ = make_sup(tmp_path, [], clock=clock, local=(21, 5))
    sup.start()
    sup.poll()
    sup.poll()
    sup.stop()
    assert len(sent) == 1 and "6 of 200 settled fills" in sent[0][1] and "NOT YET" in sent[0][1]
    again, sent2, _ = make_sup(tmp_path, [], clock=clock, local=(22, 0))
    again.start()
    again.poll()
    again.stop()
    assert sent2 == []
    early, sent3, _ = make_sup(tmp_path / "x", [], clock=clock, local=(20, 59))
    early.start()
    early.poll()
    early.stop()
    assert sent3 == []


def test_daily_summary_warns_on_stale_results_and_mismatched_books(tmp_path):
    data = tmp_path / "data"
    assert daily_summary(data, MON)[1] == "Shadow test: no results yet"
    write_shadow(data, MON - 3600)
    level, _, body = daily_summary(data, MON)
    assert level == "warning" and "60 minutes old" in body
    write_shadow(data, MON, reconciled=False)
    assert daily_summary(data, MON)[0] == "critical"


def test_supervisor_starts_only_research_processes_and_holds_no_keys(tmp_path):
    kids = default_children(tmp_path, 8770)
    targets = {c.argv[2] if c.argv[1] == "-m" else Path(c.argv[1]).name for c in kids}
    assert targets == {"atlas.shadow", "atlas.scanner", "server.py"}
    assert not any("key" in a.lower() or "secret" in a.lower() for c in kids for a in c.argv)
    for f in ("supervisor.py", "notify.py"):
        tree = ast.parse((ROOT / "atlas" / f).read_text())
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(x in m for m in mods for x in ("gateway", "exchange", "control", "ledger")), (f, mods)


def test_dashboard_shows_evidence_and_supervisor_status(tmp_path):
    sys.path.insert(0, str(ROOT / "dashboard"))
    from real_state import RealState
    data = tmp_path / "data"
    write_shadow(data, MON)
    rs = RealState(data)
    assert rs._supervisor(MON) is None                    # never run: nothing shown
    (data / "logs").mkdir()
    (data / "logs" / "supervisor.json").write_text(json.dumps({"ts": MON, "children": [
        {"label": "Shadow trading test", "running": True, "restarts": 2, "last_exit": ""}]}))
    sv = rs._supervisor(MON + 5)
    assert sv["running"] and sv["children"][0]["restarts"] == 2
    assert not rs._supervisor(MON + 120)["running"]
