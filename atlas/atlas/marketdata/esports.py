"""E-Sports event identity for Kalshi esports markets.

Your spec: "Validate event identity carefully. Never confuse different
teams, tournaments, maps, matches... If live information is delayed,
missing, or contradictory, disable trading for that event."

Each market's identity is parsed from its official rules text and then
cross-checked against its ticker. Any contradiction (different time, map
number, or a YES team that is not one of the two teams) makes the identity
INVALID, and invalid events are never traded.

Live game state (current map score, rounds, substitutions) needs a paid
data feed that ATLAS does not have. Until one is configured,
`LIVE_FEED_AVAILABLE` is False and the gateway refuses every esports order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LIVE_FEED_AVAILABLE = False

GAMES = {"CS2": "CS2", "League of Legends": "LoL", "Valorant": "Valorant", "Dota 2": "Dota 2",
         "Rainbow Six": "Rainbow Six", "Overwatch": "Overwatch", "Call of Duty": "Call of Duty",
         "Rocket League": "Rocket League", "Mobile Legends": "Mobile Legends"}
SERIES_GAME = {"KXCS2GAME": "CS2", "KXCS2MAP": "CS2", "KXLOLGAME": "LoL", "KXLOLMAP": "LoL",
               "KXVALORANTGAME": "Valorant", "KXVALORANTMAP": "Valorant", "KXDOTA2GAME": "Dota 2",
               "KXDOTA2MAP": "Dota 2", "KXR6GAME": "Rainbow Six", "KXOWGAME": "Overwatch",
               "KXCODGAME": "Call of Duty", "KXRLGAME": "Rocket League"}
MAP_SERIES = {s for s in SERIES_GAME if s.endswith("MAP")}

_RULES = re.compile(
    r"^If (?P<sel>.+?) wins (?:(?:game|map) (?P<n>\d+) in )?the (?P<tour>.+): (?P<a>.+?) vs\. (?P<b>.+?) "
    r"(?P<game>" + "|".join(re.escape(g) for g in GAMES) + r") match originally scheduled for "
    r"(?P<date>[A-Z][a-z]{2} \d{1,2}, \d{4}) at (?P<time>\d{1,2}:\d{2} [AP]M) (?P<tz>E[SD]T)\b")
_TICKER = re.compile(r"^(?P<series>[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<hhmm>\d{4})"
                     r"(?P<codes>[A-Z0-9]+)(?:-(?P<n>\d+))?-(?P<sel>[A-Z0-9]+)$")


@dataclass
class EsportsIdentity:
    ticker: str
    valid: bool
    problems: list[str] = field(default_factory=list)
    game: str | None = None
    kind: str | None = None             # "match" or "map"
    map_number: int | None = None
    tournament: str | None = None
    team_a: str | None = None
    team_b: str | None = None
    yes_team: str | None = None
    scheduled_start: float | None = None
    source: str = "kalshi:rules_primary"

    @property
    def tradable(self) -> tuple[bool, str]:
        if not self.valid:
            return False, "Event identity is uncertain: " + "; ".join(self.problems)
        if not LIVE_FEED_AVAILABLE:
            return False, "No reliable live game-state feed: esports trading is disabled"
        return True, ""


def identify(ticker: str, rules_primary: str) -> EsportsIdentity:
    ident = EsportsIdentity(ticker=ticker, valid=False)
    bad = ident.problems.append
    r = _RULES.match(rules_primary or "")
    t = _TICKER.match(ticker or "")
    if not r:
        bad("Rules text not in the expected form")
        return ident
    if not t:
        bad("Ticker not in the expected form")
        return ident
    g = r.groupdict()
    ident.game = GAMES[g["game"]]
    ident.tournament = g["tour"]
    ident.team_a, ident.team_b, ident.yes_team = g["a"], g["b"], g["sel"]
    ident.kind = "map" if g["n"] else "match"
    ident.map_number = int(g["n"]) if g["n"] else None
    start = datetime.strptime(f"{g['date']} {g['time']}", "%b %d, %Y %I:%M %p").replace(tzinfo=ET)
    ident.scheduled_start = start.timestamp()

    series = t["series"]
    if series in SERIES_GAME and SERIES_GAME[series] != ident.game:
        bad(f"Series {series} is {SERIES_GAME[series]} but the rules say {ident.game}")
    if (series in MAP_SERIES) != (ident.kind == "map"):
        bad("Ticker and rules disagree on match vs map market")
    if (t["n"] and int(t["n"])) != ident.map_number:
        bad(f"Ticker map {t['n']} vs rules map {ident.map_number}")
    if ident.yes_team not in (ident.team_a, ident.team_b):
        bad(f"YES team {ident.yes_team!r} is not one of the two teams")
    if ident.team_a == ident.team_b:
        bad("Both teams have the same name")
    ticker_time = datetime.strptime(f"20{t['yy']}{t['mon']}{t['dd']}{t['hhmm']}", "%Y%b%d%H%M").replace(tzinfo=ET)
    if ticker_time != start:
        bad(f"Ticker time {ticker_time:%b %d %H:%M} ET vs rules time {start:%b %d %H:%M} ET")
    if (g["tz"] == "EST") != (start.utcoffset().total_seconds() == -5 * 3600):
        bad(f"Rules say {g['tz']} but that date is {'EST' if start.utcoffset().total_seconds() == -5 * 3600 else 'EDT'}")
    ident.valid = not ident.problems
    return ident
