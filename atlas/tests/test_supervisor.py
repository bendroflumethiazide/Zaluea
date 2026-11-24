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
    assert ev["verdict"] == "FAIL" and "do not reconcile" in ev["why"] and ev["rules"][3]["value"] == "BOOKS DO NOT MATCH"
    early = evaluate({"closed": good[:50], "closed_ts": stamps[:50]}, summary(50, 40), summary(reconciled=False))
    assert early["verdict"] == "FAIL" and "not reliable" in early["why"]       # not hidden behind "NOT YET"
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
    lines = []
    assert Notifier(toast=boom, log=lines.append).send("info", "x", "y") is True     # logged; toast failure noted
    assert "[INFO] x: y" in lines[0] and "could not be shown" in lines[1]


def test_a_slow_toast_never_holds_up_the_caller():
    slow = lambda t, b: time.sleep(2) or True
    n = Notifier(toast=slow, log=lambda *_: None, background=True)
    t0 = time.monotonic()
    for i in range(30):                                    # more than the queue holds: extras are skipped
        n.send("warning", f"t{i}", "b")
    assert time.monotonic() - t0 < 0.5


def test_queued_watchdog_notifications_are_delivered_once_even_if_the_toast_fails(tmp_path):
    hb = HealthBoard(tmp_path / "health.db", create=True)
    hb.notify("critical", "Watchdog stopped new trading", "ledger mismatch")
    hb.con.close()
    failing, sent = quiet(toast_ok=False)
    assert deliver_outbox(tmp_path / "health.db", failing) == 1      # logged and marked: never retried forever
    assert deliver_outbox(tmp_path / "health.db", failing) == 0
    assert sent == [("Watchdog stopped new trading", "ledger mismatch")]
    assert deliver_outbox(tmp_path / "missing.db", failing) == 0


def test_a_backlog_of_old_notifications_does_not_flood_the_screen(tmp_path):
    hb = HealthBoard(tmp_path / "health.db", create=True)
    for i in range(8):
        hb.notify("warning", f"note {i}", "body")
    hb.con.close()
    lines = []
    n = Notifier(toast=lambda t, b: sent.append(t) or True, log=lines.append)
    sent = []
    assert deliver_outbox(tmp_path / "health.db", n) == 8
    assert sent == ["5 earlier ATLAS notifications", "note 5", "note 6", "note 7"]
    assert sum("note " in x for x in lines) == 8                       # every one is in the log
    assert deliver_outbox(tmp_path / "health.db", n) == 0


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
        assert c.proc is None and c.restarts == 1 and "code 3" in c.last_exit
        assert c.next_start == clock.t + 2                # first retry after 2 s, as documented
        clock.t += 1
        sup.poll()
        assert c.proc is None                            # still waiting out the backoff
        clock.t += 1
        sup.poll()
        wait_exit(c)
        sup.poll()
        assert c.restarts == 2 and c.next_start == clock.t + 4          # then 4 s, 8 s ...
        assert len(sent) == 1 and "Crashy stopped" in sent[0][0] and "in 2 s" in sent[0][1]   # one alert
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
        assert c.next_start == clock.t + 2
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


def test_waking_from_sleep_is_not_mistaken_for_a_freeze(tmp_path):
    mono = [0.0]
    c = Child("napper", [PY, "-c", "import time; time.sleep(60)"], "Napper", stale_after_s=30,
              heartbeat=lambda: 1000.0)               # last heartbeat written just before the PC slept
    sup, sent, clock = make_sup(tmp_path, [c], monotonic=lambda: mono[0])
    sup.start()
    try:
        sup.poll()
        proc = c.proc
        clock.t += 8 * 3600                               # 8 hours of sleep: wall clock jumps,
        mono[0] += 8 * 3600                               # and the gap between passes is huge
        sup.poll()
        assert c.proc is proc and not sent                # given a grace period, not killed
        for _ in range(31):                               # then normal 1-second passes with no new heartbeat
            clock.t += 1
            mono[0] += 1
            sup.poll()
        assert c.proc is None and "Napper froze" in sent[0][0]   # still silent afterwards: then it is frozen
    finally:
        sup.stop()


def test_a_copy_already_running_elsewhere_is_reported_not_restarted_in_a_loop(tmp_path):
    c = Child("dup", [PY, "-c", "raise SystemExit(75)"], "Dup")
    sup, sent, clock = make_sup(tmp_path, [c])
    sup.start()
    try:
        sup.poll()
        wait_exit(c)
        sup.poll()
        assert c.next_start == clock.t + 300 and "already running" in sent[0][0]
    finally:
        sup.stop()


def test_one_bad_pass_does_not_stop_supervision(tmp_path):
    sup, sent, _ = make_sup(tmp_path, [])
    sup.start()
    try:
        def broken(now):
            raise KeyError("account")
        sup._maybe_summary = broken
        sup.safe_poll()                                   # must not raise
        assert "carried on" in sent[0][0] and "KeyError" in sent[0][1]
    finally:
        sup.stop()


def test_damaged_shadow_results_give_a_warning_summary_not_a_crash(tmp_path):
    data = tmp_path / "data"
    (data / "sim").mkdir(parents=True)
    for bad in ('{"simulated": true, "accounts": [{}]}', '[1, 2]', '{"ts": "x", "accounts": "?"}',
                '{"ts": 1, "accounts": [], "evidence": {"m": {"rules": []}}}'):
        (data / "sim" / "shadow.json").write_text(bad)
        level, title, _ = daily_summary(data, MON)
        assert level in ("warning", "info", "critical") and title


def test_status_file_replace_retries_while_a_reader_has_it_open(tmp_path, monkeypatch):
    from atlas.storage import write_atomic
    real = Path.replace
    fails = [2]

    def flaky(self, target):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError(5, "Access is denied")       # what Windows raises mid-read
        return real(self, target)
    monkeypatch.setattr(Path, "replace", flaky)
    write_atomic(tmp_path / "s.json", '{"ok": 1}', delay_s=0)
    assert json.loads((tmp_path / "s.json").read_text()) == {"ok": 1}
    fails[0] = 99
    with pytest.raises(PermissionError):
        write_atomic(tmp_path / "s.json", "{}", attempts=3, delay_s=0)


@pytest.mark.parametrize("module,lock", [("atlas.shadow", "sim/shadow.lock"), ("atlas.scanner", "scanner.lock")])
def test_a_second_shadow_runner_or_scanner_refuses_to_start(tmp_path, module, lock):
    import subprocess
    from atlas.storage import InstanceLock
    held = InstanceLock(tmp_path / lock)
    held.acquire()
    try:
        r = subprocess.run([PY, "-m", module, "--data", str(tmp_path)], cwd=ROOT, capture_output=True,
                           text=True, timeout=60, env={**__import__("os").environ, "HOME": str(tmp_path)})
        assert r.returncode == 75 and "already running" in r.stdout
    finally:
        held.release()
