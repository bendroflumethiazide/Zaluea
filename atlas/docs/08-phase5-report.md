# Phase 5 report: shadow trading

**Status:** built and tested (25 new tests, all passing; the full suite also passes). It now needs to run for weeks to
collect live evidence. No real order was placed, and none can be: shadow trading has no key, no gateway and no
order code.

## Why this phase looks the way it does

The Phase 4 backtest found **no reliable edge** in the "buy the favorite late in the window" strategy. Over 142
out-of-sample trades it lost $10.61 (t = −1.74). The model was well calibrated, but the market's own price
was better calibrated still, and paying the spread plus the taker fee turned a small information gap into
a loss.

The remaining idea worth testing is to **stop paying the spread and earn it instead**. That means resting a
bid (a "maker" order) and letting someone else's market order fill it. The danger is *adverse selection*:
resting bids tend to fill exactly when the price is about to drop. A historical backtest cannot measure this,
because candles say nothing about whose order a trade hit. It can only be measured against the live trade
feed, so Phase 5 was built for that.

## What was built

| Part | File | What it does |
|---|---|---|
| Simulator | `atlas/shadow/sim.py` | Simulated exchange. It is not connected to anything. |
| Simulated account | `atlas/shadow/account.py` | Uses the **real** ATLAS ledger class on a **separate file** under `sim/`, with the real limits. |
| Journal | `atlas/shadow/journal.py` | Records every order, fill, cancellation, rejection and missed opportunity. Missed and rejected ones are later resolved with their hypothetical result. |
| Maker strategy | `atlas/strategies/crypto_maker.py` | Rests a bid only when the model value beats the price after an assumed maker fee. |
| Runner | `atlas/shadow/runner.py` | `python -m atlas.shadow --data <folder>`: polls live **public** data every 3 s. |
| Report | `atlas/shadow/report.py` | Writes `sim/shadow.json` for the dashboard. `--report` prints a table. |
| Kalshi client | `exchange/kalshi.py` | New public reads: `book()` (both sides with sizes) and `recent_trades()` (public trade feed, block trades excluded). |
| Dashboard | `dashboard/` | The Shadow trading panel now shows the real simulated results, clearly labelled, with the real account kept apart. |

### How fills are simulated (deliberately pessimistic)

- **Latency.** An order goes live 1 s after the decision, against the order book *as it is then*. A cancel also takes 1 s to land, and a trade in that gap can still fill the order.
- **Orders that buy at the ask (taker):**
  - Fill only up to the size displayed at the best ask. The rest is cancelled, which is a partial fill.
  - The simulator never walks deeper into the book.
  - If the ask moved above the limit during the delay, nothing fills.
- **Resting orders (maker):**
  - Fill **only** when a real public trade, printed after the order went live, *sold into* the bid.
  - A trade **at** the bid price first uses up the whole size that was already displayed there when the order joined. Nobody ahead is assumed to cancel.
  - A trade **through** the price fills the order directly.
  - One public trade is never counted twice. A trade time-stamped after "now" is never used.
  - An order that would cross the ask is rejected (post-only).
- **Fees:**
  - Orders that buy at the ask pay the worst-case Kalshi fee: the formula rounded up, plus the $0.01 rounding allowance.
  - Resting fills pay an assumed **0.0175 × C × P × (1 − P)** fee plus the allowance. That is a quarter of the taker rate.
  - Kalshi lists the BTC 15-minute series as plain `quadratic`, which suggests makers pay **nothing** there, but this could not be confirmed from the documentation. The report therefore also shows the result with no maker fee.
- **Settlement.** The payout is $1 per winning contract with no fee, from Kalshi's published result.
- **Restarts.** Simulated orders live in memory. After a restart, any order the ledger still shows as open is cancelled, and a note is logged.

### Two simulated accounts per strategy

| Account | Limits | Purpose |
|---|---|---|
| `as_live` | Exactly the real limits: $2 per purchase, $3 per market, $20 per day, 3 positions, stop after $3 of losses or 3 losses in a row | Shows what live trading would really have done |
| `research` | Same per-purchase and per-market caps, but **no daily stops** | Keeps collecting evidence after a bad day |

Both the new maker strategy and the old buy-the-favorite strategy run, so the two can be compared on the same
live data.

## Part 25 shadow-trading tests

