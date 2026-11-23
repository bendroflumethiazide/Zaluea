# ATLAS implementation plan

Status: **approved 2026-11-23.** Nothing in ATLAS can place an order yet.

Guiding rule: **capital preservation first, intelligent trading second, profitability third.** When anything is uncertain, ATLAS does not trade.

---

## 1. Architecture

### 1.1 Separate processes, each with the least privilege it needs

```
                 ┌──────────────────────────────┐
                 │  atlas-engine                │  market data, scanners, strategies,
                 │  NO live credentials         │  probability/EV, correlation,
                 │                              │  shadow trading + paper simulator
                 └──────────────┬───────────────┘
                                │ trade PROPOSALS only (local socket)
                                ▼
┌──────────────────┐   ┌──────────────────────────────┐   ┌───────────────────────────┐
│ atlas-dashboard  │──▶│  atlas-gateway               │◀─▶│ atlas-shadowrisk          │
│ no credentials   │   │  ONLY process with live      │   │ separate code + database, │
│ writes control   │   │  trading credentials.        │   │ its own exchange reads;   │
│ requests (with   │   │  Primary risk manager,       │   │ signs approve/deny        │
│ your confirmation│   │  budget ledger, approvals,   │   └───────────────────────────┘
└──────────────────┘   │  order lifecycle,            │
                       │  reconciliation              │──▶ Kalshi / stock broker
                       └──────────────┬───────────────┘
                                      │ heartbeats, trading lock
                       ┌──────────────▼───────────────┐
                       │  atlas-watchdog              │  independent health checks;
                       │  can only DISABLE trading    │  sets lock, cancels entry orders,
                       │  and cancel entry orders     │  notifies you
                       └──────────────────────────────┘
```

**Why this shape:**
- **Paper and shadow trading cannot place live orders.** The engine process never loads a live credential and has no order-placement code. A test proves it: it scans the engine's imports and its network targets.
- **Strategies only propose.** The gateway is the only path to an exchange or broker. Its checks run in a fixed order, and any error rejects the order.
- **The shadow risk manager is truly independent.** It is a separate module that is forbidden from importing the primary risk code (a test enforces this), with its own database. Its ledger is rebuilt from the *exchange's* fills and balances, not from ATLAS's records. The gateway executes only with *both* an approval from the primary risk manager and a signed approval from the shadow risk manager for that exact order.
- **The watchdog can stop trading but never start it.** It has no code path that raises limits or grants authorization.

### 1.2 Storage
- SQLite databases (WAL mode), one per owner: `ledger.db` (gateway), `shadowrisk.db`, `audit.db`, `sim.db` (paper and shadow trading, physically separate), `market.db`.
- If any database is unreadable, the gateway rejects new live orders.

### 1.3 Money representation
- All amounts are **integers in units of $0.0001**. Kalshi quotes prices to 4 decimals, and stocks under $1 can trade in sub-pennies.
- No floats are used anywhere in authorization decisions; a test scans the risk code for float use.
- Kalshi's fee is computed per order and rounded up to the cent, as the exchange charges it.

### 1.4 Approval and authorization flow for one live order

1. A strategy proposes: market, side, maximum price, maximum quantity, reason, model version, probability, expected value.
2. Gateway pre-checks, in order: authentication, account status, kill switches and lock, clock check, daily authorization, module enabled, data age, executable price (from the live order book, not the last trade), liquidity and spread, duplicate check.
3. The primary risk manager computes the worst-case cost, *including fees*, and checks it against every limit. The most restrictive limit wins.
4. The correlation engine checks the combined exposure of correlated groups.
5. The shadow risk manager runs its own checks from its own data and returns a signed decision.
6. If per-trade approval is ON, you approve **that exact order**. The approval is bound to its market, side, quantity, maximum price and maximum total cost. Any material change voids it. It expires after a short window, 60 seconds by default.
7. Budget is reserved in one atomic database transaction, then the order is submitted with a pre-recorded `client_order_id`.
8. **If the response is unknown** (timeout, connection drop, 5xx), the gateway never resubmits. It looks up the order by `client_order_id` and reconciles first. The reservation stays until the outcome is confirmed.
9. Fills update the ledger at **actual** fill prices and fees. Unfilled amounts are released only after the cancellation or expiry is confirmed.

---

## 2. Initial limits (your spec values)

| Limit | Value |
|---|---|
| Global daily spending (purchases + fees, all modules) | $20.00 |
| Maximum individual purchase | $2.00 |
| Maximum exposure per market | $3.00 |
| Maximum daily realized loss | $3.00 |
| Maximum concurrent positions | 3 |
| Maximum open entry orders | 3 |
| Maximum consecutive losing trades | 3 |
| Category ceilings: Kalshi / Stocks / ETFs | $10 each (all inside the $20 total) |
| Accounting day | America/New_York |

**Proposed rule for changing limits** (your choice, see §5):
- **Lowering** a limit takes effect immediately.
- **Raising** a limit needs typed confirmation on the dashboard, is audit-logged, and takes effect at the **next** accounting day.
- Absolute ceilings in code ($100/day, $25 per order) cannot be raised from the dashboard at all.
- No strategy, optimizer or AI component has a write path to limits.

**Restart behavior:** "AUTHORIZE TODAY'S TRADING BUDGET" always comes back **OFF** after a restart, even on the same day. Autonomous loops never resume by themselves.

---

## 3. Development phases

Each phase ends with passing tests and a short report. **I stop after each phase for your review.** Realistically this is many sessions of work. I'll say plainly what is done, tested, partial or not started.

