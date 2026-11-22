# Kalshi BTC 15-minute markets: study

Data: **2848 settled KXBTC15M markets**, 2026-10-23 to 2026-11-22 (UTC). Model fitted on the first half, all strategy results are on the held-out second half (from 2026-11-07, 1424 markets).

## 1. How the market behaves

- **YES (up) rate:** 47.1% of markets settled YES.
- **Streaks:** after an UP window the next was UP 45.5% of the time; after a DOWN window, 48.5%. (No exploitable momentum if both are near the base rate.)
- **Volume per market:** median 3,599,477 contracts, mean 3,596,575.
- **Coinbase vs. Kalshi strike (BRTI):** median gap $3.99, middle 90% between $-14.34 and $24.35. Coinbase is a usable but imperfect stand-in for the settlement index.
- **Close calls:** 8.6% of markets settled within $10 of the strike, where the Coinbase proxy can disagree with BRTI.

Typical spread and activity by minute after open:

| Minute | Median spread (¢) | Quoted (%) | Median volume in that minute |
|---:|---:|---:|---:|
| 1 | 1.0 | 100% | 203,531 |
| 2 | 1.0 | 100% | 168,133 |
| 5 | 1.0 | 100% | 131,217 |
| 8 | 1.0 | 100% | 149,614 |
| 10 | 1.0 | 100% | 174,663 |
| 12 | 0.1 | 98% | 248,086 |
| 13 | 0.1 | 89% | 318,802 |
| 14 | 0.1 | 54% | 481,738 |

## 2. Is the market price accurate?

