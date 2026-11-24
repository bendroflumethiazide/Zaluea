"""Backtest the stock/ETF rules fixed in docs/09-stocks-plan.md, on Alpaca daily bars.

    python research/stocks_backtest.py                 download (needs your Alpaca PAPER keys), then report
    python research/stocks_backtest.py --offline       report from the files already downloaded

Reads only market data: it cannot place orders. Keys come from the settings check_connection saved
(the key ID and the PATH to the secret file; the secret itself is never printed or stored here).
Writes research/data/stocks/<SYMBOL>.csv and research/data/stocks-backtest-report.md, and prints the report.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.analytics.daily import (RULES, Bar, judge_pullback, judge_trend, simulate, window)  # noqa: E402

DATA = ROOT / "research" / "data" / "stocks"
REPORT = ROOT / "research" / "data" / "stocks-backtest-report.md"
SYMBOLS = ["SPY", "QQQ", "IWM"]
DOWNLOAD_FROM = "2015-03-02"          # 200 trading days of warm-up before 2016-01-04
EARLY, RECENT = ("2016-01-04", "2021-12-31"), ("2022-01-01", "2100-01-01")


def download(symbols: list[str]) -> None:
    from atlas.check_connection import load_config
    from atlas.exchange.alpaca import AlpacaClient
    cfg = load_config()
    if not cfg.get("alpaca_key_id") or not cfg.get("alpaca_secret_file"):
        raise SystemExit("No Alpaca paper keys saved yet. Follow docs/04-connecting.md (Alpaca part), "
                         "then run this again.")
    client = AlpacaClient(cfg["alpaca_key_id"], Path(cfg["alpaca_secret_file"]).read_text().strip(), paper=True)
    end = (date.today() - timedelta(days=1)).isoformat()
    DATA.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        bars = client.daily_bars(sym, DOWNLOAD_FROM, end)
        with open(DATA / f"{sym}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "close"])
            for b in bars:
                w.writerow([b["date"], b["o"], b["c"]])
        print(f"{sym}: {len(bars)} daily bars, {bars[0]['date'] if bars else '-'} to {bars[-1]['date'] if bars else '-'}")


def load(sym: str) -> list[Bar]:
    with open(DATA / f"{sym}.csv", newline="") as fh:
        rows = [Bar(r["date"], float(r["open"]), float(r["close"])) for r in csv.DictReader(fh)]
    days = [b.day for b in rows]
    if days != sorted(days) or len(set(days)) != len(days):
        raise SystemExit(f"{sym}: dates are not strictly increasing; re-download")
    if any(b.o <= 0 or b.c <= 0 for b in rows):
        raise SystemExit(f"{sym}: non-positive price in the data; re-download")
    return rows


def pct(x):
    return "–" if x is None else f"{x:.1%}"


def report(symbols: list[str]) -> str:
    out = ["# Stocks and ETFs backtest", "",
           "Rules and pass/fail tests were fixed in [docs/09-stocks-plan.md](../../docs/09-stocks-plan.md) before "
           "any data was downloaded. Costs 0.05% per buy and per sell; trades at the next day's open; cash earns 0.", ""]
    trend_in, pull_trades, pull_days, bh_growth, bh_days = {}, [], 0, 1.0, 0
    for sym in symbols:
        bars = load(sym)
        out += [f"## {sym}  ({bars[0].day} to {bars[-1].day}, {len(bars)} bars)", "",
                "| Rule | Period | Annual return | Worst fall | Trades | Time invested |", "|---|---|---|---|---|---|"]
        res = {name: simulate(bars, rule) for name, rule in RULES.items()}
        for name, r in res.items():
            for label, (d0, d1) in (("2016–2021", EARLY), ("2022–", RECENT)):
                m = window(r, d0, d1)
                inv = f"{m['days_in'] / m['days']:.0%}" if m["days"] else "–"
                out.append(f"| {name} | {label} | {pct(m['cagr'])} | {pct(m['max_dd'])} | {len(m['trades'])} | {inv} |")
        s0, s1, s2 = (window(res[n], *RECENT) for n in RULES)
        trend_in[sym] = (s1, s0)
        pull_trades += s2["trades"]
        pull_days += s2["days_in"]
        bh_growth *= 1 + s0["total"]
        bh_days += s0["days"]
        out.append("")
    bh_daily = bh_growth ** (1 / bh_days) - 1 if bh_days else 0.0
    trend = judge_trend(trend_in)
    pull = judge_pullback(pull_trades, bh_daily, pull_days)
    out += ["## Verdicts (2022 onward)", "",
            f"**S1 trend filter: {'PASS' if trend['passed'] else 'FAIL'}** (needs SPY and one of the others)", ""]
    out += [f"- {s}: {'✅' if trend['per_symbol_pass'][s] else '❌'} {d}" for s, d in trend["detail"].items()]
    out += ["", f"**S2 pullback: {'PASS' if pull['passed'] else 'FAIL'}** (pooled over {', '.join(symbols)})", ""]
    out += [f"- {'✅' if ok else '❌'} {d}" for ok, d in pull["checks"]]
    winners = [n for n, j in (("S1 trend filter", trend), ("S2 pullback", pull)) if j["passed"]]
    out += ["", "## Outcome", "",
            (f"{' and '.join(winners)} passed and may go to a live shadow test with its own pre-set rules. "
             "Live trading stays off." if winners else
             "No rule passed. As planned, the stocks module becomes scheduled buying of SPY within your limits, "
             "with no trading rule."), ""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="use the files already downloaded")
    a = ap.parse_args(argv)
    if not a.offline:
        download(SYMBOLS)
    text = report(SYMBOLS)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved to {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
