"""Part 25: audit logging tests."""
import sqlite3
import threading

import pytest

from atlas.audit import AuditLog
from atlas.money import Money

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"


@pytest.fixture
def log(tmp_path):
    a = AuditLog(tmp_path / "audit.db", create=True)
    yield a
    a.close()


def test_records_every_field_and_verifies(log):
    log.append("trade_proposal", asset="KXBTC15M-X", strategy="flip_fav", model_version="v1",
               decision="rejected", reason="spread too wide",
               data={"max_cost": Money.parse("1.78"), "prob": 0.91})
    log.append("risk_decision", decision="approved", data={"remaining": Money.dollars(18)})
    r = log.verify()
    assert r.ok and r.count == 2
    ev = log.export()
    assert ev[0]["category"] == "trade_proposal" and ev[0]["data"]["max_cost"] == "1.7800"
    assert ev[0]["event_id"] and ev[0]["ts"] and ev[1]["prev_hash"] == ev[0]["hash"]


def test_triggers_block_update_and_delete(log):
    log.append("x")
    with pytest.raises(sqlite3.DatabaseError):
        log.con.execute("UPDATE events SET reason='edited' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError):
        log.con.execute("DELETE FROM events WHERE seq=1")


def test_detects_modified_record(log):
    for i in range(5):
        log.append("event", reason=f"r{i}")
    log.con.execute("DROP TRIGGER events_no_update")          # attacker goes around the trigger
    log.con.execute("UPDATE events SET reason='forged' WHERE seq=3")
    r = log.verify()
    assert not r.ok and r.first_bad_seq == 3 and "modified" in r.reason


def test_detects_broken_chain_and_gaps(log):
    for i in range(4):
        log.append("event", reason=f"r{i}")
    log.con.execute("DROP TRIGGER events_no_delete")
    log.con.execute("DELETE FROM events WHERE seq=2")
    r = log.verify()
    assert not r.ok and r.first_bad_seq == 3


def test_anchor_catches_truncation(log):
    for i in range(5):
        log.append("event", reason=f"r{i}")
    anchor = log.head()
    log.con.execute("DROP TRIGGER events_no_delete")
    log.con.execute("DELETE FROM events WHERE seq=5")         # chain of 1..4 is still valid
    assert log.verify().ok
    ok, why = log.check_anchor(*anchor)
    assert not ok and "shorter" in why


def test_survives_restart(tmp_path):
    p = tmp_path / "audit.db"
    a = AuditLog(p, create=True)
    a.append("restart", reason="first run")
    a.close()
    b = AuditLog(p)
    b.append("restart", reason="second run")
    assert b.verify().ok and b.verify().count == 2
    b.close()


def test_missing_log_is_an_error_not_a_fresh_start(tmp_path):
    with pytest.raises(Exception):
        AuditLog(tmp_path / "gone.db")


def test_credentials_never_enter_the_log(log):
    log.append("api_error", reason="auth failed",
               data={"api_key": "AKIA123SECRET", "headers": {"Authorization": "Bearer abc"},
                     "KALSHI-ACCESS-SIGNATURE": "sig==", "body": PEM,
                     "nested": [{"private_key_path": "C:/keys/k.pem"}], "ticker": "SPY"})
    raw = log.con.execute("SELECT data, reason FROM events").fetchone()
    text = raw["data"] + raw["reason"]
    for secret in ("AKIA123SECRET", "Bearer abc", "sig==", "MIIEow", "C:/keys/k.pem"):
        assert secret not in text
    assert "SPY" in text


def test_concurrent_appends_keep_one_valid_chain(tmp_path):
    p = tmp_path / "audit.db"
    AuditLog(p, create=True).close()

    def worker(n):
        a = AuditLog(p)
        for i in range(25):
            a.append("event", reason=f"{n}-{i}")
        a.close()

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    a = AuditLog(p)
    r = a.verify()
    assert r.ok and r.count == 100
