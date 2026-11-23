"""Progress against the Phase 5 decision rules (docs/08-phase5-report.md), fixed before any results existed.

The maker strategy may be proposed for live trading only if, in the RESEARCH account:
  1. at least 200 filled orders have settled;
  2. total P&L is above zero with the pessimistic maker fee, and the per-trade t-statistic is at least 2;
  3. at least 60% of calendar weeks (New York time) are not losing;
  4. the as_live account would not have breached any limit (its books reconcile; the limits are enforced by
     the same ledger code the live system uses).
Diagnostic, not a rule: if unfilled orders win far more often than filled ones, fills are being picked off.

Even a full pass only allows a PROPOSAL. Live trading stays off until the owner enables it.
"""
from __future__ import annotations

import math
from datetime import datetime

from ..clock import ACCOUNTING_TZ
from ..money import Money

MIN_SETTLED = 200
MIN_T = 2.0
MIN_GOOD_WEEKS_PCT = 60
PICKED_OFF_GAP_PTS = 15          # diagnostic: missed win rate this many points above filled win rate
PICKED_OFF_MIN_EACH = 20         # ...once both samples have at least this many orders


def t_statistic(pnls: list[int]) -> float | None:
    """Mean over its standard error. None when it is undefined (fewer than 2 trades, or no spread)."""
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    return None if var == 0 else mean / math.sqrt(var / n)


def weekly_totals(pnls: list[int], stamps: list[float]) -> dict[str, int]:
    """P&L per ISO calendar week in New York time, keyed like '2026-W48'."""
    out: dict[str, int] = {}
    for p, ts in zip(pnls, stamps):
        y, w, _ = datetime.fromtimestamp(ts, ACCOUNTING_TZ).isocalendar()
        k = f"{y}-W{w:02d}"
        out[k] = out.get(k, 0) + p
    return out


def evaluate(research_totals: dict, research: dict, as_live: dict | None) -> dict:
    """research_totals: report._ledger_totals() of the research ledger; research / as_live: account summaries."""
    pnls, stamps = research_totals["closed"], research_totals["closed_ts"]
    n, total = len(pnls), sum(pnls)
    t = t_statistic(pnls)
    weeks = weekly_totals(pnls, stamps)
    good = sum(1 for v in weeks.values() if v >= 0)
    good_pct = round(100 * good / len(weeks), 1) if weeks else None

    rules = [
        {"id": 1, "rule": f"At least {MIN_SETTLED} settled fills (research account)",
         "value": f"{n} of {MIN_SETTLED}", "progress_pct": min(100, round(100 * n / MIN_SETTLED)),
         "passed": n >= MIN_SETTLED},
        {"id": 2, "rule": f"P&L above zero with the pessimistic maker fee, and t-statistic at least {MIN_T:g}",
         "value": f"{Money(total)}, t = {'–' if t is None else f'{t:.2f}'}",
         "passed": total > 0 and t is not None and t >= MIN_T},
        {"id": 3, "rule": f"At least {MIN_GOOD_WEEKS_PCT}% of calendar weeks not losing",
         "value": "no settled weeks yet" if not weeks else f"{good} of {len(weeks)} weeks ({good_pct}%)",
         "passed": good_pct is not None and good_pct >= MIN_GOOD_WEEKS_PCT},
        {"id": 4, "rule": "The as_live account never breached a limit (its books reconcile)",
         "value": "no as_live account" if as_live is None else
                  ("books match" if as_live["reconciled"] else "BOOKS DO NOT MATCH"),
         "passed": as_live is not None and bool(as_live["reconciled"])},
    ]
    if all(r["passed"] for r in rules):
        verdict, why = "PASS", "Every rule is met. This allows a proposal only; live trading stays off until you enable it."
    elif not rules[0]["passed"]:
        verdict, why = "NOT YET", f"Too few settled fills to judge ({n} of {MIN_SETTLED}). Keep it running."
    else:
        failed = ", ".join(str(r["id"]) for r in rules if not r["passed"])
        verdict, why = "FAIL", f"Enough evidence, but rule {failed} is not met. The strategy stays off."

    fw, mw = research.get("win_rate_pct"), research.get("missed_win_rate_pct")
    picked_off = (fw is not None and mw is not None and research["settled"] >= PICKED_OFF_MIN_EACH
                  and research["missed_resolved"] >= PICKED_OFF_MIN_EACH and mw - fw >= PICKED_OFF_GAP_PTS)
    return {"verdict": verdict, "why": why, "rules": rules, "weeks": weeks, "t": t,
            "diagnostic": {"filled_win_rate": fw, "missed_win_rate": mw, "picked_off": picked_off,
                           "note": ("Unfilled bids win far more often than filled ones: fills look picked off."
                                    if picked_off else
                                    "Compare these two win rates; a large gap in favour of unfilled bids is a warning.")}}
