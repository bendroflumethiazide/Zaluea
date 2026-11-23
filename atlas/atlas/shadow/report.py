"""Shadow-trading results: per simulated account, plus the real account kept strictly apart.

Every figure is computed from the simulated ledgers and the journal with exact integer money.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from ..money import Money

LABEL = "SIMULATED: shadow trading with no real money. Real results are shown separately."


def _ledger_totals(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        fees = con.execute("SELECT COALESCE(SUM(fee),0) f FROM executions WHERE action='buy'").fetchone()["f"]
        rows = con.execute("SELECT market, side, action, qty, price, fee, ts FROM executions ORDER BY seq").fetchall()
        bought = con.execute("SELECT COALESCE(SUM(qty),0) q FROM executions WHERE action='buy'").fetchone()["q"]
    finally:
        con.close()
    basis, held, closed, closed_ts = {}, {}, [], []
    for r in rows:
        k = (r["market"], r["side"])
        if r["action"] == "buy":
            basis[k] = basis.get(k, 0) + r["qty"] * r["price"] + r["fee"]
            held[k] = held.get(k, 0) + r["qty"]
        else:
            q = held.get(k, 0)
            removed = basis.get(k, 0) if r["qty"] == q else basis.get(k, 0) * r["qty"] // max(q, 1)
            basis[k] = basis.get(k, 0) - removed
            held[k] = q - r["qty"]
            closed.append(r["qty"] * r["price"] - r["fee"] - removed)
            closed_ts.append(r["ts"])
    open_pos = [{"market": k[0], "side": k[1], "qty": held[k], "basis": basis[k]} for k in held if held[k] > 0]
    return {"fees": fees, "closed": closed, "closed_ts": closed_ts, "open_cost": sum(p["basis"] for p in open_pos), "positions": open_pos,
            "bought": bought}


def account_summary(folder: Path, name: str, kind: str, journal) -> dict:
    t = _ledger_totals(folder / "sim_ledger.db")
    j = journal.rows(account=name)
    count = Counter(r["kind"] for r in j)
    filled_orders = {r["coid"] for r in j if r["kind"] == "fill"}
    contracts = sum(r["qty"] for r in j if r["kind"] == "fill")
    missed = [r for r in j if r["kind"] == "missed"]
    missed_done = [r for r in missed if r["outcome"]]
    rejected = [r for r in j if r["kind"] == "rejected"]
    reasons = Counter(json.loads(r["detail"]).get("reason", "")[:100] if r["detail"] else "" for r in rejected)
    pnl = sum(t["closed"])
    wins = sum(1 for x in t["closed"] if x > 0)
    out = {
        "account": name, "order_type": kind,
        "submitted": count["submitted"], "orders_filled": len(filled_orders),
        "fill_rate_pct": round(100 * len(filled_orders) / count["submitted"], 1) if count["submitted"] else None,
        "contracts_filled": contracts, "partial_remainders": sum(1 for r in missed if r["coid"] in filled_orders),
        "settled": len(t["closed"]), "wins": wins,
        "win_rate_pct": round(100 * wins / len(t["closed"]), 1) if t["closed"] else None,
        "pnl_total": str(Money(pnl)), "fees_paid": str(Money(t["fees"])), "open_cost": str(Money(t["open_cost"])),
        "missed": len(missed), "missed_resolved": len(missed_done),
        "missed_win_rate_pct": round(100 * sum(1 for r in missed_done if r["outcome"] == "won") / len(missed_done), 1)
        if missed_done else None,
        "missed_hypothetical_pnl": str(Money(sum(r["hypo_pnl"] for r in missed_done))),
        "rejected": len(rejected), "rejected_reasons": reasons.most_common(3),
        "open_orders": 0,
        "reconciled": contracts == t["bought"],         # journal and simulated ledger must agree exactly
        "positions": t["positions"],
        "curve_c": [sum(t["closed"][:i + 1]) // 10_000 for i in range(len(t["closed"]))],   # display only
    }
    if kind == "maker":   # the maker fee is an assumption: show the result if makers pay nothing
        out["pnl_if_no_maker_fee"] = str(Money(pnl + t["fees"]))
    return out


def real_summary(data_dir: Path) -> dict:
    path = data_dir / "ledger.db"
    if not path.exists():
        return {"available": False, "note": "No real ledger in this folder. Live trading is off."}
    t = _ledger_totals(path)
    return {"available": True, "pnl_total": str(Money(sum(t["closed"]))), "settled": len(t["closed"]),
            "note": "Real account (read-only). Live trading is off unless you authorized it."}


def maker_evidence(folders: dict[str, Path], summaries: dict[str, dict]) -> dict[str, dict]:
    """Decision-rule progress for every resting-order strategy that has a research account.
    folders / summaries are keyed by account name ('<strategy>/<research|as_live>')."""
    from .evidence import evaluate
    out = {}
    for name, a in summaries.items():
        if a["order_type"] == "maker" and name.endswith("/research"):
            strategy = name.rsplit("/", 1)[0]
            out[strategy] = evaluate(_ledger_totals(folders[name] / "sim_ledger.db"), a,
                                     summaries.get(f"{strategy}/as_live"))
    return out


def build_state(runner, now: float, notes: list[str]) -> dict:
    accounts, folders = [], {}
    for s, acct in runner.accounts:
        a = account_summary(acct.folder, acct.name, acct.kind, runner.journal)
        a["open_orders"] = len(acct.sim.open_orders())
        accounts.append(a)
        folders[acct.name] = acct.folder
    recent = [{"ts": r["ts"], "account": r["account"], "market": r["market"], "side": r["side"],
               "kind": r["kind"], "qty": r["qty"],
               "price": str(Money(r["price"])) if r["price"] is not None else None,
               "outcome": r["outcome"]} for r in runner.journal.rows(limit=25) if r["kind"] != "note"]
    return {"ts": now, "label": LABEL, "simulated": True,
            "assumptions": {"latency_s": accounts and runner.accounts[0][1].sim.latency_s,
                            "maker_fee": f"{runner.maker_fee_pct}% of the taker fee formula (pessimistic)",
                            "taker_fills": "only up to the size displayed at the ask, after the latency",
                            "resting_fills": "only when a later public trade sells through our price, "
                                             "after the queue ahead of us"},
            "reference_ok": runner.ref_ok, "reference_note": runner.ref_reason,
            "feed_notes": [n for n in runner.feed_notes.values() if n], "notes": notes,
            "latest": runner.latest, "accounts": accounts, "recent": recent,
            "evidence": maker_evidence(folders, {a["account"]: a for a in accounts}),
            "real": real_summary(runner.data_dir)}


def report_markdown(data_dir: str | Path) -> str:
    from .journal import Journal
    root = Path(data_dir) / "sim"
    if not (root / "journal.db").exists():
        return "No shadow-trading data yet. Start it with: python -m atlas.shadow --data <folder>"
    journal = Journal(root / "journal.db")
    lines = [f"# Shadow trading report", "", f"**{LABEL}**", ""]
    lines += ["| Account | Orders | Filled | Fill rate | Settled | Win rate | P&L | Fees | "
              "Missed (resolved) | Missed win rate | Missed hypothetical P&L | Rejected |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    makers, takers, folders = [], [], {}
    for ledger in sorted(root.glob("*/*/sim_ledger.db")):
        folder = ledger.parent
        name = f"{folder.parent.name}/{folder.name}"
        kind = "maker" if folder.parent.name == "crypto_maker" else "taker"
        a = account_summary(folder, name, kind, journal)
        folders[name] = folder
        pct = lambda v: "–" if v is None else f"{v}%"
        lines.append(f"| {name} | {a['submitted']} | {a['orders_filled']} | {pct(a['fill_rate_pct'])} | {a['settled']} | "
                     f"{pct(a['win_rate_pct'])} | {a['pnl_total']} | {a['fees_paid']} | {a['missed']} "
                     f"({a['missed_resolved']}) | {pct(a['missed_win_rate_pct'])} | {a['missed_hypothetical_pnl']} | "
                     f"{a['rejected']} |")
        (makers if kind == "maker" else takers).append(a)
    for a in makers:
        lines.append(f"\n{a['account']}: P&L if resting orders pay no maker fee: {a['pnl_if_no_maker_fee']}")
    for strategy, ev in maker_evidence(folders, {a["account"]: a for a in makers + takers}).items():
        lines += ["", f"## Decision rules: {strategy} — {ev['verdict']}", "", ev["why"], ""]
        lines += [f"- {'✅' if r['passed'] else '⬜'} {r['id']}. {r['rule']}: {r['value']}" for r in ev["rules"]]
        d = ev["diagnostic"]
        pct = lambda v: "–" if v is None else f"{v}%"
        lines.append(f"- Diagnostic: filled bids won {pct(d['filled_win_rate'])}, unfilled bids would have won "
                     f"{pct(d['missed_win_rate'])}. {d['note']}")
    real = real_summary(Path(data_dir))
    bad = [a for a in makers + takers if not a["reconciled"]]
    lines.append("" if not bad else "\n**WARNING: the journal and the simulated ledger disagree for "
                 + ", ".join(a["account"] for a in bad) + ". These results are not reliable.**")
    lines += ["", "## Real account (kept separate)", "", real["note"]]
    if real["available"]:
        lines.append(f"Real realized P&L: {real['pnl_total']} over {real['settled']} settled trades.")
    lines += ["", "How to read this: a resting-order strategy is only promising if the win rate of FILLED orders "
              "is close to the win rate of MISSED ones. If missed orders win far more often, fills are being "
              "picked off (adverse selection) and the strategy would lose live."]
    journal.close()
    return "\n".join(lines)
