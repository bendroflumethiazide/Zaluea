"""The owner's connection check, run against the Kalshi simulator."""
import io
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import atlas.check_connection as cc
from atlas.exchange.fake import FakeExchange
from atlas.exchange.kalshi import DEMO, KalshiExchange, Signer

from .kalshi_sim import KalshiSim


def setup(monkeypatch, now):
    fake = FakeExchange(clock=lambda: now)
    fake.add_market("KXBTC15M-26NOV231200-00", "KXBTC15M-26NOV231200", yes=("0.40", "0.42", 100))
    sim = KalshiSim(fake)
    pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption())
    monkeypatch.setattr(cc, "load_signer", lambda kid, f: Signer(kid, pem))
    monkeypatch.setattr(cc, "KalshiExchange",
                        lambda base, signer, subaccount=None: KalshiExchange(base, signer, subaccount=subaccount,
                                                                             transport=sim, clock=lambda: now,
                                                                             min_interval=0))
    listing = json.dumps({"markets": [{"ticker": "KXBTC15M-26NOV231200-00", "exchange_index": 0,
                                       "market_type": "binary"}]}).encode()
    monkeypatch.setattr(cc.urllib.request, "urlopen", lambda url, timeout=20: io.BytesIO(listing))
    monkeypatch.setattr(cc.time, "time", lambda: now)
    return fake, sim


def args(**kw):
    base = dict(kalshi_key_id="abcd1234", kalshi_key_file="k.pem", kalshi_env="demo", subaccount=None,
                demo_order_test=False, alpaca_key_id=None, alpaca_secret_file=None, alpaca_live=False,
                apply_safe_settings=False)
    return SimpleNamespace(**(base | kw))


def test_read_only_check_places_nothing(monkeypatch, capsys):
    import time
    fake, sim = setup(monkeypatch, time.time())
    assert cc.check_kalshi(args())
    assert not [r for r in sim.requests if r[0] != "GET"]
    out = capsys.readouterr().out
    assert "abcd1234" not in out and "…1234" in out              # key id masked


def test_demo_order_test_cannot_fill(monkeypatch, capsys):
    import time
    fake, sim = setup(monkeypatch, time.time())
    assert cc.check_kalshi(args(demo_order_test=True))
    (o,) = fake.orders.values()
    assert o.qty == 1 and o.filled == 0 and o.status == "cancelled" and o.limit == 10_000   # $0.01
    assert fake.positions() == []


def test_demo_order_test_refuses_production(monkeypatch):
    import time
    fake, sim = setup(monkeypatch, time.time())
    k = KalshiExchange(DEMO, None, transport=sim, min_interval=0)
    assert cc.demo_order_test(k, args(kalshi_env="prod", demo_order_test=True)) is False
    assert fake.orders == {}


def test_saved_config_supplies_ids_but_never_secrets(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"kalshi_key_id": "29cf-demo", "kalshi_key_file": "k.key",
                               "private_key": "-----BEGIN PRIVATE KEY-----", "secret": "x"}), encoding="utf-8")
    loaded = cc.load_config(cfg)
    assert loaded == {"kalshi_key_id": "29cf-demo", "kalshi_key_file": "k.key"}


def test_config_with_windows_bom_is_read(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"kalshi_env": "demo"}).encode())
    assert cc.load_config(cfg) == {"kalshi_env": "demo"}


def test_order_test_skips_unfunded_shards(monkeypatch, capsys):
    import time
    fake, sim = setup(monkeypatch, time.time())
    k = KalshiExchange(DEMO, None, transport=sim, min_interval=0)
    a = args(demo_order_test=True)
    a.funded_shards = {2}                       # the only listed market is on shard 0
    assert cc.demo_order_test(k, a) is False
    assert fake.orders == {} and "No open market on a shard" in capsys.readouterr().out