| Phase | Builds | Done when |
|---|---|---|
| **1. Audit** | Audit report, this plan, live mode disabled in the old bot | ✅ Done |
| **2. Safety foundation** ✅ Done ([report](03-phase2-report.md)) | Money types; global spending ledger with atomic reservations; primary risk manager; shadow risk manager (separate code and database); persistent authorization (default OFF, expires at NY midnight, OFF after restart); emergency stop; hash-chained audit log with integrity checks; watchdog; reconciliation framework; **a fake-exchange simulator** for timeouts, partial fills and errors | All Part 25 spending, authorization, risk, shadow-risk, audit and watchdog tests pass against the fake exchange |
| **3. Market data** ✅ Done ([report](05-phase3-report.md)) | Kalshi client checked against current docs and the demo exchange (orders, fills, cancels, client order IDs, rate limits); live price feed; multi-exchange BTC reference (Coinbase, Kraken, Bitstamp) to track BRTI better; stock/ETF data via the chosen broker; esports market data; data freshness and source on every data point | Demo-exchange tests show order states, partial fills and cancels handled correctly; stale-data rejection tested |
| **4. Analytics** | Probability/EV engine with prediction logging (Brier, log loss, calibration by bucket and strategy); strategy framework (each strategy with its own config, history and risk profile); scanner; correlation engine (rolling correlations, sector/industry, ETF holdings overlap, event groups) | Walk-forward backtests with no look-ahead (tested); correlation tests pass |
| **5. Shadow trading** | Continuous simulation on live data: realistic fills from order-book depth, partial fills, delay, fees, expiry; records missed and rejected opportunities | Separation tests pass (cannot reach live execution; separate balance) |
| **6. Execution gateway** | The full 12-step gateway, idempotency, order lifecycle, cancel confirmation, slippage caps, limit orders only | Every module routes through it; fault-injection tests pass |
| **7. Dashboard** | Account, safety, controls, scanner, correlation, shadow, activity panels; notifications; daily summary; confirmations for financial changes; **password login** | Accessibility audit passes; control-path tests pass |
| **8. Paper trading** | Weeks of paper and shadow running; stress and failure drills (kill processes, cut network, corrupt database, skew the clock) | Drills fail closed every time; enough paper trades to judge each strategy |
| **9. Manual-approval live** | Real orders only with daily authorization **and** your per-trade approval, starting with the smallest sizes | You turn it on yourself; I never do |
| **10. Optional autonomous** | Autonomous entries within the hard limits | Only if all tests pass and live results match shadow results |

---

## 4. What I cannot safely do, or can only partly do (honest limits)

1. **Read Kalshi's current API docs.** `docs.kalshi.com` is blocked by this environment's network settings. Phase 3 needs it allowed; I'll request that when we get there. Until then, every order field is treated as unverified, and the demo exchange is the source of truth.
2. **Stocks and ETFs need a broker with an API.** I recommend **Alpaca**:
   - It has a free paper-trading environment and fractional shares.
   - Its *paper* keys can feed market data to the engine without ever touching your real account.

   Whether its live API keys can be restricted to "trade only, no transfers" must be confirmed from its docs in Phase 3. If it can't, I'll say so before any live use.
3. **Esports live game state** (map score, rounds, substitutions) needs a paid feed such as PandaScore or GRID. Scraping sites like HLTV breaks their terms. Without a reliable feed, your own spec requires esports trading to stay **disabled**. ATLAS will still analyze and shadow-trade esports from Kalshi prices and historical results.
4. **ETF holdings data** for overlap checks needs a source. I'll start with issuer holdings files for a curated list (SPY, QQQ, sector SPDRs and similar). Unknown ETFs get conservative exposure assumptions, as the spec requires.
5. **The audit log will be tamper-*evident*, not tamper-*proof*.** On a PC you control, someone with full file access could rewrite the whole chain. To mitigate, the latest hash is included in each daily summary sent to you, so later edits are detectable.
6. **The watchdog needs to cancel orders**, which means it needs a trading credential. Kalshi keys probably can't be limited to "cancel only" (to be confirmed). The watchdog's code will contain only cancel calls, which a test enforces, and entry orders carry an exchange-side expiry as a backstop.
7. **Per-trade approval and 15-minute markets conflict.** A BTC 15-minute opportunity lasts seconds to minutes, and an approval expires if the price moves. Expect many approvals to expire. That is the safe outcome, and the spec accepts it.
8. **Profitability is not promised.**
   - The only edge found so far (BTC 15-minute favorites) is small and borderline.
   - Esports showed none.
   - No stock or ETF strategy has been tested.

   All new strategies start in shadow mode and must earn their way to live trading with out-of-sample evidence.
9. **Running 24/7 on your Windows PC:** I'll provide Task Scheduler setup. I can't install it on your PC myself, because remote access wasn't available.

---

## 5. Decisions (confirmed 2026-11-23)

1. **Stock/ETF broker:** Alpaca. Paper keys feed the engine's market data; live keys are held only by the gateway.
2. **Notifications:** Windows desktop notifications.
3. **Limit changes:** lowering takes effect immediately; raising needs typed confirmation and takes effect at the next accounting day; the absolute ceilings in code cannot be raised from the dashboard.
4. **Per-trade approval window:** 60 seconds.
5. **Location:** new `atlas/` folder; `kalshi/` stays for research.

**Dashboard preview built early.** You asked for the app now, so the Phase 7 dashboard exists as a front end running on clearly labelled sample data (`atlas/dashboard`). It has no connection to any exchange or broker and cannot place orders. Each later phase replaces sample panels with real data.
