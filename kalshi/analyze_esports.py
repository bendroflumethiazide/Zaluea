"""Study Kalshi esports winner markets and write reports/esports.md.

Run collect_esports.py first.

Key idea: price each market at the match's scheduled start (the last
quote before it), then ask how well those prices predicted the winner and
what buying at the ask would have returned after Kalshi's taker fee.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from kalshi_client import fnum, taker_fee

HERE = Path(__file__).parent
DATA, REPORTS = HERE / "data", HERE / "reports"


def series_quotes(rows):
    """Sorted (end_ts, bid, ask, volume) with bid/ask forward-filled."""
    out, b, a = [], None, None
    for t, nb, na, v in sorted(rows):
        b = nb if nb is not None else b
        a = na if na is not None else a
        out.append((t, b, a, v or 0.0))
    return out


def quote_at(q, ts):
    """Last (bid, ask) at or before ts, or (None, None). Empty book -> None."""
    b = a = None
    for t, qb, qa, _ in q:
        if t > ts:
            break
        b, a = qb, qa
    b = b if b and b > 0 else None
    a = a if a and a < 1 else None
    return b, a


def summarize(x, cost):
    x, cost = np.array(x), np.array(cost)
    if len(x) == 0:
        return dict(n=0, ev=0.0, win=0.0, roi=0.0, t=0.0)
    se = x.std(ddof=1) / math.sqrt(len(x)) if len(x) > 1 else float("nan")
    return dict(n=len(x), ev=float(x.mean()), win=float(np.mean(x > 0)),
                roi=float(x.sum() / cost.sum()), t=float(x.mean() / se) if se > 0 else 0.0)


def main():
    markets = json.loads((DATA / "esports_markets.json").read_text())
    candles = json.loads((DATA / "esports_candles.json").read_text())

    rows = []
    for m in markets:
        if not m["start_ts"] or m["ticker"] not in candles:
            continue
        q = series_quotes(candles[m["ticker"]])
        if not q:
            continue
        s = m["start_ts"]
        b, a = quote_at(q, s)
        b60, a60 = quote_at(q, s - 3600)
        pre_vol = sum(v for t, *_, v in q if t <= s)
        live = [(t, qb, qa) for t, qb, qa, _ in q if t > s and qb is not None and qa is not None]
        mids_live = [(qb + qa) / 2 for _, qb, qa in live if qb > 0 and qa < 1]
        rows.append(dict(
            ticker=m["ticker"], event=m["event_ticker"], game=m["game"], kind=m["kind"],
            y=1 if m["result"] == "yes" else 0, volume=fnum(m["volume_fp"]) or 0.0,
            start=s, bid=b, ask=a, bid60=b60, ask60=a60, pre_vol=pre_vol,
            live_min=min(mids_live) if mids_live else None,
            live_max=max(mids_live) if mids_live else None,
            live=live,
        ))

    def has_q(r):
        return r["bid"] is not None and r["ask"] is not None

    def mid(r):
        return (r["bid"] + r["ask"]) / 2

    match = [r for r in rows if r["kind"] == "match"]
    maps = [r for r in rows if r["kind"] == "map"]
    t0 = min(r["start"] for r in rows); t1 = max(r["start"] for r in rows)
    import time as _t
    d = lambda t: _t.strftime("%Y-%m-%d", _t.gmtime(t))

    L = []
    w = L.append
    w("# Kalshi esports markets: study\n")
    w(f"Data: **{len(rows):,} settled markets** "
      f"({len({r['event'] for r in match}):,} matches, plus map/game markets), "
      f"matches scheduled {d(t0)} to {d(t1)}. Prices are taken at the match's "
      f"scheduled start time unless stated otherwise.\n")

    # --- 1. Liquidity --------------------------------------------------
    w("## 1. Liquidity by game\n")
    w("| Game | Type | Markets | Quoted at start | Median spread at start (¢) | Median total volume | Median volume in final 3h pre-start | Share of volume in-play |")
    w("|---|---|---:|---:|---:|---:|---:|---:|")
    groups = defaultdict(list)
    for r in rows:
        groups[(r["game"], r["kind"])].append(r)
    for (g, kd), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        qd = [r for r in rs if has_q(r)]
        sp = [100 * (r["ask"] - r["bid"]) for r in qd]
        tv = sum(r["volume"] for r in rs)
        pv = sum(min(r["pre_vol"], r["volume"]) for r in rs)
        w(f"| {g} | {kd} | {len(rs):,} | {len(qd) / len(rs):.0%} | "
          f"{np.median(sp) if sp else float('nan'):.0f} | {np.median([r['volume'] for r in rs]):,.0f} | "
          f"{np.median([r['pre_vol'] for r in rs]):,.0f} | {1 - pv / tv if tv else 0:.0%} |")
    w("")

    # --- 2. Calibration ------------------------------------------------
    w("## 2. Are pre-match prices accurate? (match winner markets)\n")
    w("Every market whose book had both a bid and an ask at the scheduled start. "
      "Each match has two markets (one per team), so each match appears twice, mirrored.\n")
    edges = (0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0001)
    for label, sel in (("All quoted (any spread)", [r for r in match if has_q(r)]),
                       ("Tight books only (spread ≤ 6¢)", [r for r in match if has_q(r) and r["ask"] - r["bid"] <= 0.06])):
        w(f"**{label}: {len(sel):,} markets**\n")
        w("| Mid-price bucket | n | Avg mid | Actual win rate | Gap (pts) | Avg ask | ROI buying at ask, after fee |")
        w("|---|---:|---:|---:|---:|---:|---:|")
        for lo, hi in zip(edges, edges[1:]):
            rs = [r for r in sel if lo <= mid(r) < hi]
            if len(rs) < 10:
                continue
            am = np.mean([mid(r) for r in rs]); wr = np.mean([r["y"] for r in rs])
            pnl = [r["y"] - r["ask"] - taker_fee(r["ask"]) for r in rs]
            s = summarize(pnl, [r["ask"] for r in rs])
            w(f"| {lo:.1f}–{min(hi, 1):.1f} | {len(rs)} | {am:.3f} | {wr:.3f} | "
              f"{100 * (wr - am):+.1f} | {np.mean([r['ask'] for r in rs]):.3f} | {s['roi']:+.1%} |")
        ps = np.array([mid(r) for r in sel]); ys = np.array([r["y"] for r in sel])
        w(f"\nBrier score {np.mean((ps - ys) ** 2):.4f} (0.25 = coin flip).\n")

    w("**Per game (tight books, spread ≤ 6¢):** how often the pre-match favorite won.\n")
    w("| Game | Matches | Favorite's avg price | Favorite actually won | Gap (pts) | Brier |")
    w("|---|---:|---:|---:|---:|---:|")
    for g in sorted({r["game"] for r in match}):
        rs = [r for r in match if r["game"] == g and has_q(r) and r["ask"] - r["bid"] <= 0.06 and mid(r) > 0.5]
        if len(rs) < 15:
            continue
        ps = np.array([mid(r) for r in rs]); ys = np.array([r["y"] for r in rs])
        w(f"| {g} | {len(rs)} | {ps.mean():.3f} | {ys.mean():.3f} | {100 * (ys.mean() - ps.mean()):+.1f} | "
          f"{np.mean((ps - ys) ** 2):.4f} |")
    w("")

    # --- 3. Overround --------------------------------------------------
    w("## 3. Cost of trading both sides\n")
    ev = defaultdict(list)
    for r in match:
        ev[r["event"]].append(r)
    over, gaps = [], []
    for e, rs in ev.items():
        if len(rs) == 2 and all(has_q(r) for r in rs):
            over.append(rs[0]["ask"] + rs[1]["ask"] - 1)
            gaps.append(rs[0]["bid"] + rs[1]["bid"])
    if over:
        w(f"For {len(over):,} matches with both team markets quoted at start, buying both "
          f"teams at the ask cost a median **{100 * np.median(over):.0f}¢ over $1** "
          f"(middle 50%: {100 * np.percentile(over, 25):.0f}–{100 * np.percentile(over, 75):.0f}¢). "
          f"That gap, plus the fee, is what any edge has to beat.\n")

    # --- 4. Line movement ---------------------------------------------
    w("## 4. Does the price move in the last hour before the match?\n")
    mv = [r for r in match if has_q(r) and r["bid60"] and r["ask60"] and r["ask60"] < 1]
    if mv:
        m0 = np.array([(r["bid60"] + r["ask60"]) / 2 for r in mv]); m1 = np.array([mid(r) for r in mv])
        ys = np.array([r["y"] for r in mv])
        w(f"{len(mv):,} markets quoted both 1 hour before and at start. "
          f"Median absolute move {100 * np.median(abs(m1 - m0)):.1f}¢. "
          f"Brier 1h before: {np.mean((m0 - ys) ** 2):.4f}; at start: {np.mean((m1 - ys) ** 2):.4f}.\n")
        up = [(r, (r["bid60"] + r["ask60"]) / 2) for r in mv if mid(r) - (r["bid60"] + r["ask60"]) / 2 >= 0.05]
        if len(up) >= 20:
            x = [r["y"] - r["ask"] - taker_fee(r["ask"]) for r, _ in up]
            s = summarize(x, [r["ask"] for r, _ in up])
            w(f"Following the money: buying a team at the start after its price rose ≥5¢ in the "
              f"last hour → {s['n']} trades, win rate {s['win']:.1%}, ROI {s['roi']:+.1%}, t-stat {s['t']:+.2f}.\n")

    # --- 5. In-play ----------------------------------------------------
    w("## 5. In-play behaviour (match winner markets)\n")
    fav = [r for r in match if has_q(r) and mid(r) >= 0.70 and r["live_min"] is not None]
    if fav:
        dip = [r for r in fav if r["live_min"] <= 0.30]
        w(f"- Pre-match favorites (≥70¢) won {np.mean([r['y'] for r in fav]):.1%} of {len(fav):,} matches. "
          f"{len(dip) / len(fav):.1%} of them traded at or below 30¢ during the match; "
          f"of those, {np.mean([r['y'] for r in dip]) if dip else 0:.1%} still won.")
    winners = [r for r in match if r["y"] == 1 and r["live_min"] is not None]
    if winners:
        w(f"- {np.mean([r['live_min'] <= 0.25 for r in winners]):.1%} of eventual winners traded at or "
          f"below 25¢ at some point in-play (comebacks).")
    w("")
    w("Buying the in-play leader once its price first reaches a level (match markets):\n")
    w("| Trigger price | Trades | Win rate | Avg fill | ROI after fee | t-stat |")
    w("|---:|---:|---:|---:|---:|---:|")
    for lvl in (0.70, 0.80, 0.90, 0.95):
        x, cost = [], []
        for r in match:
            for t, qb, qa in r["live"]:
                if qa is not None and lvl <= qa < 1 and qb and qb > 0 and qa - qb <= 0.05:
                    x.append(r["y"] - qa - taker_fee(qa)); cost.append(qa)
                    break
        s = summarize(x, cost)
        if s["n"]:
            w(f"| {lvl:.2f} | {s['n']:,} | {s['win']:.1%} | {np.mean(cost):.3f} | {s['roi']:+.2%} | {s['t']:+.2f} |")
    w("")

    # --- 6. Map markets -----------------------------------------------
    w("## 6. Map/game markets, priced at match start\n")
    sel = [r for r in maps if has_q(r) and r["ask"] - r["bid"] <= 0.10]
    if sel:
        ps = np.array([mid(r) for r in sel]); ys = np.array([r["y"] for r in sel])
        w(f"{len(sel):,} map markets quoted with spread ≤ 10¢ at match start. "
          f"Brier {np.mean((ps - ys) ** 2):.4f}.\n")
        w("| Mid-price bucket | n | Avg mid | Actual win rate | Gap (pts) | ROI buying at ask |")
        w("|---|---:|---:|---:|---:|---:|")
        for lo, hi in zip(edges, edges[1:]):
            rs = [r for r in sel if lo <= mid(r) < hi]
            if len(rs) < 10:
                continue
            am = np.mean([mid(r) for r in rs]); wr = np.mean([r["y"] for r in rs])
            s = summarize([r["y"] - r["ask"] - taker_fee(r["ask"]) for r in rs], [r["ask"] for r in rs])
            w(f"| {lo:.1f}–{min(hi, 1):.1f} | {len(rs)} | {am:.3f} | {wr:.3f} | {100 * (wr - am):+.1f} | {s['roi']:+.1%} |")
        w("")

    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "esports.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
