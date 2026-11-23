# ATLAS Phase 3: Market data report

Date: 2026-11-23. Result: **168 automated tests, all passing**, plus a live check on Kalshi's demo exchange.
Nothing in this phase traded. The real clients were exercised against documented responses, a Kalshi HTTP simulator, and live **public** endpoints only.

## 1. Verified against current documentation (not memory)

I read Kalshi's OpenAPI spec 3.37.0 and its guides, and Alpaca's API reference, on 2026-11-23. That reading changed the design:

| Finding | Consequence |
|---|---|
| Kalshi's legacy `POST /portfolio/orders` now returns **410 Gone**. Orders use `POST /portfolio/events/orders` (V2). | The old bot's live mode could not have placed orders. ATLAS uses V2. |
| V2 quotes the YES leg only: `bid` = buy YES; buying NO at p = `ask` at 1−p. | Direction is translated and tested for all four cases. |
| Responses carry prices with **up to 6 decimals**. | Money is now exact to $0.000001, and limits are stored as decimal strings. |
| Contract counts can be fractional. | ATLAS trades whole contracts; a fractional fill is a data-integrity stop. |
| `side`/`action` fields are deprecated, and their stated earliest removal date (2026-05-28) has passed. | Only `outcome_side`/`book_side` are used. |
| Positions are one signed number per market (YES positive, NO negative). | ATLAS never holds both sides of one market (ledger and shadow both enforce this). |
| The docs on unknown outcomes say: "do not resubmit blindly"; a client order id doesn't protect after a fill or cancel. | This matches ATLAS's no-resubmit rule. Lookups go by client order id. |
| Reads lag writes; `/exchange/user_data_timestamp` tells how current they are. | Reconciliation waits for that timestamp. A lag is "not yet", not a mismatch. |
| Kalshi accounts are split into exchange shards; BTC 15-minute markets are on shard 2. | The connection check shows each shard's balance. |
| API keys have scopes, and Ed25519 keys are supported. | Plan: a trade-only gateway key (no `write::transfer`), read-only monitor keys, and both key types work. |
| Series fee types: `quadratic`, `quadratic_with_maker_fees`, `quadratic_with_combo_maker_fees` and `flat`. | Fees are computed exactly per series; unknown types are refused. |
| Alpaca quotes are JSON numbers. | Parsed as text; floats never reach ATLAS. |
| Alpaca accounts default to **margin ×4 with shorting**. | ATLAS refuses to trade unless the account is long-only, has no margin and has no options; an owner-only switch applies those settings. |
| Alpaca fractional shares require `time_in_force: day`. | Stock/ETF execution is held back until exact fractional accounting exists (Phase 6). |

## 2. What was built

| Component | File | Status |
|---|---|---|
| Money at $0.000001 | `atlas/money.py` | ✅ Fee exact at 6-decimal prices (3,000 random cases) |
| Kalshi client | `atlas/exchange/kalshi.py` | ✅ Signing (RSA and Ed25519), V2 orders, reads, error codes, no POST retries, no money-moving endpoints |
| Kalshi HTTP simulator | `tests/kalshi_sim.py` | ✅ The full stack runs end to end through the real client, including timeouts before and after processing and a 502 after processing |
| Fee schedule enforcement | `atlas/risk/primary.py`, `gateway.py`, shadow | ✅ The proposal's fee multiplier must match the live series; unknown types refused |
| Exchange-data wait | `atlas/gateway.py` | ✅ |
| BTC reference price | `atlas/marketdata/btc.py` | ✅ Median of Coinbase, Kraken and Bitstamp; stale or contradictory data gives no price |
| Alpaca client | `atlas/exchange/alpaca.py` | ◐ Reads, safety check and order builder ✅; execution held until Phase 6 |
| E-Sports identity | `atlas/marketdata/esports.py` | ✅ Rules text and ticker cross-check; esports trading disabled without a live game-state feed |
| Connection check | `atlas/check_connection.py` | ✅ Read-only by default; demo-only unfillable order test |
| Key guide | `docs/04-connecting.md` | ✅ |

**Live public checks (2026-11-23):**
- Kalshi time agreed with local time within 0.17 s.
- The BTC 15-minute market resolved to shard 2, fee `quadratic` ×1.
- The order book was consistent: YES bid/ask 20¢/21¢ and NO bid/ask 79¢/80¢.
- A CS2 market was classified as esports.
- Three BTC sources agreed within 0.03%.

## 3. Verified live on the owner's PC (Kalshi DEMO, 2026-11-23)

`python -m atlas.check_connection --demo-order-test` passed every check:
- **Signing:** the Ed25519 key signs correctly, and the clock was within ±2 s of Kalshi.
- **Reads:** balance, per-shard balances, positions, resting orders and the user-data timestamp all read correctly.
- **Order cycle:** one 1-contract YES order at $0.01, immediate-or-cancel, on `KXDEMOFUTHOURLY-26NOV23H03-UP`. It was accepted, filled 0 and cancelled, and was found again by its client order id with status cancelled and filled 0.

**Bugs found by the live runs and fixed** (none had shown up in simulated tests):
1. **Per-shard balances printed as `$None`.** The spec names the field `IndexedBalance.balance`; ATLAS read `balance_dollars`.
2. **Order prices were sent with 6 decimals and rejected** with "invalid dollar precision: -6". Request prices take 2–4 decimals. They are now sent with exactly 4, and any price needing more is refused before sending. The rejection was correctly treated as definitive.

The simulator now reproduces both behaviours, so tests cover them.

## 4. Not done or limited (honest list)

- **Alpaca has not been called with real (paper) keys yet.** Kalshi demo is verified as described above.
- **Stock and ETF execution is disabled** until the ledger supports exact fractional shares (Phase 6). Market data, account safety checks and order building are done.
- **Esports trading stays disabled:** there is no reliable live game-state feed (they are paid services). Identity validation works.
- **WebSocket streaming** (faster fills and order-book updates) is not built. REST polling is used. Faster flip detection comes with the strategy work.
- **The recommended Kalshi host** `external-api.kalshi.com` isn't on this cloud environment's allow-list. Tests here used the still-supported `api.elections.kalshi.com`; your PC has no such restriction.

## 5. What you can do now

Follow `docs/04-connecting.md`:
1. Create a Kalshi demo key and an Alpaca paper key.
2. Run `python -m atlas.check_connection` with them.
3. Send me the output; it never prints your secrets.
