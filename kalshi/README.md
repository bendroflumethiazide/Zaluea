# Kalshi research and trading bot

Studies of Kalshi's **BTC 15-minute** and **esports** markets, plus a bot
that trades the one pattern that held up in testing.

| File | What it does |
|---|---|
| `kalshi_client.py` | Read-only client for Kalshi's public market data (no key needed) |
| `collect_btc15m.py` | Downloads settled BTC 15-min markets, minute prices and Coinbase BTC prices |
| `analyze_btc15m.py` | Writes `reports/btc15m.md` |
| `collect_esports.py` | Downloads settled esports match and map markets with minute prices |
| `analyze_esports.py` | Writes `reports/esports.md` |
| `bot.py` | BTC 15-min favorite bot with flip guards (paper / demo / live) |
| `dashboard.py`, `ui/` | Friendly local dashboard for the bot |
| `reports/` | The study results and `bot_performance.md` |

## Setup

```bash
cd kalshi
pip install -r requirements.txt
python collect_btc15m.py --days 30 && python analyze_btc15m.py
python collect_esports.py --days 120 && python analyze_esports.py
```

Downloaded data goes to `kalshi/data/` (not committed; it is large).

## The bot

```bash
python bot.py                       # paper trading: live prices, simulated $5 trades
python bot.py --mode demo           # Kalshi demo exchange, fake money
python bot.py --mode live --i-understand-real-money
python bot.py --report              # rebuild reports/bot_performance.md
```

### Dashboard

Run it in a second terminal, next to the bot:

```bash
python dashboard.py      # then open http://127.0.0.1:8765
```

The dashboard shows:
- what the bot is doing, in plain words
- today's results
- the market it's watching
- every flip-guard check as it runs
- recent trades

It can pause or resume trading and change the stake and daily limits.
It cannot switch to real money; that stays a deliberate command-line step.
It listens only on your own computer and refuses requests from other websites.

**Accessibility (WCAG 2.2 AA):**
- Tested with axe-core: 0 violations in light, dark and phone layouts.
- Fully keyboard operable, with a skip link and visible focus rings.
- Proper headings, labels and table headers throughout.
- Pass/fail is shown with text and icons, not color alone.
- Status changes and finished trades are announced to screen readers; the ticking timer is not.
- Form errors are listed in a summary that links to each field.
- Dark theme throughout; follows the system reduced-motion and high-contrast settings.
- Works at 200% text size and on phone screens.

Demo and live need an API key from Kalshi (Account → API Keys):

```bash
export KALSHI_KEY_ID=your-key-id
export KALSHI_KEY_FILE=/path/to/kalshi-private-key.pem   # never commit this file
```

Use a key from **demo.kalshi.co** for `--mode demo`; demo and live keys are separate.

**Strategy.** In minutes 10–13 of each 15-minute window, the bot buys the
favorite when it is priced 80–97¢, but only if every flip guard passes:

1. BTC is at least 2 standard deviations from the strike, measured with
   recent 1-minute volatility.
2. The model's probability is at least the price plus the fee.
3. The price has not dropped more than 5¢ in the last 2 minutes, which would
   mean a flip is already under way.
4. BTC has not jumped toward the strike in the last 30 seconds.

While it holds a position, the bot sells if the held side's price falls to
50¢ (stop-loss).

**Safety.**
- About $5 at risk per trade (`--stake`).
- At most $20 put into new trades per day (`--max-daily-spend`), so 4 trades at $5 each.
- $15 daily loss cap (`--max-daily-loss`).
- At most 20 trades a day.
- One position at a time.
- Create a file named `STOP` in this folder to pause it instantly.

**Honest expectations.** Each win earns about 10–15¢ on a $5 stake. Each loss
not caught by the stop-loss costs almost the full $5. The backtest edge is
small (t ≈ 2.3 on 84 out-of-sample trades). It was measured from quoted
prices, not real fills, over one month of data. Treat it as unproven until
paper and demo trading confirm it.
