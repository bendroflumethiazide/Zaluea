# BTC 15-minute flip-favorite: walk-forward backtest

Generated 2026-11-23 06:29 UTC by `research/run_backtest.py`.

## Verdict: **NO RELIABLE EDGE: only 112 out-of-sample trades (needs 200); mean P&L -0.0743/trade with t = -1.57 (needs > 0 and t >= 2.0)**

**Criteria, fixed before any result was seen** (`atlas/analytics/backtest.py`). Over the out-of-sample weeks combined:
- at least **200 trades**;
- a **mean profit per trade above $0, with a t-statistic of at least 2.0**;
- **at least 60% of test weeks** not losing money.

## Data

- **Markets:** 11,845 settled KXBTC15M markets with minute quotes, 2026-07-21 to 2026-11-23. Older markets come from Kalshi's archive endpoints, newer ones from the live API.
- **BTC reference:** the mean of Coinbase and Bitstamp 1-minute closes (180,117 minutes). 0 minutes were dropped because the two sources disagreed by more than 0.30%.
- **Strike:** Kalshi's own `floor_strike` for each market.

## Method

- **Decisions:** made at minutes 10, 11, 12 and 13 of each window, using only data available at that moment. The strategy receives time-cut data that raises an error on any later access.
- **Fills:** at the quoted ask, plus **$0.01 slippage per contract**, plus the **exact Kalshi fee** rounded up to the cent. Each trade is sized to at most $2 including fees, with at most one trade per market.
- **Walk-forward:** each week's parameters are chosen using only the weeks before it, by best training profit among 40 configurations, then applied unchanged to that week. Tuned settings: `vol_scale` ∈ {0.9, 1.0, 1.1, 1.25, 1.4}, `z_min` ∈ {1.5, 2.0, 2.5, 3.0}, `min_price` ∈ {0.80, 0.88}.
- **Known optimism:** historical order-book depth isn't available, so every fill at the ask is assumed to succeed. Real fills can be smaller or worse. A positive result here is a ceiling, not a promise.

## Out-of-sample result (all test weeks combined)

| Trades | Won | Win rate | Net P&L | Spent | Return on spend | Mean per trade | t-statistic | Weeks not losing |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 112 | 104 | 92.9% | -$8.326 | $216.326 | -3.85% | $-0.0743 | -1.57 | 80% |

## Week by week (each week tested with parameters chosen from earlier weeks only)

| Week | Chosen settings | Trades | Won | Net P&L | t |
|---|---|---:|---:|---:|---:|
| 2026-W33 | vol_scale=1.0, z_min=2.5, min_price=0.80 | 69 | 63 | -$6.416 | -1.40 |
| 2026-W34 | vol_scale=1.4, z_min=2.5, min_price=0.80 | 4 | 3 | -$1.664 | -0.88 |
| 2026-W35 | vol_scale=1.4, z_min=2.5, min_price=0.80 | 7 | 7 | $0.264 | 12.55 |
| 2026-W36 | vol_scale=1.25, z_min=3.0, min_price=0.80 | 6 | 6 | $0.316 | 3.64 |
| 2026-W37 | vol_scale=1.25, z_min=3.0, min_price=0.80 | 6 | 6 | $0.314 | 4.56 |
| 2026-W38 | vol_scale=1.25, z_min=3.0, min_price=0.80 | 9 | 8 | -$1.614 | -0.81 |
| 2026-W39 | vol_scale=1.25, z_min=3.0, min_price=0.80 | 4 | 4 | $0.154 | 7.24 |
| 2026-W40 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 1 | 1 | $0.032 | 0.00 |
| 2026-W41 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 1 | 1 | $0.058 | 0.00 |
| 2026-W42 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 0 | 0 | $0.00 | 0.00 |
| 2026-W43 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 2 | 2 | $0.13 | 13.00 |
| 2026-W44 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 0 | 0 | $0.00 | 0.00 |
| 2026-W45 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 3 | 3 | $0.10 | 50.00 |
| 2026-W46 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 0 | 0 | $0.00 | 0.00 |
| 2026-W47 | vol_scale=1.4, z_min=3.0, min_price=0.80 | 0 | 0 | $0.00 | 0.00 |

## Sensitivity: the same test with zero slippage (NOT the verdict)

This shows how much the $0.01 slippage assumption matters. Zero slippage: 706 trades, net -$22.18, mean $-0.0314 per trade, t = -1.69, 60% of weeks not losing. By the same criteria: NO RELIABLE EDGE: mean P&L -0.0314/trade with t = -1.69 (needs > 0 and t >= 2.0).

## Does the model predict better than the market's own price? (minute 12, all markets)

- **Model:** Brier score 0.0981, log loss 0.3199.
- **Market mid-price:** Brier score 0.0927, log loss 0.2954.
- Lower is better. If the market's own price scores better, there is little information left for this model to exploit.

| Model's probability | Markets | Mean predicted | Actually happened | 95% interval | Consistent? |
|---|---:|---:|---:|---|---|
| 0–10% | 3356 | 2.4% | 3.9% | 3.3%–4.6% | **no** |
| 10–20% | 930 | 14.7% | 12.9% | 10.9%–15.2% | yes |
| 20–30% | 728 | 24.8% | 23.9% | 20.9%–27.1% | yes |
| 30–40% | 599 | 35.2% | 38.1% | 34.3%–42.0% | yes |
| 40–50% | 614 | 45.0% | 44.8% | 40.9%–48.7% | yes |
| 50–60% | 566 | 55.2% | 58.3% | 54.2%–62.3% | yes |
| 60–70% | 585 | 65.0% | 69.7% | 65.9%–73.3% | **no** |
| 70–80% | 652 | 75.1% | 81.6% | 78.4%–84.4% | **no** |
| 80–90% | 835 | 85.1% | 89.1% | 86.8%–91.0% | **no** |
| 90–100% | 2900 | 97.5% | 97.9% | 97.3%–98.4% | yes |

