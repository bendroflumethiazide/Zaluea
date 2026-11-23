# ATLAS

A safety-first, multi-market trading platform for Kalshi, esports, stocks and ETFs.
Priorities, in order: **capital preservation first, intelligent trading second, profitability third.**

## Status

| Phase | State |
|---|---|
| 1. Audit and plan | ✅ Done: [`docs/01-audit.md`](docs/01-audit.md), [`docs/02-plan.md`](docs/02-plan.md) |
| 2. Safety foundation | ✅ Done, see [`docs/03-phase2-report.md`](docs/03-phase2-report.md) |
| Dashboard | ✅ Built; shows real safety data read-only with `--data`; market panels still sample |
| 3. Market data | ✅ Done: real Kalshi + Alpaca clients, BTC reference, esports identity; see [`docs/05-phase3-report.md`](docs/05-phase3-report.md) |
| 4. Analytics (probability, EV, strategies, correlation) | Next |
| 5–10 | Not started |

**Connect your practice accounts:** follow [`docs/04-connecting.md`](docs/04-connecting.md), then run
`python -m atlas.check_connection` (read-only unless you add `--demo-order-test`).

No part of ATLAS can place a real order yet: the only exchange it knows is a fake one used for testing.
Live trading will only ever be enabled by you.

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
  tests/        166 automated tests (Part 25 acceptance tests, clients, simulators)
  docs/         audit, plan, phase reports
  dashboard/    server.py, sample_state.py, real_state.py, static/ (HTML, CSS, JS, feed image)
```
