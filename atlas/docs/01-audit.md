# ATLAS Phase 1: Repository audit

Audit date: 2026-11-23
Checkpoint: commit `e762d85` on branch `claude/kalshi-bot-rvdyqo`. It is pushed to GitHub, and every later change can be rolled back to it.

## 1. What is in the repository

The repository holds two unrelated projects:

| Path | What it is | ATLAS action |
|---|---|---|
| `/` (`index.mjs`, `Site/`, `package.json`, `Procfile`, `app.json`) | "Zaluea", a Node.js web proxy (Ultraviolet / bare-server) | **Not touched.** ATLAS lives in its own `atlas/` folder. |
| `kalshi/` | The current Kalshi research and trading bot (Python) | Audited below. Useful parts are reused; unsafe parts are replaced. |

There is no database, no test suite, no scheduler, no CI and no secret store. The bot reads two environment variables (`KALSHI_KEY_ID`, `KALSHI_KEY_FILE`) and writes files under `kalshi/data/`, which git ignores.

## 2. Component inventory (`kalshi/`)

| File | Purpose | State |
|---|---|---|
| `kalshi_client.py` | Read-only client for Kalshi's public market data. Includes pagination, batch candlesticks, an adaptive rate-limit back-off, and fee and number helpers. | Works; tested at volume (~24k markets). **Reusable.** |
| `collect_btc15m.py`, `analyze_btc15m.py` | Research on the 15-minute BTC markets (2,848 markets, 30 days) plus Coinbase prices. | Works. **Reusable** as the basis of the research pipeline. |
| `collect_esports.py`, `analyze_esports.py` | Research on esports markets (23,234 markets, 120 days). | Works. **Reusable.** |
| `bot.py` | One-process strategy, sizing, order sending, position tracking, settlement and limits for BTC 15-minute markets. Modes: paper, demo, live. | Paper mode works. **Live path is unsafe** (§4). **Live mode is now disabled** (§6). |
| `dashboard.py`, `ui/` | Local web dashboard (127.0.0.1): status, pause/resume, settings. Accessible to WCAG 2.2 AA; axe-core reports 0 violations. | Works. **UI patterns reusable.** Its control model must be redesigned (§4). |
| `reports/` | Study results. | Reference material. |

## 3. Answers to the twelve audit questions

**1. What the bot does.** It watches the current KXBTC15M market (BTC up or down over 15 minutes). In minutes 10–13 it considers buying the favorite (YES or NO) when that side is priced 0.80–0.97 and every "flip guard" passes:
- BTC is at least 2 standard deviations from the strike.
- The model probability is at least the price plus the fee.
- The price has not dropped 5¢ or more in about 2 minutes.
- BTC has not jumped toward the strike in the last 30 seconds.

It holds one position at a time and sells if its side drops to 0.50. Esports is research only; it never traded esports.

**2. How it finds opportunities.** It polls the REST API every 5 seconds for the one open BTC 15-minute market. It does not scan any other market.

**3. How it estimates probability.** It uses a random-walk (normal distribution) model on Coinbase BTC-USD: `P = Φ(ln(S/K) / (k·σ·√T))`.
- σ is the standard deviation of the last 60 one-minute log returns.
- k = 1.05 was fitted on the first half of 30 days of data.
- K is Coinbase's price in the minute before open, used as a proxy for CF Benchmarks' BRTI.

The proxy differs from the actual settlement index by -$14 to +$24 (5th–95th percentile). Calibration was measured; the market price beat the model at every minute.

**4. How it submits orders.** It sends one `POST /portfolio/orders` request as an immediate-or-cancel limit order with a random `client_order_id`. **The field names (`yes_price_dollars`/`no_price_dollars`, `time_in_force: "immediate_or_cancel"`) and the fill fields it reads (`fill_count_fp`/`fill_count`) were written from memory.** The official documentation could not be read from this environment, and the code has never run against the demo exchange. The request signing (RSA-PSS SHA-256 over `timestamp + method + path`) was verified against a locally generated key, and the demo exchange answered a fake key with HTTP 401, which confirms the authentication path is reachable.

**5. How it tracks positions.** It keeps one position in memory (`self.pos`) plus a local append-only file (`data/bot_trades.jsonl`). After a restart it rebuilds open positions **from that local file only**. It never asks the exchange what it actually holds.

**6. How it handles API failures.** On HTTP 429 or 5xx it waits and **resends the same request, including order submissions**, up to 5 times. Network errors and timeouts escape to the main loop, which logs them and tries again 5 seconds later. There is no per-module disable, no back-off state and no reconciliation.