| Requirement | Test(s) in `tests/test_shadow_trading.py` |
|---|---|
| Cannot submit live orders | `test_shadow_trading_cannot_reach_live_execution`: reads the source and finds no `place_order` call, no gateway, control or Alpaca import, and every Kalshi client is built with `signer=None`. `test_runner_refuses_a_kalshi_client_with_a_key` |
| Separate balance | `test_runner_end_to_end_with_separate_balance`: the real `ledger.db` is byte-for-byte unchanged after a full simulated cycle. `test_simulated_account_refuses_a_folder_outside_sim` |
| Transaction costs | `test_taker_fill_is_limited_to_displayed_size_and_pays_the_worst_case_fee`, `test_fill_and_settlement_pnl_include_every_fee`, `test_maker_fee_assumption_is_pessimistic_and_can_be_switched_off` |
| Partial fills, liquidity, queue | `test_taker_fill_is_limited_…`, `test_resting_order_fills_only_from_later_public_trades`, `test_queue_ahead_at_our_price_fills_first`, `test_one_public_trade_is_not_counted_twice` |
| Latency | `test_taker_uses_the_book_after_the_latency_not_at_decision_time`, `test_cancel_takes_the_latency_to_land` |
| No future data | `test_a_trade_printed_after_now_is_future_data_and_waits`, plus the Phase 4 look-ahead guard on every strategy input |
| Simulated vs real P&L | End-to-end test: `shadow.json` is labelled SIMULATED and carries the real account's P&L separately. Dashboard test: `test_dashboard_shows_simulated_results_labelled_…` |
| Same limits as live | `test_as_live_account_enforces_the_real_limits`, `test_as_live_stops_after_three_losses_but_research_keeps_going` |
| Missed and rejected opportunities | `test_runner_end_to_end_…` (missed order resolved after settlement), `test_rejections_are_recorded_with_their_reason`, `test_restart_cancels_simulated_orders_left_open` |

## First live observations

- **The BTC 15-minute markets are extremely busy.** One market printed **1,654 public trades in 30 seconds**. A typical best bid had **thousands of contracts** queued at it (8,339 in one sample).
- **Joining the best bid therefore almost never fills.** The strategy improves the bid by one cent whenever the spread is at least $0.02. That puts the order at the front of a new price level instead of behind thousands of contracts.
- **The maker's guards fire often, as intended.** For example, the model's value fell below $0.03 per contract after fees, or BTC moved more than one standard deviation against a side within 60 s. When a guard fires, the order is withdrawn.

These are observations, not results. Results need weeks.

## Decision rules, fixed now, before any results exist

The maker strategy may be proposed for live trading only if, in the **research** account:

1. at least **200** filled orders have settled;
2. total P&L is above zero **with the pessimistic maker fee**, and the per-trade t-statistic is at least **2**;
3. at least **60%** of calendar weeks are not losing;
4. the `as_live` account would not have breached any limit.

**Diagnostic (not a pass/fail rule):** if unfilled orders win much more often than filled ones, fills are being
picked off. A strategy that passes rules 1–3 despite that is still treated with suspicion.

Even if every rule passes, live trading stays off until you enable it yourself. It would then start at the $2
per-purchase limit, with approval required for every trade.

## Honest limitations

- **Queue position.**
  - The queue is estimated from book snapshots taken every ~3 s. Size added between a snapshot and our order going live is not counted ahead of us, so the estimate is slightly optimistic.
  - Cancellations ahead of us are never counted, so it is also pessimistic. The two effects do not cancel exactly.
- **Our order is invisible to everyone else.** A real resting bid can change other traders' behaviour, and a $2 bid is too small to matter much, but the simulation cannot model it.
- **Gaps in the trade feed.** If more than 20,000 trades arrive between polls, the feed is incomplete, and the dashboard says so. Missing trades can only make resting fills *rarer*, which is pessimistic.
- **The maker fee is an assumption.** Both results are reported: with the assumed fee and with no fee.
- **Only BTC 15-minute markets are simulated.** Esports markets stay blocked until a live match-data feed exists (Phase 3 rule). Stocks and ETFs remain blocked until Phase 6.

## How to run it on your PC

```powershell
cd path\to\Zaluea\atlas
python -m atlas.shadow --data "$env:USERPROFILE\.atlas\data"
```

Leave it running alongside the scanner and the dashboard. It needs no keys. Results appear in the Shadow
trading panel, and `python -m atlas.shadow --data "$env:USERPROFILE\.atlas\data" --report` prints the table.
Stopping it is safe. Any simulated orders that were open are simply cancelled on the next start.