Fitted volatility scale on the training half: **k = 1.05** (1.0 means Coinbase's recent volatility is used as-is).

Scores on the test half (lower is better). *Market* is the mid of the best bid and ask; *Model* is the random-walk formula above.

| Minute | n | Market Brier | Model Brier | Market log loss | Model log loss |
|---:|---:|---:|---:|---:|---:|
| 1 | 1424 | 0.2299 | 0.2307 | 0.6513 | 0.6540 |
| 3 | 1424 | 0.2072 | 0.2075 | 0.6010 | 0.6033 |
| 5 | 1423 | 0.1879 | 0.1875 | 0.5530 | 0.5522 |
| 8 | 1424 | 0.1451 | 0.1478 | 0.4467 | 0.4546 |
| 10 | 1424 | 0.1188 | 0.1200 | 0.3717 | 0.3755 |
| 12 | 1404 | 0.0847 | 0.0877 | 0.2736 | 0.2841 |
| 13 | 1264 | 0.0706 | 0.0737 | 0.2306 | 0.2433 |
| 14 | 757 | 0.0583 | 0.0644 | 0.1942 | 0.2082 |

Calibration of the market mid at minute 5 and minute 12 (all data): when the market says X%, how often did YES happen?

**Minute 5**

| Price bucket | n | Avg price | Actual YES rate | Gap (pts) |
|---|---:|---:|---:|---:|
| 0.0–0.1 | 112 | 0.069 | 0.045 | -2.4 |
| 0.1–0.2 | 293 | 0.153 | 0.143 | -1.0 |
| 0.2–0.3 | 338 | 0.249 | 0.254 | +0.6 |
| 0.3–0.4 | 395 | 0.350 | 0.319 | -3.1 |
| 0.4–0.5 | 324 | 0.451 | 0.435 | -1.6 |
| 0.5–0.6 | 367 | 0.549 | 0.526 | -2.3 |
| 0.6–0.7 | 340 | 0.650 | 0.626 | -2.3 |
| 0.7–0.8 | 325 | 0.751 | 0.708 | -4.3 |
| 0.8–0.9 | 237 | 0.849 | 0.806 | -4.3 |
| 0.9–1.0 | 117 | 0.930 | 0.966 | +3.6 |

**Minute 12**

| Price bucket | n | Avg price | Actual YES rate | Gap (pts) |
|---|---:|---:|---:|---:|
| 0.0–0.1 | 900 | 0.029 | 0.028 | -0.1 |
| 0.1–0.2 | 221 | 0.143 | 0.199 | +5.6 |
| 0.2–0.3 | 134 | 0.245 | 0.216 | -2.9 |
| 0.3–0.4 | 127 | 0.348 | 0.323 | -2.5 |
| 0.4–0.5 | 120 | 0.447 | 0.442 | -0.6 |
| 0.5–0.6 | 123 | 0.549 | 0.480 | -7.0 |
| 0.6–0.7 | 121 | 0.646 | 0.653 | +0.7 |
| 0.7–0.8 | 129 | 0.756 | 0.736 | -1.9 |
| 0.8–0.9 | 184 | 0.853 | 0.853 | -0.0 |
| 0.9–1.0 | 746 | 0.974 | 0.983 | +0.8 |

## 3. Strategy backtests (test half, after taker fees)

Fills assume you get the quoted best price at the end of the minute, for small size. Real fills will be somewhat worse, so treat small positive numbers as zero.

**A. Model vs. market:** buy the side where the model's probability beats the price (after fee) by more than the threshold. One trade per market.

| Edge threshold | Train trades | Train avg P&L/contract | Test trades | Test avg P&L/contract | Test win rate | Test ROI | t-stat |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.02 | 1366 | -0.53¢ | 1339 | +0.07¢ | 42.5% | +0.2% | +0.06 |
| 0.04 | 1150 | -1.60¢ | 1054 | -0.03¢ | 37.6% | -0.1% | -0.03 |
| 0.06 | 873 | -0.66¢ | 743 | +0.01¢ | 33.6% | +0.0% | +0.00 |
| 0.08 | 621 | +1.14¢ | 504 | +2.23¢ | 32.7% | +7.6% | +1.19 |
| 0.10 | 433 | +0.45¢ | 311 | +3.09¢ | 31.8% | +11.2% | +1.32 |
| 0.15 | 184 | +2.96¢ | 125 | +6.91¢ | 33.6% | +27.0% | +1.78 |

Robustness check for threshold 0.10: average P&L per contract by the price paid. A real edge should have the same sign in both halves.

| Price paid | Train n | Train avg | Test n | Test avg |
|---|---:|---:|---:|---:|
| 0.0–0.2 | 136 | +1.79¢ | 124 | +0.25¢ |
| 0.2–0.4 | 146 | -3.62¢ | 124 | +3.18¢ |
| 0.4–0.6 | 73 | -3.52¢ | 40 | +2.15¢ |
| 0.6–1.0 | 78 | +9.47¢ | 23 | +19.56¢ |

**B. Buy the heavy favorite late:** buy whichever side is priced at or above the threshold at a given minute (all data).

| Minute | Min price | Trades | Win rate | Avg price | Avg P&L/contract | ROI | t-stat |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 0.80 | 1757 | 92.4% | 0.926 | -0.66¢ | -0.71% | -1.07 |
| 10 | 0.90 | 1259 | 95.9% | 0.956 | -0.06¢ | -0.07% | -0.11 |
| 10 | 0.95 | 790 | 98.5% | 0.977 | +0.67¢ | +0.68% | +1.55 |
| 12 | 0.80 | 2081 | 94.3% | 0.949 | -0.87¢ | -0.91% | -1.78 |
| 12 | 0.90 | 1703 | 97.1% | 0.971 | -0.15¢ | -0.15% | -0.38 |
| 12 | 0.95 | 1310 | 98.3% | 0.985 | -0.30¢ | -0.31% | -0.86 |
| 13 | 0.80 | 2022 | 95.7% | 0.959 | -0.42¢ | -0.44% | -0.98 |
| 13 | 0.90 | 1750 | 97.5% | 0.976 | -0.26¢ | -0.27% | -0.72 |
| 13 | 0.95 | 1444 | 98.8% | 0.988 | -0.01¢ | -0.01% | -0.05 |
| 14 | 0.80 | 1326 | 96.2% | 0.964 | -0.36¢ | -0.38% | -0.73 |
| 14 | 0.90 | 1162 | 98.1% | 0.980 | -0.01¢ | -0.01% | -0.02 |
| 14 | 0.95 | 989 | 99.3% | 0.989 | +0.27¢ | +0.28% | +1.03 |

**C. Fade the opening price:** at minute 1, buy the cheaper side when it is below 0.45 (all data).

Trades 1955, win rate 37.7%, avg P&L +0.03¢/contract, ROI +0.1%, t-stat +0.03.

## 5. Flip-aware favorites (the strategy `bot.py` trades)

Buy the favorite (0.80–0.97) in minutes 10–13, but only when the flip guards pass. Guards are added one at a time so you can see what each one does. Stop-loss sells if the held side's bid falls to 0.50.

| Rules | Half | Trades | Win rate | Avg price | Avg P&L/contract | P&L per $5 trade | Stop-outs | t-stat |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Favorite only, no guards | train | 1044 | 88.2% | 0.892 | -1.62¢ | $-0.076 | 0 | -1.64 |
| Favorite only, no guards | test | 1041 | 89.5% | 0.894 | -0.49¢ | $-0.023 | 0 | -0.53 |
| + BTC ≥ 2 sd from strike | train | 128 | 97.7% | 0.948 | +2.50¢ | $+0.127 | 0 | +1.89 |
| + BTC ≥ 2 sd from strike | test | 84 | 97.6% | 0.955 | +1.81¢ | $+0.091 | 0 | +1.08 |
| + model ≥ price + fee | train | 128 | 97.7% | 0.948 | +2.50¢ | $+0.127 | 0 | +1.89 |
| + model ≥ price + fee | test | 84 | 97.6% | 0.955 | +1.81¢ | $+0.091 | 0 | +1.08 |
| + momentum guard + 0.50 stop (bot) | train | 128 | 96.9% | 0.948 | +2.80¢ | $+0.142 | 4 | +2.74 |
| + momentum guard + 0.50 stop (bot) | test | 84 | 97.6% | 0.955 | +2.62¢ | $+0.131 | 2 | +2.33 |

Read this with care: these parameters were picked from a grid on the training half, so the test half is the honest number. Winning trades earn a few cents per contract while a loss costs the whole stake, so a short unlucky run can wipe out many wins.

## 4. Time of day (UTC)

| Hours (UTC) | Markets | YES rate | Median volume | Median spread at min 5 (¢) |
|---|---:|---:|---:|---:|
| 00–04 | 480 | 42.3% | 4,371,141 | 1.0 |
| 04–08 | 476 | 48.7% | 3,627,285 | 1.0 |
| 08–12 | 452 | 49.6% | 2,092,367 | 1.0 |
| 12–16 | 480 | 46.5% | 2,997,565 | 1.0 |
| 16–20 | 480 | 47.5% | 3,896,986 | 1.0 |
| 20–24 | 480 | 47.9% | 4,199,737 | 1.0 |

