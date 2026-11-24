# Stocks and ETFs: what we will test, fixed before any results exist

Written 2026-11-24, before any stock or ETF price data was downloaded. Nothing below may change after
the results are seen. If a rule turns out to be badly chosen, the fix is a **new** plan, tested on data
collected **after** that plan is written.

## Honest starting point

For a small account, decades of evidence say the hardest benchmark to beat is simply **buying and holding a
broad index ETF**. Most short-term trading rules that looked good in old studies have weakened since they
were published. So the default outcome of this phase is expected to be:

> "No trading rule earns its place. The stocks module buys a broad ETF on a schedule, within your limits."

That would be a good, safe result, not a failure. A rule replaces it only if it clearly passes the tests below.

## Data

- **Source:** Alpaca daily bars (`/v2/stocks/{symbol}/bars`, `timeframe=1Day`).
  - `adjustment=all`: prices are adjusted for splits and dividends, so returns include dividends.
  - `feed=sip`: all US exchanges.
  - Downloaded on your PC with your paper-account keys; this cloud environment cannot reach market data.
- **Period:** 2016-01-04 to the most recent full trading day.
- **ETFs:**
  - **SPY:** S&P 500.
  - **QQQ:** Nasdaq-100.
  - **IWM:** Russell 2000.

## How trades are simulated (deliberately pessimistic)

- **No look-ahead:** a decision uses only data up to that day's close. It is executed at the **next day's open**, and a test checks that changing future prices never changes a past decision.
- **Costs:** 0.05% on every buy and every sell, about ten times SPY's typical half-spread, to cover slippage and fractional-share fills.
- **Cash earns nothing.** That understates the trend rule, which sits in cash for long stretches.
- **Positions:** long only. Each rule is either fully in one ETF or fully in cash; no leverage.

## The rules (parameters taken from the published literature, not tuned)

| | Rule |
|---|---|
| **S0: buy and hold** (the benchmark) | Buy at the first open after the 200-day warm-up; hold to the end. |
| **S1: trend filter** | Hold the ETF while its close is above its 200-day simple moving average. Otherwise hold cash. |
| **S2: pullback (mean reversion)** | When not holding: buy if the close is above the 200-day average **and** the 2-day RSI is below 10. Sell when the close is above the 5-day average, or after 5 trading days. |

## Out-of-sample period

Nothing is tuned, but results are still reported in two halves so a rule that only worked in one era is visible:
- **Early:** 2016–2021.
- **Recent:** 2022 onward.

The pass/fail rules use the **recent** half.

## Pass/fail rules

**S1, trend filter.** It passes only if, in the recent period, for SPY **and** at least one of QQQ or IWM:
1. its worst peak-to-trough fall (maximum drawdown) is no more than **70%** of buy-and-hold's; **and**
2. its annual return is no more than **2 percentage points** below buy-and-hold's.

Capital preservation comes first: a rule that cuts the worst losses by a third for a small cost in return is acceptable.

**S2, pullback.** It passes only if, in the recent period, pooled over the three ETFs:
1. at least **100** completed trades;
2. average net return per trade above zero with a t-statistic of at least **2**;
3. at least **60%** of calendar years with a positive total;
4. its return per day actually invested beats buy-and-hold's average daily return. If it doesn't, holding the index is simply better.

**Outcome:**
- A rule that **passes** goes to a live shadow test (simulated money, real quotes) with its own pre-set rules, exactly like the Kalshi strategy. Even then, live trading stays off until you enable it.
- If **nothing passes**, the stocks module becomes scheduled buying of SPY within your limits, and no "trading" rule.
