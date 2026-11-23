# ATLAS

A safety-first, multi-market trading platform for Kalshi, esports, stocks and ETFs.
Priorities, in order: **capital preservation first, intelligent trading second, profitability third.**

## Status

| Phase | State |
|---|---|
| 1. Audit and plan | ✅ Done: [`docs/01-audit.md`](docs/01-audit.md), [`docs/02-plan.md`](docs/02-plan.md) |
| Dashboard preview | ✅ Front end built and tested on **sample data**. It is not connected to any exchange or broker and cannot place orders. |
| 2. Safety foundation | Next |
| 3–10 | Not started |

No part of ATLAS can place a real order yet. Live trading will only ever be enabled by you.

## Run the dashboard preview (Windows)

Open PowerShell and run:

```powershell
cd path\to\Zaluea\atlas
python -m pip install -r requirements.txt
cd dashboard
python server.py
```

Then open **http://127.0.0.1:8770** in your browser.

The page:
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
  docs/         audit, plan, and later the final engineering report
  dashboard/    server.py, sample_state.py, static/ (HTML, CSS, JS, feed image)
```
