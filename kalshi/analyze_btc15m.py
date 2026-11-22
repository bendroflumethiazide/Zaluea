"""Study Kalshi "BTC 15 min" markets (KXBTC15M) and write reports/btc15m.md.

Run collect_btc15m.py first.

Model used for comparison
-------------------------
Each market asks: will the 60-second average of BTC (CF Benchmarks BRTI)
before close be >= the 60-second average before open? We proxy BRTI with
Coinbase BTC-USD 1-minute candles. At minute m after open we know the spot
S_m, the strike proxy K (Coinbase price over the minute before open) and the
recent 1-minute volatility sigma. Treating the log price as a random walk,

    P(yes) = Phi( ln(S_m / K) / (k * sigma * sqrt(T_eff)) )

with T_eff = (14 - m) + 1/3 minutes (the final 60-second average removes
about 2/3 of the last minute's variance) and k a volatility scale fitted on
the first half of the data only. Everything is evaluated on the second half.
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
MINUTES = range(1, 15)


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load():
    markets = json.loads((DATA / "btc15m_markets.json").read_text())
    candles = json.loads((DATA / "btc15m_candles.json").read_text())
    cb = json.loads((DATA / "coinbase_btc_1m.json").read_text())
    # Coinbase row: [start, low, high, open, close, volume]
    cb_close = {int(r[0]): r[4] for r in cb}
    cb_mid = {int(r[0]): (r[3] + r[4]) / 2 for r in cb}
    return markets, candles, cb_close, cb_mid


def build_rows(markets, candles, cb_close, cb_mid):
    """One dict per market with per-minute quotes, spot and model inputs."""
    rows = []
    for m in markets:
        o = m["open_ts"]
        cs = sorted(candles.get(m["ticker"], []), key=lambda c: c["end_period_ts"])
        if not cs:
            continue
        k_cb = cb_mid.get(o - 60)
        if k_cb is None:
            continue
        bid, ask, vol = {}, {}, {}
        j, b, a = 0, None, None
        for mi in range(1, 16):
            t = o + 60 * mi
            v = 0.0
            while j < len(cs) and cs[j]["end_period_ts"] <= t:
                c = cs[j]
                b = fnum(c["yes_bid"].get("close_dollars"))
                a = fnum(c["yes_ask"].get("close_dollars"))
                v += fnum(c.get("volume_fp")) or 0.0
                j += 1
            bid[mi] = b if b and b > 0 else None
            ask[mi] = a if a and a < 1 else None
            vol[mi] = v
        spot, sig = {}, {}
        for mi in MINUTES:
            t = o + 60 * mi
            s = cb_close.get(t - 60)
            closes = [cb_close.get(t - 60 * (i + 1)) for i in range(61)]
            closes = [x for x in closes if x]
            if s is None or len(closes) < 40:
                continue
            r = np.diff(np.log(closes[::-1]))
            spot[mi], sig[mi] = s, float(np.std(r))
        rows.append(dict(ticker=m["ticker"], open_ts=o, y=1 if m["result"] == "yes" else 0,
                         strike=fnum(m["floor_strike"]), settle=fnum(m["expiration_value"]),
                         k_cb=k_cb, bid=bid, ask=ask, vol=vol, spot=spot, sig=sig,
                         volume=fnum(m["volume_fp"]) or 0.0))
    rows.sort(key=lambda r: r["open_ts"])
    return rows


def model_p(r, mi, kscale):
    s, sg = r["spot"].get(mi), r["sig"].get(mi)
    if s is None or not sg:
        return None
    t_eff = (14 - mi) + 1 / 3
    z = math.log(s / r["k_cb"]) / (kscale * sg * math.sqrt(t_eff))
    return min(max(phi(z), 1e-4), 1 - 1e-4)


def mid(r, mi):
    b, a = r["bid"].get(mi), r["ask"].get(mi)
    return (b + a) / 2 if b is not None and a is not None else None


def logloss(ps, ys):
    ps = np.clip(np.array(ps), 1e-4, 1 - 1e-4)
    ys = np.array(ys)
    return float(-np.mean(ys * np.log(ps) + (1 - ys) * np.log(1 - ps)))


def brier(ps, ys):
    return float(np.mean((np.array(ps) - np.array(ys)) ** 2))


def fit_kscale(train):
    best = None
    for k in np.arange(0.6, 2.01, 0.05):
        ps, ys = [], []
        for r in train:
            for mi in (2, 5, 8, 11, 13):
                p = model_p(r, mi, k)
                if p is not None:
                    ps.append(p); ys.append(r["y"])
        ll = logloss(ps, ys)
        if best is None or ll < best[1]:
            best = (round(float(k), 2), ll)
    return best[0]


def backtest(rows, kscale, thr, minutes=range(2, 14), max_price=0.97, min_price=0.03):
    """First signal per market: buy YES at ask or NO at (1 - bid), pay taker fee."""
    pnl = []
    for r in rows:
        for mi in minutes:
            p = model_p(r, mi, kscale)
            b, a = r["bid"].get(mi), r["ask"].get(mi)
            if p is None or b is None or a is None:
                continue
            ey = p - a - taker_fee(a)
            no_px = 1 - b
            en = (1 - p) - no_px - taker_fee(no_px)
            if ey >= en and ey > thr and min_price <= a <= max_price:
                pnl.append((r["y"] - a - taker_fee(a), a, mi))
                break
            if en > ey and en > thr and min_price <= no_px <= max_price:
                pnl.append(((1 - r["y"]) - no_px - taker_fee(no_px), no_px, mi))
                break
    return pnl


def summarize(pnl):
    if not pnl:
        return dict(n=0, ev=0.0, total=0.0, win=0.0, roi=0.0, t=0.0)
    x = np.array([p[0] for p in pnl])
    cost = np.array([p[1] for p in pnl])
    se = x.std(ddof=1) / math.sqrt(len(x)) if len(x) > 1 else float("nan")
    return dict(n=len(x), ev=float(x.mean()), total=float(x.sum()),
                win=float(np.mean(x > 0)), roi=float(x.sum() / cost.sum()),
                t=float(x.mean() / se) if se and se > 0 else 0.0)


def fav_strategy(rows, mi, lo):
    """Buy whichever side's ask is >= lo at minute mi."""
    pnl = []
    for r in rows:
        b, a = r["bid"].get(mi), r["ask"].get(mi)
        if b is None or a is None:
            continue
        if a >= lo and a < 1:
            pnl.append((r["y"] - a - taker_fee(a), a, mi))
        elif (1 - b) >= lo and b > 0:
            pnl.append(((1 - r["y"]) - (1 - b) - taker_fee(1 - b), 1 - b, mi))
    return pnl


