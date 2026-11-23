"""Shadow-trading journal: every decision, order, fill, cancellation, rejection and missed
opportunity, so reports can show not only what was traded but what was NOT and what that cost.

Missed and rejected opportunities are resolved after settlement with a HYPOTHETICAL result
(what the order would have made had it filled in full at its price), which is how adverse
selection becomes visible: if unfilled resting orders win more often than filled ones, the
fills are being picked off.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..money import Money

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, day TEXT NOT NULL, account TEXT NOT NULL, strategy TEXT NOT NULL,
    market TEXT NOT NULL, side TEXT, kind TEXT NOT NULL,
    qty INTEGER, price INTEGER, fee INTEGER, coid TEXT, detail TEXT,
    outcome TEXT, hypo_pnl INTEGER
);
CREATE INDEX IF NOT EXISTS events_market ON events(market, kind);
"""

KINDS = {"submitted", "fill", "cancelled", "expired", "rejected", "missed", "settled", "decision", "note"}
HYPOTHETICAL = ("missed", "rejected")


class Journal:
    def __init__(self, path: str | Path):
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    def add(self, *, ts: float, day: str, account: str, strategy: str, market: str, kind: str,
            side: str | None = None, qty: int | None = None, price: Money | None = None,
            fee: Money | None = None, coid: str | None = None, detail: dict | str | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown journal kind {kind}")
        if isinstance(detail, dict):
            detail = json.dumps(detail, default=str)
        with self.con:
            cur = self.con.execute(
                "INSERT INTO events (ts, day, account, strategy, market, side, kind, qty, price, fee, coid, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, day, account, strategy, market, side, kind, qty,
                 price.units if price is not None else None, fee.units if fee is not None else None, coid, detail))
        return cur.lastrowid

    def unresolved_markets(self) -> set[str]:
        return {r["market"] for r in self.con.execute(
            f"SELECT DISTINCT market FROM events WHERE kind IN {HYPOTHETICAL} AND outcome IS NULL")}

    def resolve(self, market: str, result: str) -> int:
        """Give every missed/rejected opportunity in `market` its hypothetical result."""
        n = 0
        with self.con:
            for r in self.con.execute(f"SELECT * FROM events WHERE market=? AND kind IN {HYPOTHETICAL} "
                                      "AND outcome IS NULL", (market,)).fetchall():
                won = r["side"] == result
                pnl = (r["qty"] * 1_000_000 if won else 0) - r["qty"] * r["price"] - (r["fee"] or 0)
                self.con.execute("UPDATE events SET outcome=?, hypo_pnl=? WHERE id=?",
                                 ("won" if won else "lost", pnl, r["id"]))
                n += 1
        return n

    def rows(self, account: str | None = None, kinds: tuple[str, ...] | None = None, limit: int | None = None):
        q, args = "SELECT * FROM events WHERE 1=1", []
        if account:
            q += " AND account=?"; args.append(account)
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"; args += list(kinds)
        q += " ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
        return [dict(r) for r in self.con.execute(q, args)]

    def close(self):
        self.con.close()
