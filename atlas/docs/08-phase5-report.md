# Phase 5 report: shadow trading

**Status:** built and tested (the full suite of 254 tests passes), with a supervisor to keep it running. It now needs to run for weeks to
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
  - The simulator charges fees per fill, exactly as Kalshi's fee-rounding documentation describes. The trade fee is rounded to $0.000001. A rounding fee then brings the balance back to the cent; the more expensive of the two account types is assumed. A per-order accumulator rebates the over-payment, and a test reproduces Kalshi's worked example ($0.005).
  - Orders that buy at the ask use the 0.07 rate.
  - Resting fills use an **assumed 0.0175 rate**, a quarter of the taker rate. Kalshi lists the BTC 15-minute series as plain `quadratic`, which suggests makers pay **nothing** there, but this could not be confirmed from the documentation. The report therefore also shows the result with no maker fee.
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
- **The maker's guards fire often, as intended.** For example, the model's value fell below $0.03 per contract after fees, or BTC moved more than one standard deviation against a side within 60 s.
- **A flaw found by the first live run, and fixed.** The first version withdrew a bid whenever the market's best bid slipped by one cent. On live data, its two bids lived 2–6 seconds each and never filled, and both would have won (+$0.44 hypothetical). A rule like that can never gather evidence. Since the fix, a resting bid stays while the model still values it above **its own price** after fees and no flip guard fires. It is cancelled when the value is gone, when a guard fires, or at minute 14. Two tests cover this.
- **A second flaw found live, and fixed, in both strategies.** Right after a restart, the strategy bid $0.50 for YES eleven seconds after the price had dropped from $0.60, and was filled at once. That is the "falling knife" pattern the flip guard exists to block. But the guard compares against the price two minutes earlier, and with no history yet it was silently **skipped**. It now **fails closed**: no new order until two minutes of the market's own prices are available. The original buy-the-favorite strategy had the same gap and is fixed too.
- **A gap in the REAL safety bound, found through a simulated partial fill, and fixed.** Kalshi rounds fees per fill, and its rebate of over-paid rounding is capped. An order filled in many small pieces can therefore pay up to just under $0.01 of rounding **per fill**, while ATLAS reserved $0.01 **per order**. Both the primary and the independent shadow risk manager now reserve the provable bound: model fee rounded up to the cent plus **$0.01 per contract** (an order can never have more fills than contracts). A test checks the bound against every possible way of splitting orders of 1–6 contracts at five prices. Effect: a 2-contract $0.88 order now reserves $1.80 instead of $1.79. Strategies size orders on this worst case but value them on the expected fee, so backtests still use the exact fee.
- **A bookkeeping bug, found by reconciling the journal against the simulated ledger, and fixed.**
  - Simulated fill IDs restarted from 1 after every restart. The ledger correctly ignores an ID it has already seen, so later fills were **silently dropped**: 5 contracts filled, only 1 settled.
  - Fill IDs are now unique per order.
  - The simulated account now **stops with an error** if the ledger ever refuses a fill.
  - A refusal that repeats every 3 s (for example "daily loss limit reached") is now recorded once, not hundreds of times.
- **Every unfilled order now records why it ended.** Possible reasons: withdrawn by the strategy, post-only rejection (the ask fell to our price during the 1-second delay), too little shown at the ask, or the time limit.

**What the first 2½-hour live run suggested (discarded as evidence).** Over about 10 market windows the resting-bid strategy:
- had 7–9 of 13–18 orders filled;
- won only 1–2 of its 5–7 settled fills;
- would have won 50–62% of the orders that went unfilled.

That is the adverse-selection pattern this phase exists to detect: bids filled just before the price fell. The run is **not** counted, for three reasons. The sample is tiny, some fills came from before the guard fixes above, and the fill-ID bug corrupted part of its ledger. It is, though, an early warning that the decision rules below may well not be met.

**A clean 90-minute run on the fixed code (2026-11-23, about 6 market windows): the books match.**
- 0 failed ticks.
- The journal and both simulated ledgers agree for every account, and no reconciliation warning was printed.
- Every filled market settled with its full quantity: 14 of 14 contracts in `as_live` and 17 of 17 in `research`.

| Account | Orders | Filled | Settled | Won | P&L (assumed maker fee) | P&L (no maker fee) | Unfilled orders that would have won |
|---|---|---|---|---|---|---|---|
| maker / as_live | 10 | 5 | 5 | 4 | +$2.35 | +$2.44 | 4 of 5 |
| maker / research | 14 | 6 | 6 | 5 | +$3.62 | +$3.73 | 5 of 8 |
| favorite / both | 2 | 0 | 0 | – | $0.00 | – | 2 of 2 |

This run went the opposite way from the first: filled orders won more often than unfilled ones. With 6 settled
fills, neither run says anything about edge. Both runs together are 1% of the 200 settled fills that rule 1 needs.

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

## How to run it on your PC for weeks

Double-click `windows\Start-ATLAS.cmd`. It creates the data folder if needed, then starts a **supervisor**
(`python -m atlas.supervisor`) that runs the shadow test, the market scanner and the dashboard together. It
needs no keys and never starts the execution gateway. To have it start by itself at every login, run once:
`powershell -ExecutionPolicy Bypass -File windows\Install-Autostart.ps1` (removed by `Uninstall-Autostart.ps1`).

**The supervisor:**
- **Restarts** a process that exits: after 2 s, then 4, 8 … up to 5 minutes between attempts, and back to 2 s once a process has run for 10 minutes.
- **Restarts a frozen process:** the shadow test or scanner if it has not written results for 3 minutes.
- **Logs** each process to `<data>\logs\<name>.log`: 5 MB per file, 3 old files kept.
- **Notifies you in Windows:**
  - a crash or freeze, at most once per 30 minutes per process;
  - anything the watchdog queued;
  - a **daily summary** at 21:00 local time (`--summary-at` changes it), showing settled fills out of 200, simulated P&L and the verdict.
- **Shows its status** at the bottom of the Shadow trading panel. Only one supervisor can run per data folder.

The notification text is passed to Windows through environment variables, never pasted into a command, so a
market name or error message cannot run commands. On anything other than Windows, notifications are printed
to the log instead. **Not yet verified on Windows itself:** this cloud environment is Linux. The supervisor,
restarts, logging, single-instance lock and daily summary are tested here; the toast and the two `.ps1`
scripts are standard Windows PowerShell and need their first real run on your PC.

**Progress on the decision rules** is computed from the simulated ledgers on every update (`atlas/shadow/evidence.py`).
It is shown in the dashboard's *Evidence so far* section and at the end of `--report`. The verdict is:
- **NOT YET** until 200 fills have settled;
- **FAIL** if, after that, any rule is unmet;
- **PASS** only when all four rules are met. A pass only allows a proposal; nothing is switched on.

The pick-off diagnostic warns once filled and unfilled bids both have at least 20 results and unfilled bids
win at least 15 points more often.

Stopping is always safe: any simulated orders that were open are cancelled on the next start.
