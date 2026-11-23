# ATLAS

A safety-first, multi-market trading platform for Kalshi, esports, stocks and ETFs.
Priorities, in order: **capital preservation first, intelligent trading second, profitability third.**

## Status

| Phase | State |
|---|---|
| 1. Audit and plan | ✅ Done: [`docs/01-audit.md`](docs/01-audit.md), [`docs/02-plan.md`](docs/02-plan.md) |
| 2. Safety foundation | ✅ Done, see [`docs/03-phase2-report.md`](docs/03-phase2-report.md) |
| Dashboard | ✅ Built; shows real safety data, the live scanner and shadow trading read-only with `--data`; correlation panel still sample |
| 3. Market data | ✅ Done: real Kalshi + Alpaca clients, BTC reference, esports identity; see [`docs/05-phase3-report.md`](docs/05-phase3-report.md) |
| 4. Analytics | ✅ Done ([report](docs/07-phase4-report.md)): 4-month backtest found no reliable edge; that strategy is retired |
| 5. Shadow trading | ◐ Built and tested ([report](docs/08-phase5-report.md)); collecting live evidence |
| 6–10 | Not started |

**Live scanner (no keys, cannot trade):**
`python -m atlas.init_data --data "$env:USERPROFILE\.atlas\data"` once, then
`python -m atlas.scanner --data "$env:USERPROFILE\.atlas\data"` and, in a second window,
`python dashboard\server.py --data "$env:USERPROFILE\.atlas\data"`.

**Shadow trading (simulated money, no keys, cannot trade):** in another window run
`python -m atlas.shadow --data "$env:USERPROFILE\.atlas\data"`. Leave it running; results appear in the
dashboard's Shadow trading panel, and `python -m atlas.shadow --data ... --report` prints them.

**Connect your practice accounts:** follow [`docs/04-connecting.md`](docs/04-connecting.md), then run
`python -m atlas.check_connection` (read-only unless you add `--demo-order-test`).

ATLAS has never placed a real-money order. The only order it has ever sent was the one-contract
connection test on Kalshi's DEMO exchange. Live trading will only ever be enabled by you.

## Run the tests

```powershell
cd path\to\Zaluea\atlas
python -m pip install -r requirements.txt
python -m pytest tests
```

## Run the Hysilens dashboard (Windows)

Open PowerShell and run:

```powershell
cd path\to\Zaluea\atlas
python -m pip install -r requirements.txt
cd dashboard
python server.py
```

Then open **http://127.0.0.1:8770** in your browser. To show real safety data from an ATLAS data folder
(read-only), start it with `python server.py --data path\to\data`.

The dashboard is called **Hysilens**; ATLAS is the trading engine behind it. The page:
- shows your feed artwork at the top left, with a live activity feed under it
- has panels for the account, today's budget, safety limits, system health, trading controls, approvals, the market scanner, correlation, shadow trading, positions, activity history and the daily summary.

Every panel uses sample data, and the controls only change that sample data.

**Accessibility (WCAG 2.2 AA):**
- axe-core reports 0 violations on desktop, tablet and phone layouts.
- All text has at least 7:1 contrast against its background, checked by calculation because the layered glass backgrounds can't be measured automatically.
- Fully keyboard operable, with a skip link, visible focus rings and native confirmation dialogs.
- Important changes are announced to screen readers.
- Respects reduced-motion and high-contrast settings.

## Layout

```
atlas/
  atlas/        the platform: money, clock, storage, audit, limits, control, ledger,
                risk/ (primary), shadowrisk/ (independent), gateway, watchdog,
                exchange/ (fake, kalshi, alpaca), marketdata/ (btc, esports), check_connection
  tests/        163 automated tests (Part 25 acceptance tests, clients, simulators)
  docs/         audit, plan, phase reports
  dashboard/    server.py, sample_state.py, real_state.py, static/ (HTML, CSS, JS, feed image)
```
