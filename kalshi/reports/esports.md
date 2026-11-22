# Kalshi esports markets: study

Data: **23,234 settled markets** (4,620 matches, plus map/game markets), matches scheduled 2026-09-14 to 2026-11-23. Prices are taken at the match's scheduled start time unless stated otherwise.

## 1. Liquidity by game

| Game | Type | Markets | Quoted at start | Median spread at start (¢) | Median total volume | Median volume in final 3h pre-start | Share of volume in-play |
|---|---|---:|---:|---:|---:|---:|---:|
| CS2 | map | 8,835 | 92% | 36 | 2,325 | 0 | 94% |
| CS2 | match | 5,893 | 93% | 4 | 8,964 | 189 | 91% |
| Dota 2 | map | 2,832 | 97% | 11 | 2,929 | 0 | 95% |
| Dota 2 | match | 1,460 | 96% | 4 | 8,165 | 108 | 94% |
| Valorant | map | 1,172 | 86% | 52 | 989 | 0 | 82% |
| LoL | map | 1,162 | 81% | 3 | 14,877 | 12 | 87% |
| LoL | match | 696 | 90% | 1 | 36,871 | 6,032 | 78% |
| Valorant | match | 588 | 92% | 5 | 6,719 | 376 | 79% |
| Rainbow Six | match | 456 | 96% | 6 | 4,968 | 484 | 85% |
| Rocket League | match | 78 | 100% | 24 | 5,710 | 249 | 85% |
| Call of Duty | match | 62 | 55% | 1 | 22,062 | 451 | 85% |

## 2. Are pre-match prices accurate? (match winner markets)

Every market whose book had both a bid and an ask at the scheduled start. Each match has two markets (one per team), so each match appears twice, mirrored.

**All quoted (any spread): 8,611 markets**

| Mid-price bucket | n | Avg mid | Actual win rate | Gap (pts) | Avg ask | ROI buying at ask, after fee |
|---|---:|---:|---:|---:|---:|---:|
| 0.0–0.1 | 209 | 0.066 | 0.048 | -1.8 | 0.078 | -45.1% |
| 0.1–0.2 | 628 | 0.152 | 0.139 | -1.3 | 0.174 | -26.1% |
| 0.2–0.3 | 869 | 0.251 | 0.226 | -2.5 | 0.279 | -24.2% |
| 0.3–0.4 | 1185 | 0.351 | 0.343 | -0.8 | 0.390 | -16.4% |
| 0.4–0.5 | 1364 | 0.448 | 0.443 | -0.5 | 0.508 | -16.1% |
| 0.5–0.6 | 1468 | 0.546 | 0.560 | +1.4 | 0.624 | -12.7% |
| 0.6–0.7 | 1173 | 0.645 | 0.643 | -0.3 | 0.685 | -8.3% |
| 0.7–0.8 | 870 | 0.745 | 0.780 | +3.5 | 0.775 | -0.8% |
| 0.8–0.9 | 638 | 0.845 | 0.862 | +1.7 | 0.869 | -1.7% |
| 0.9–1.0 | 207 | 0.933 | 0.947 | +1.4 | 0.945 | -0.1% |

Brier score 0.1971 (0.25 = coin flip).

**Tight books only (spread ≤ 6¢): 6,139 markets**

| Mid-price bucket | n | Avg mid | Actual win rate | Gap (pts) | Avg ask | ROI buying at ask, after fee |
|---|---:|---:|---:|---:|---:|---:|
| 0.0–0.1 | 198 | 0.066 | 0.051 | -1.5 | 0.076 | -39.6% |
| 0.1–0.2 | 503 | 0.150 | 0.125 | -2.5 | 0.164 | -29.6% |
| 0.2–0.3 | 656 | 0.251 | 0.226 | -2.5 | 0.265 | -19.9% |
| 0.3–0.4 | 850 | 0.351 | 0.328 | -2.3 | 0.365 | -14.5% |
| 0.4–0.5 | 926 | 0.447 | 0.446 | -0.1 | 0.461 | -7.0% |
| 0.5–0.6 | 897 | 0.550 | 0.557 | +0.7 | 0.564 | -4.2% |
| 0.6–0.7 | 801 | 0.647 | 0.664 | +1.7 | 0.661 | -1.9% |
| 0.7–0.8 | 624 | 0.746 | 0.784 | +3.8 | 0.760 | +1.5% |
| 0.8–0.9 | 485 | 0.846 | 0.870 | +2.4 | 0.860 | +0.2% |
| 0.9–1.0 | 199 | 0.934 | 0.950 | +1.6 | 0.944 | +0.2% |

