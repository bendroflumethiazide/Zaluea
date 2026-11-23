"""E-Sports identity validation, using real Kalshi rules text."""
from atlas.marketdata.esports import identify

CS2 = ("KXCS2GAME-26NOV221925RUSALKAA-RUS",
       "If Rush wins the CCT South America Challengers #4 2026: Rush vs. ALKA CS2 match originally scheduled for "
       "Nov 22, 2026 at 7:25 PM EST, then the market resolves to Yes.")
CS2_MAP = ("KXCS2MAP-26NOV221925RUSALKAA-2-RUS",
           "If Rush wins map 2 in the CCT South America Challengers #4 2026: Rush vs. ALKA CS2 match originally "
           "scheduled for Nov 22, 2026 at 7:25 PM EST, then the market resolves to Yes.")
VAL = ("KXVALORANTGAME-26NOV230400FPXAT-FPX",
       "If FunPlus Phoenix wins the China National Tournament Split Omega: Pro Division 2026: FunPlus Phoenix vs. "
       "A Team Valorant match originally scheduled for Nov 23, 2026 at 4:00 AM EST, then the market resolves to Yes.")
LOL_GAME2 = ("KXLOLMAP-26NOV211100MRRB-2-RB",
             "If Rising Bees wins game 2 in the Nova Series 2026: Mental Rush vs. Rising Bees League of Legends match "
             "originally scheduled for Nov 21, 2026 at 11:00 AM EST, then the market resolves to Yes.")
# Real market from the earlier study: the ticker says 01:00 but the rules say 3:00 AM.
VAL_MISMATCH = ("KXVALORANTMAP-26NOV220100OASMAX-4-OAS",
                "If Oasis Gaming wins map 4 in the Asia Pacific Predator League Philippines 2027: Oasis Gaming vs. "
                "Maxxing Valorant match originally scheduled for Nov 22, 2026 at 3:00 AM EST, then the market "
                "resolves to Yes.")


def test_match_market_identity():
    i = identify(*CS2)
    assert i.valid, i.problems
    assert (i.game, i.kind, i.team_a, i.team_b, i.yes_team) == ("CS2", "match", "Rush", "ALKA", "Rush")
    assert i.tournament == "CCT South America Challengers #4 2026" and i.scheduled_start


def test_map_market_identity():
    i = identify(*CS2_MAP)
    assert i.valid and i.kind == "map" and i.map_number == 2


def test_tournament_names_containing_colons():
    i = identify(*VAL)
    assert i.valid and i.tournament == "China National Tournament Split Omega: Pro Division 2026"
    assert (i.team_a, i.team_b) == ("FunPlus Phoenix", "A Team")


def test_league_game_numbers():
    i = identify(*LOL_GAME2)
    assert i.valid and i.game == "LoL" and i.map_number == 2 and i.yes_team == "Rising Bees"


def test_contradictory_time_invalidates_the_event():
    i = identify(*VAL_MISMATCH)
    assert not i.valid and any("Ticker time" in p for p in i.problems)
    assert i.tradable[0] is False


def test_map_number_mismatch_invalidates():
    t, rules = CS2_MAP
    i = identify(t.replace("-2-", "-3-"), rules)
    assert not i.valid and any("map" in p for p in i.problems)


def test_wrong_game_or_team_invalidates():
    t, rules = CS2
    assert not identify(t.replace("KXCS2GAME", "KXLOLGAME"), rules).valid
    assert not identify(t, rules.replace("If Rush wins", "If Vitality wins")).valid


def test_unparseable_text_is_invalid_not_guessed():
    assert not identify("KXCS2GAME-X", "Something else entirely").valid


def test_even_valid_events_are_not_tradable_without_a_live_feed():
    ok, why = identify(*CS2).tradable
    assert not ok and "live game-state feed" in why