**7. Financial protections that exist.**
- Amount per trade (default $5, dashboard range $1–$25).
- Daily spending limit ($20) computed from local entry records.
- Daily realized loss limit ($15).
- Maximum trades per day (20).
- One position at a time; one entry per market.
- A `STOP` file kill switch.
- Live mode needed an explicit flag.
- Paper mode loads no keys, so it cannot sign an order.

**8. What could cause accidental overspending.**
- **(Critical)** The order retry loop in `bot.py` `Kalshi.req` (lines 109–119) re-POSTs after a 5xx or 429, even though the first request may have been accepted.
- **(Critical)** A timeout or connection error on the POST loses the response. The entry is never recorded, so 5 seconds later the same market looks "untraded", and a *new* order with a *new* `client_order_id` can be sent (lines 365–380, 452).
- **(Critical)** If the response's fill fields are named differently than assumed, `filled` reads 0. The bot logs "not filled" and keeps no record while the exchange holds contracts, so it can buy again (lines 373–378).
- **(High)** The budget lives only in a local JSON file. Deleting or corrupting it resets spending to $0. This fails open.
- **(High)** Nothing stops two copies of the bot running at once, and two copies race on the same budget.
- **(High)** Settings in `data/bot_settings.json` can be raised by any local process or browser request to localhost, take effect immediately, and need no confirmation.
- **(Medium)** Costs are estimated as the limit price plus an approximate fee, not the actual fill price and fee. Kalshi rounds fees up to the cent per order.
- **(Medium)** All money is floating-point.

**9. What could cause duplicate trades.** All three critical items in question 8. The only duplicate guard ("one entry per ticker today") depends on an entry record that is written *after* a successful response.

**10. What could cause incorrect position reporting.**
- **(Critical)** The stop-loss sell (line 404) ignores the order result. The position is marked closed, and profit or loss is booked at the *assumed* price even if the immediate-or-cancel sell did not fill. The bot then believes it is flat while holding contracts, and may open another position.
- **(High)** There is no reconciliation against exchange positions, resting orders, fills or balance.
- **(Medium)** Partial fills are booked at the limit price; the real average price and fees are not read.
- **(Medium)** "Today" is the UTC day; the spec requires America/New_York.

**11. Reusable components.** The public market-data client and its rate-limit handling, the research collectors and studies, the request-signing code, the flip-guard model (as one strategy behind the new risk pipeline), the dashboard's accessibility patterns and dark theme, and the backtest findings.

**12. Components that need redesign.** Order submission, position tracking, budget and loss accounting, configuration control, the dashboard's control path, logging, restart recovery, and process structure. The current bot has a single process where the strategy calls the exchange directly. The spec forbids that: strategies must only *propose* orders.

## 4. Other findings

| # | Finding | Severity |
|---|---|---|
| F1 | No check that the system clock is correct; daily limits depend on it. | High |
| F2 | No watchdog. If the bot crashes while holding a position, the stop-loss stops being monitored. Loss is capped by the contract cost, but nobody is told. | High |
| F3 | The dashboard has no authentication. It only checks that requests come from the same origin and use a JSON content type, so any local program can pause trading or change settings. | Medium |
| F4 | Logs are plain, editable text. Nothing detects tampering. | Medium |
| F5 | Paper and live share one code path, separated only by `if self.mode != "paper"`. The separation is incidental (no key loaded), not structural. | Medium |
| F6 | The strategy's evidence is thin: one month of data, parameters chosen by grid search, fills assumed at the quoted price, t ≈ 2.3 on 84 out-of-sample trades. | Medium (strategy risk) |
| F7 | Esports showed no edge after costs on 23k markets. It has no live game-state data source. | Info |
| F8 | Research data exists only in a temporary cloud container (not committed, by design). | Info |
| F9 | Whether the repository is public is not confirmed. It must never contain keys either way. | Info |

## 5. Conflicts between the existing bot and the ATLAS spec

| Spec requirement | Existing bot | Resolution |
|---|---|---|
| Maximum individual purchase $2 | $5 default; up to $25 allowed | New defaults follow the spec. |
| Daily realized loss $3 | $15 | Spec value. |
| Accounting day in America/New_York | UTC | Spec value. |
| Exact money arithmetic | Floats | Integer money units (see plan). |
| Strategies cannot execute | Strategy calls the exchange directly | Separate execution gateway process. |
| Fail closed | Missing budget file means $0 spent (fails open) | Missing or unreconstructable ledger blocks orders. |

## 6. Actions taken during this audit

1. Recorded checkpoint `e762d85` (already on GitHub).
2. **Disabled `--mode live` in `kalshi/bot.py`.** It now exits with an explanation. Paper and demo (fake money) still work. This removes the only route by which the existing code could spend real money, until ATLAS's gateway replaces it.

No other existing behavior was changed.
