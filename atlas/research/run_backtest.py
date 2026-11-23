"""Run the walk-forward backtest of the crypto flip-favorite strategy.

    python research/run_backtest.py [--only-with-quotes]

Reads research/data/ (see collect_btc15m.py and collect_reference.py) and
writes docs/06-btc15m-backtest.md. The success criteria are fixed in
atlas/analytics/backtest.py (CRITERIA) before any result is seen.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.analytics.backtest import CRITERIA, GRID, load, model_calibration, stats, walk_forward  # noqa: E402
from atlas.analytics.probability import brier, calibration, log_loss  # noqa: E402
from atlas.money import Money  # noqa: E402
from atlas.strategies.crypto_flip import FlipConfig  # noqa: E402


def d(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "06-btc15m-backtest.md"))
    a = ap.parse_args()
    t0 = time.time()
    markets, ref, meta = load(ROOT / "research" / "data")
    with_quotes = [m for m in markets if m.quotes["yes"]._ts]
    print(f"{len(markets)} markets ({len(with_quotes)} with quotes), {meta['reference_minutes']} reference minutes",
          flush=True)
    res = walk_forward(with_quotes, ref)
    print(f"walk-forward done in {time.time() - t0:.0f} s: {res.verdict}", flush=True)
    model, market = model_calibration(with_quotes, ref, FlipConfig())
    s = res.meta["oos"]

    L = []
    w = L.append
    w("# BTC 15-minute flip-favorite: walk-forward backtest\n")
    w(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `research/run_backtest.py`.\n")
    w(f"## Verdict: **{res.verdict}**\n")
    w("**Criteria, fixed before any result was seen** (`atlas/analytics/backtest.py`). Over the out-of-sample weeks combined:")
    w(f"- at least **{CRITERIA['min_oos_trades']} trades**;")
    w(f"- a **mean profit per trade above $0, with a t-statistic of at least {CRITERIA['min_t_stat']}**;")
    w(f"- **at least {CRITERIA['min_share_weeks_non_negative']:.0%} of test weeks** not losing money.\n")
    w("## Data\n")
    w(f"- **Markets:** {len(with_quotes):,} settled KXBTC15M markets with minute quotes, "
      f"{d(meta['first'])} to {d(meta['last'])}. Older markets come from Kalshi's archive endpoints, newer ones from the live API.")
    w(f"- **BTC reference:** the mean of Coinbase and Bitstamp 1-minute closes ({meta['reference_minutes']:,} minutes). "
      f"{meta['reference_minutes_dropped']:,} minutes were dropped because the two sources disagreed by more than 0.30%.")
    w("- **Strike:** Kalshi's own `floor_strike` for each market.\n")
    w("## Method\n")
    w("- **Decisions:** made at minutes 10, 11, 12 and 13 of each window, using only data available at that moment. "
      "The strategy receives time-cut data that raises an error on any later access.")
    w("- **Fills:** at the quoted ask, plus **$0.01 slippage per contract**, plus the **exact Kalshi fee** rounded up to the cent. "
      "Each trade is sized to at most $2 including fees, with at most one trade per market.")
    w("- **Walk-forward:** each week's parameters are chosen using only the weeks before it, by best training profit "
      f"among {res.meta['configs']} configurations, then applied unchanged to that week. Tuned settings: "
      + ", ".join(f"`{k}` ∈ {{{', '.join(v)}}}" for k, v in GRID.items()) + ".")
    w("- **Known optimism:** historical order-book depth isn't available, so every fill at the ask is assumed to succeed. "
      "Real fills can be smaller or worse. A positive result here is a ceiling, not a promise.\n")
    w("## Out-of-sample result (all test weeks combined)\n")
    w("| Trades | Won | Win rate | Net P&L | Spent | Return on spend | Mean per trade | t-statistic | Weeks not losing |")
    w("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    roi = s["pnl"].units / s["cost"].units if s["cost"].units else 0.0
    w(f"| {s['trades']} | {s['wins']} | {s['wins'] / max(1, s['trades']):.1%} | {s['pnl']} | {s['cost']} | {roi:+.2%} | "
      f"${s['mean']:+.4f} | {s['t']:.2f} | {res.meta['share_weeks_non_negative']:.0%} |\n")
    w("## Week by week (each week tested with parameters chosen from earlier weeks only)\n")
    w("| Week | Chosen settings | Trades | Won | Net P&L | t |")
    w("|---|---|---:|---:|---:|---:|")
    for wk in res.weeks:
        if not wk.get("config"):
            w(f"| {wk['week']} | {wk.get('note', 'n/a')} | 0 | 0 | $0.00 | |")
            continue
        cfg = ", ".join(f"{k}={v}" for k, v in wk["config"].items())
        w(f"| {wk['week']} | {cfg} | {wk['trades']} | {wk['wins']} | {wk['pnl']} | {wk['t']:.2f} |")
    w("")
    if model:
        w("## Does the model predict better than the market's own price? (minute 12, all markets)\n")
        w(f"- **Model:** Brier score {brier(model):.4f}, log loss {log_loss(model):.4f}.")
        w(f"- **Market mid-price:** Brier score {brier(market):.4f}, log loss {log_loss(market):.4f}.")
        w("- Lower is better. If the market's own price scores better, there is little information left for this model to exploit.\n")
        w("| Model's probability | Markets | Mean predicted | Actually happened | 95% interval | Consistent? |")
        w("|---|---:|---:|---:|---|---|")
        for b in calibration(model):
            w(f"| {b.lo_bp / 100:.0f}–{b.hi_bp / 100:.0f}% | {b.n} | {b.mean_pred:.1%} | {b.observed:.1%} | "
              f"{b.ci_low:.1%}–{b.ci_high:.1%} | {'yes' if b.consistent else '**no**'} |")
        w("")
    Path(a.out).write_text("\n".join(L) + "\n")
    print(f"report written to {a.out}", flush=True)


if __name__ == "__main__":
    main()