def zdist(r, mi, kscale):
    s, sg = r["spot"].get(mi), r["sig"].get(mi)
    if s is None or not sg:
        return None
    return math.log(s / r["k_cb"]) / (kscale * sg * math.sqrt((14 - mi) + 1 / 3))


def flip_aware(rows, kscale, zmin, margin, stop, mom, minutes=(10, 11, 12, 13),
               lo=0.80, hi=0.97):
    """Favorite strategy with flip guards. Returns (pnl, price, minute, stopped)."""
    out = []
    for r in rows:
        done = False
        for mi in minutes:
            b, a = r["bid"].get(mi), r["ask"].get(mi)
            p, z = model_p(r, mi, kscale), zdist(r, mi, kscale)
            if None in (b, a, p, z):
                continue
            for side in ("yes", "no"):
                px = a if side == "yes" else 1 - b
                ps, zs = (p, z) if side == "yes" else (1 - p, -z)
                if not lo <= px <= hi or zs < zmin or ps < px + taker_fee(px) + margin:
                    continue
                pb, pa = r["bid"].get(mi - 2), r["ask"].get(mi - 2)
                if mom is not None and pb is not None and pa is not None:
                    d = ((a + b) - (pa + pb)) / 2
                    if (d if side == "yes" else -d) < -mom:
                        continue
                y = r["y"] if side == "yes" else 1 - r["y"]
                pnl, stopped = y - px - taker_fee(px), False
                if stop is not None:
                    for mj in range(mi + 1, 15):
                        bb, aa = r["bid"].get(mj), r["ask"].get(mj)
                        if bb is None or aa is None:
                            continue
                        sp = bb if side == "yes" else 1 - aa
                        if sp <= stop:
                            pnl = sp - px - taker_fee(px) - taker_fee(sp)
                            stopped = True
                            break
                out.append((pnl, px, mi, stopped))
                done = True
                break
            if done:
                break
    return out


