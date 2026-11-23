# ATLAS Phase 4: Analytics report

Date: 2026-11-23. Nothing in this phase can trade: strategies only produce candidate signals, and the scanner has no gateway or keys.

## 1. What was built

| Component | File | What it does |
|---|---|---|
| Probability and EV engine | `atlas/analytics/probability.py` | Expected value and break-even probability in exact Money, rounded down so an edge is never overstated. Brier score, log loss, and calibration buckets with 95% Wilson intervals. A prediction log records the estimate, the market's implied probability, sources, model version, timestamp and outcome. Models are labelled **UNVALIDATED** until 300 resolved predictions. |
| Strategy framework | `atlas/strategies/base.py` | Strategies return a Signal or a NO TRADE with reasons; they have no exchange access. **Point-in-time data:** asking for anything after the decision time raises `LookAheadError`. Any config change creates a new, traceable model version. |
| Crypto flip-favorite | `atlas/strategies/crypto_flip.py` | Rebuilt on Kalshi's own strike (`floor_strike`) and the multi-exchange BTC reference. Flip guards (distance in standard deviations, momentum, sudden jumps), exact fee, size limited to $2, and an EV floor. Every failed check is reported. |
| Walk-forward backtest | `atlas/analytics/backtest.py`, `research/run_backtest.py` | Success criteria fixed before any result. Each week is tested with parameters chosen only from earlier weeks. Exact fees and $0.01 slippage per contract. Report in `docs/06-btc15m-backtest.md`. |
| History collector | `research/collect_btc15m.py`, `research/collect_reference.py` | About 4 months of settled BTC 15-minute markets (live and archived endpoints) and 1-minute BTC closes from Coinbase and Bitstamp. Research cache only, not committed. |
| Correlation engine | `atlas/analytics/correlation.py` | Risk groups: all crypto markets form one group; events by match or event; equities by measured rolling correlation and ETF holdings. Unknown equities join a broad `us_equity` group, which is conservative. Sector exposure and ETF overlap reports. |
| Correlation limit | `atlas/limits.py`, `atlas/gateway.py` | New limit `max_correlated_exposure` ($4), enforced at gateway step 6. Older data folders receive it at its default, audited. |
| Market scanner | `atlas/scanner.py` | Runs the strategy on live public data and explains every verdict. Logs and later scores one prediction per market, so real calibration accumulates. Writes `scanner.json` for Hysilens. |
| Dashboard | `dashboard/real_state.py`, `static/` | The scanner panel shows live rows and the model's current scorecard when the scanner is running. |
| Data folder setup | `atlas/init_data.py` | `python -m atlas.init_data --data <folder>` |

## 2. Part 25 correlation tests (now done)

| Spec test | Test |
|---|---|
| Detects highly correlated stock holdings | `test_detects_highly_correlated_stock_holdings` |
| Detects overlapping ETF exposure | `test_detects_overlapping_etf_exposure` |
| Concentration across modules | `test_concentration_across_modules_crypto_is_one_group` |
| Prevents exceeding configured limits | `test_prevents_exceeding_the_configured_limit`, `test_gateway_rejects_a_trade_that_overloads_a_correlated_group` |
| Missing data handled conservatively | `test_missing_data_is_handled_conservatively` |

## 3. Backtest result

See `docs/06-btc15m-backtest.md` (filled in when the full data set has downloaded).

## 4. Honest limits

- **Historical fills are assumed.** Kalshi's archive has no order-book depth, so the backtest assumes the ask could be filled. That is optimistic.
- **The historical BTC reference uses two sources** (Coinbase, Bitstamp): Kraken's public history only covers about the last 12 hours. Live scanning uses all three.
- **ETF holdings data isn't bundled.** Holdings must come from a dated, sourced file. Without one, equities and ETFs are grouped together conservatively.
- **Sector labels cover about 20 large caps.** Anything else is "Unknown" and grouped conservatively.
- **The model's live calibration starts at zero.** The scanner has to run for a while (300 settled markets is about 3 days of BTC 15-minute markets) before its probabilities count as validated.
