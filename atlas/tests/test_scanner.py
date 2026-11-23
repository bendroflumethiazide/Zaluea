"""Market scanner with stand-in data sources (no network)."""
import json

from atlas.marketdata.btc import BtcReference, PricePoint
from atlas.money import Money
from atlas.scanner import Scanner

OPEN = 1_795_400_000.0
M = Money.parse


class FakeKalshi:
    def __init__(self):
        self.result = None

    def open_markets(self, series, limit=5):
        if series == "KXBTC15M":
            return [{"ticker": "KXBTC15M-T", "close_time": "x"}]
        return [{"ticker": "KXCS2GAME-26NOV221925RUSALKAA-RUS", "rules_primary": "If Rush wins the CCT 2026: "
                 "Rush vs. ALKA CS2 match originally scheduled for Nov 22, 2026 at 7:25 PM EST, then ..."}]

    def market_info(self, t):
        return {"event": "KXBTC15M-E", "close_ts": OPEN + 900, "open_ts": OPEN, "strike": "60000.00",
                "fee_type": "quadratic", "fee_multiplier": "1"}

    def orderbook(self, t, side):
        bid, ask = ("0.9000", "0.9100") if side == "yes" else ("0.0900", "0.1000")
        return {"bid": bid, "ask": ask, "ask_size": 50, "ts": clock[0]}

    def market_result(self, t):
        return self.result


clock = [OPEN + 12 * 60]


def history(minutes):
    pts, p = [], 60400.0
    for i in range(minutes):
        p += 12.0 if i % 2 else -12.0
        pts.append((clock[0] - (minutes - 1 - i) * 60, M(f"{p:.2f}")))
    return pts


def src(name):
    def f(c):
        return PricePoint(name, M("60400.00"), clock[0], clock[0])
    f.__name__ = name
    return f


def test_scan_writes_rows_logs_one_prediction_and_scores_it(tmp_path):
    k = FakeKalshi()
    s = Scanner(tmp_path, kalshi=k, reference=BtcReference((src("a"), src("b")), clock=lambda: clock[0]),
                history=history, clock=lambda: clock[0])
    out = s.scan_once()
    rows = {r["category"]: r for r in out["rows"]}
    assert rows["kalshi"]["verdict"] == "shadow" and "cannot trade" in rows["kalshi"]["reason"]
    assert rows["esports"]["verdict"] == "no_trade" and "live game-state feed" in rows["esports"]["reason"]
    assert rows["stocks"]["verdict"] == "no_trade"
    assert json.loads((tmp_path / "scanner.json").read_text())["rows"]
    s.scan_once()                                                      # same market: still one prediction
    assert s.log.con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    clock[0] = OPEN + 1000                                             # after close
    k.result = "yes"
    s.scan_once()
    sc = s.log.scorecard(strategy=s.strategy.name)
    assert sc.n == 1 and not sc.validated
    clock[0] = OPEN + 12 * 60


class FakeAlpaca:
    """Read-only stand-in. Any order call would raise AttributeError (not defined)."""
    secret = "super-secret-value"

    def __init__(self, problems=(), is_open=False):
        self.problems, self.is_open = list(problems), is_open

    def safety_problems(self):
        return self.problems

    def market_clock(self):
        return {"is_open": self.is_open}

    def latest_quote(self, symbol):
        wide = symbol == "SPY"
        return {"symbol": symbol, "bid": "671.14" if wide else "100.00", "ask": "714.05" if wide else "100.02",
                "bid_size": 1, "ask_size": 3, "ts": clock[0], "source": "alpaca:iex"}


def make(tmp_path, alpaca):
    return Scanner(tmp_path, kalshi=FakeKalshi(), reference=BtcReference((src("a"), src("b")), clock=lambda: clock[0]),
                   history=history, clock=lambda: clock[0], alpaca=alpaca)


def test_equity_rows_explain_closed_market_and_wide_spread(tmp_path):
    out = make(tmp_path, FakeAlpaca()).scan_once()
    eq = {r["market"]: r for r in out["rows"] if r["category"] in ("stocks", "etfs")}
    assert set(eq) == {"SPY", "QQQ", "AAPL", "MSFT", "NVDA"}
    assert all(r["verdict"] == "no_trade" for r in eq.values())
    assert "Market closed" in eq["SPY"]["reason"] and "above the 0.30% limit" in eq["SPY"]["reason"]
    assert "above" not in eq["AAPL"]["reason"] and eq["AAPL"]["price"] == 100.02
    assert eq["SPY"]["category"] == "etfs" and eq["AAPL"]["category"] == "stocks"


def test_unsafe_account_is_reported(tmp_path):
    out = make(tmp_path, FakeAlpaca(problems=["Short selling is enabled"], is_open=True)).scan_once()
    spy = next(r for r in out["rows"] if r["market"] == "SPY")
    assert "Account not safe: Short selling is enabled" in spy["reason"]


def test_secret_never_reaches_scanner_output(tmp_path):
    make(tmp_path, FakeAlpaca()).scan_once()
    assert FakeAlpaca.secret not in (tmp_path / "scanner.json").read_text()


def test_scanner_never_places_orders():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / "atlas" / "scanner.py").read_text())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"place_order", "cancel", "apply_safe_settings", "order_body"}