Brier score 0.1891 (0.25 = coin flip).

**Per game (tight books, spread ≤ 6¢):** how often the pre-match favorite won.

| Game | Matches | Favorite's avg price | Favorite actually won | Gap (pts) | Brier |
|---|---:|---:|---:|---:|---:|
| CS2 | 1926 | 0.676 | 0.701 | +2.5 | 0.1919 |
| Dota 2 | 500 | 0.711 | 0.736 | +2.5 | 0.1820 |
| LoL | 295 | 0.750 | 0.732 | -1.8 | 0.1766 |
| Rainbow Six | 99 | 0.691 | 0.697 | +0.6 | 0.2055 |
| Valorant | 141 | 0.705 | 0.738 | +3.2 | 0.1689 |

## 3. Cost of trading both sides

For 4,298 matches with both team markets quoted at start, buying both teams at the ask cost a median **3¢ over $1** (middle 50%: 2–7¢). That gap, plus the fee, is what any edge has to beat.

## 4. Does the price move in the last hour before the match?

8,298 markets quoted both 1 hour before and at start. Median absolute move 1.5¢. Brier 1h before: 0.1995; at start: 0.1965.

Following the money: buying a team at the start after its price rose ≥5¢ in the last hour → 812 trades, win rate 59.6%, ROI -12.8%, t-stat -5.31.

## 5. In-play behaviour (match winner markets)

- Pre-match favorites (≥70¢) won 83.1% of 1,706 matches. 21.4% of them traded at or below 30¢ during the match; of those, 25.8% still won.
- 24.7% of eventual winners traded at or below 25¢ at some point in-play (comebacks).

Buying the in-play leader once its price first reaches a level (match markets):

| Trigger price | Trades | Win rate | Avg fill | ROI after fee | t-stat |
|---:|---:|---:|---:|---:|---:|
| 0.70 | 5,254 | 76.5% | 0.774 | -2.67% | -3.60 |
| 0.80 | 4,614 | 83.7% | 0.849 | -2.42% | -3.80 |
| 0.90 | 3,835 | 92.2% | 0.924 | -0.72% | -1.54 |
| 0.95 | 3,295 | 95.7% | 0.960 | -0.62% | -1.68 |

## 6. Map/game markets, priced at match start

4,682 map markets quoted with spread ≤ 10¢ at match start. Brier 0.2075.

| Mid-price bucket | n | Avg mid | Actual win rate | Gap (pts) | ROI buying at ask |
|---|---:|---:|---:|---:|---:|
| 0.0–0.1 | 80 | 0.065 | 0.037 | -2.7 | -62.5% |
| 0.1–0.2 | 201 | 0.153 | 0.159 | +0.6 | -16.4% |
| 0.2–0.3 | 399 | 0.253 | 0.216 | -3.8 | -27.6% |
| 0.3–0.4 | 679 | 0.350 | 0.306 | -4.4 | -22.4% |
| 0.4–0.5 | 1003 | 0.446 | 0.464 | +1.8 | -4.9% |
| 0.5–0.6 | 995 | 0.548 | 0.540 | -0.8 | -8.8% |
| 0.6–0.7 | 678 | 0.645 | 0.696 | +5.1 | +1.8% |
| 0.7–0.8 | 384 | 0.744 | 0.779 | +3.5 | -0.4% |
| 0.8–0.9 | 187 | 0.842 | 0.861 | +1.9 | -1.5% |
| 0.9–1.0 | 76 | 0.929 | 0.974 | +4.5 | +2.0% |