def calib_table(pairs, edges=(0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0001)):
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [(p, y) for p, y in pairs if lo <= p < hi]
        if sel:
            out.append((lo, min(hi, 1), len(sel), np.mean([p for p, _ in sel]),
                        np.mean([y for _, y in sel])))
    return out


def main():
    markets, candles, cb_close, cb_mid = load()
    rows = build_rows(markets, candles, cb_close, cb_mid)
    n = len(rows)
    half = n // 2
    train, test = rows[:half], rows[half:]
    t0 = rows[0]["open_ts"]; t1 = rows[-1]["open_ts"]
    import time as _t
    d0 = _t.strftime("%Y-%m-%d", _t.gmtime(t0)); d1 = _t.strftime("%Y-%m-%d", _t.gmtime(t1))
    dt = _t.strftime("%Y-%m-%d", _t.gmtime(test[0]["open_ts"]))

    L = []
    w = L.append
    w("# Kalshi BTC 15-minute markets: study\n")
    w(f"Data: **{n} settled KXBTC15M markets**, {d0} to {d1} (UTC). "
      f"Model fitted on the first half, all strategy results are on the "
      f"held-out second half (from {dt}, {len(test)} markets).\n")

    # --- 1. Basics -----------------------------------------------------
    ys = np.array([r["y"] for r in rows])
    vols = np.array([r["volume"] for r in rows])
    w("## 1. How the market behaves\n")
    w(f"- **YES (up) rate:** {ys.mean():.1%} of markets settled YES.")
    prev = [(rows[i - 1]["y"], rows[i]["y"]) for i in range(1, n)
            if rows[i]["open_ts"] - rows[i - 1]["open_ts"] == 900]
    pu = np.mean([b for a, b in prev if a == 1]); pd_ = np.mean([b for a, b in prev if a == 0])
    w(f"- **Streaks:** after an UP window the next was UP {pu:.1%} of the time; "
      f"after a DOWN window, {pd_:.1%}. (No exploitable momentum if both are near the base rate.)")
    w(f"- **Volume per market:** median {np.median(vols):,.0f} contracts, "
      f"mean {vols.mean():,.0f}.")
    basis = [r["strike"] - r["k_cb"] for r in rows if r["strike"]]
    w(f"- **Coinbase vs. Kalshi strike (BRTI):** median gap ${np.median(basis):,.2f}, "
      f"middle 90% between ${np.percentile(basis, 5):,.2f} and ${np.percentile(basis, 95):,.2f}. "
      f"Coinbase is a usable but imperfect stand-in for the settlement index.")
    flips = [r for r in rows if r["strike"] and r["settle"] and abs(r["settle"] - r["strike"]) < 10]
    w(f"- **Close calls:** {len(flips) / n:.1%} of markets settled within $10 of the strike, "
      f"where the Coinbase proxy can disagree with BRTI.\n")

    w("Typical spread and activity by minute after open:\n")
    w("| Minute | Median spread (¢) | Quoted (%) | Median volume in that minute |")
    w("|---:|---:|---:|---:|")
    for mi in (1, 2, 5, 8, 10, 12, 13, 14):
        sp = [(r["ask"][mi] - r["bid"][mi]) * 100 for r in rows
              if r["ask"].get(mi) is not None and r["bid"].get(mi) is not None]
        q = len(sp) / n
        v = np.median([r["vol"].get(mi, 0) for r in rows])
        w(f"| {mi} | {np.median(sp):.1f} | {q:.0%} | {v:,.0f} |")
    w("")

    # --- 2. Calibration -----------------------------------------------
    k = fit_kscale(train)
    w("## 2. Is the market price accurate?\n")
    w(f"Fitted volatility scale on the training half: **k = {k}** "
      f"(1.0 means Coinbase's recent volatility is used as-is).\n")
    w("Scores on the test half (lower is better). *Market* is the mid of the "
      "best bid and ask; *Model* is the random-walk formula above.\n")
    w("| Minute | n | Market Brier | Model Brier | Market log loss | Model log loss |")
    w("|---:|---:|---:|---:|---:|---:|")
    for mi in (1, 3, 5, 8, 10, 12, 13, 14):
        pm, pmod, yy = [], [], []
        for r in test:
            a, b = mid(r, mi), model_p(r, mi, k)
            if a is not None and b is not None:
                pm.append(a); pmod.append(b); yy.append(r["y"])
        if yy:
            w(f"| {mi} | {len(yy)} | {brier(pm, yy):.4f} | {brier(pmod, yy):.4f} | "
              f"{logloss(pm, yy):.4f} | {logloss(pmod, yy):.4f} |")
    w("")
    w("Calibration of the market mid at minute 5 and minute 12 (all data): "
      "when the market says X%, how often did YES happen?\n")
    for mi in (5, 12):
        pairs = [(mid(r, mi), r["y"]) for r in rows if mid(r, mi) is not None]
        w(f"**Minute {mi}**\n")
        w("| Price bucket | n | Avg price | Actual YES rate | Gap (pts) |")
        w("|---|---:|---:|---:|---:|")
        for lo, hi, cnt, ap, ar in calib_table(pairs):
            w(f"| {lo:.1f}–{hi:.1f} | {cnt} | {ap:.3f} | {ar:.3f} | {100 * (ar - ap):+.1f} |")
        w("")

    # --- 3. Strategies -------------------------------------------------
    w("## 3. Strategy backtests (test half, after taker fees)\n")
    w("Fills assume you get the quoted best price at the end of the minute, "
      "for small size. Real fills will be somewhat worse, so treat small "
      "positive numbers as zero.\n")
    w("**A. Model vs. market:** buy the side where the model's probability "
      "beats the price (after fee) by more than the threshold. One trade per market.\n")
    w("| Edge threshold | Train trades | Train avg P&L/contract | Test trades | Test avg P&L/contract | Test win rate | Test ROI | t-stat |")
    w("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for thr in (0.02, 0.04, 0.06, 0.08, 0.10, 0.15):
        tr = summarize(backtest(train, k, thr)); te = summarize(backtest(test, k, thr))
        w(f"| {thr:.2f} | {tr['n']} | {tr['ev'] * 100:+.2f}¢ | {te['n']} | {te['ev'] * 100:+.2f}¢ | "
          f"{te['win']:.1%} | {te['roi']:+.1%} | {te['t']:+.2f} |")
    w("")
    w("Robustness check for threshold 0.10: average P&L per contract by the "
      "price paid. A real edge should have the same sign in both halves.\n")
    w("| Price paid | Train n | Train avg | Test n | Test avg |")
    w("|---|---:|---:|---:|---:|")
    ptr, pte = backtest(train, k, 0.10), backtest(test, k, 0.10)
    for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, 1)):
        a = [x for x, px, _ in ptr if lo <= px < hi]
        b = [x for x, px, _ in pte if lo <= px < hi]
        w(f"| {lo:.1f}–{hi:.1f} | {len(a)} | {100 * np.mean(a) if a else 0:+.2f}¢ | "
          f"{len(b)} | {100 * np.mean(b) if b else 0:+.2f}¢ |")
    w("")
    w("**B. Buy the heavy favorite late:** buy whichever side is priced at or above "
      "the threshold at a given minute (all data).\n")
    w("| Minute | Min price | Trades | Win rate | Avg price | Avg P&L/contract | ROI | t-stat |")
    w("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for mi in (10, 12, 13, 14):
        for lo in (0.80, 0.90, 0.95):
            p = fav_strategy(rows, mi, lo)
            s = summarize(p)
            if s["n"]:
                w(f"| {mi} | {lo:.2f} | {s['n']} | {s['win']:.1%} | {np.mean([x[1] for x in p]):.3f} | "
                  f"{s['ev'] * 100:+.2f}¢ | {s['roi']:+.2%} | {s['t']:+.2f} |")
    w("")
    w("**C. Fade the opening price:** at minute 1, buy the cheaper side when it is below 0.45 (all data).\n")
    p = []
    for r in rows:
        b, a = r["bid"].get(1), r["ask"].get(1)
        if b is None or a is None:
            continue
        if a < 0.45:
            p.append((r["y"] - a - taker_fee(a), a, 1))
        elif 1 - b < 0.45:
            p.append(((1 - r["y"]) - (1 - b) - taker_fee(1 - b), 1 - b, 1))
    s = summarize(p)
    w(f"Trades {s['n']}, win rate {s['win']:.1%}, avg P&L {s['ev'] * 100:+.2f}¢/contract, "
      f"ROI {s['roi']:+.1%}, t-stat {s['t']:+.2f}.\n")

    # --- 5. Flip-aware favorites (what bot.py trades) -----------------
    w("## 5. Flip-aware favorites (the strategy `bot.py` trades)\n")
    w("Buy the favorite (0.80–0.97) in minutes 10–13, but only when the flip "
      "guards pass. Guards are added one at a time so you can see what each "
      "one does. Stop-loss sells if the held side's bid falls to 0.50.\n")
    w("| Rules | Half | Trades | Win rate | Avg price | Avg P&L/contract | P&L per $5 trade | Stop-outs | t-stat |")
    w("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    variants = (
        ("Favorite only, no guards", dict(zmin=-9, margin=-1, stop=None, mom=None)),
        ("+ BTC ≥ 2 sd from strike", dict(zmin=2.0, margin=-1, stop=None, mom=None)),
        ("+ model ≥ price + fee", dict(zmin=2.0, margin=0.0, stop=None, mom=None)),
        ("+ momentum guard + 0.50 stop (bot)", dict(zmin=2.0, margin=0.0, stop=0.5, mom=0.05)),
    )
    for name, cfg in variants:
        for hname, rs in (("train", train), ("test", test)):
            p = flip_aware(rs, k, **cfg)
            s = summarize(p)
            if not s["n"]:
                continue
            avg_px = np.mean([x[1] for x in p])
            per5 = np.mean([x[0] * (5 // (x[1] + taker_fee(x[1]))) for x in p])
            w(f"| {name} | {hname} | {s['n']} | {s['win']:.1%} | {avg_px:.3f} | {s['ev'] * 100:+.2f}¢ | "
              f"${per5:+.3f} | {sum(x[3] for x in p)} | {s['t']:+.2f} |")
    w("")
    w("Read this with care: these parameters were picked from a grid on the "
      "training half, so the test half is the honest number. Winning trades "
      "earn a few cents per contract while a loss costs the whole stake, so a "
      "short unlucky run can wipe out many wins.\n")

    # --- 4. Time of day -----------------------------------------------
    w("## 4. Time of day (UTC)\n")
    w("| Hours (UTC) | Markets | YES rate | Median volume | Median spread at min 5 (¢) |")
    w("|---|---:|---:|---:|---:|")
    by = defaultdict(list)
    for r in rows:
        by[(r["open_ts"] // 3600 % 24) // 4].append(r)
    for g in sorted(by):
        rs = by[g]
        sp = [(r["ask"][5] - r["bid"][5]) * 100 for r in rs
              if r["ask"].get(5) is not None and r["bid"].get(5) is not None]
        w(f"| {g * 4:02d}–{g * 4 + 4:02d} | {len(rs)} | {np.mean([r['y'] for r in rs]):.1%} | "
          f"{np.median([r['volume'] for r in rs]):,.0f} | {np.median(sp) if sp else float('nan'):.1f} |")
    w("")

    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "btc15m.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
