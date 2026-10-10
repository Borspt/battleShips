"""Профили ботов собираются из реплеев платформы."""

import json
import tempfile
import unittest
from pathlib import Path

from battleships.model import OpponentInfo
from battleships.scouting import plan_results, plans_table, build_profiles, profile_for, save_profiles, load_profiles
from tests.fixtures import rules


def cell(row, column):
    return {"row": row, "column": column}


def match(match_id, winner="A"):
    """Матч двух ботов: A ставит двухпалубный на край и бьёт в угол, B — встроенный HARD."""
    side = lambda seat, kind, **extra: {"seat": seat, "kind": kind, "name": None, "botName": None, "botId": None,
                                        "builtInBot": None, "rating": None} | extra
    rating = {"kFactor": 10.0, "formula": {"ratingAfter": 1510.5}}
    return {
        "matchId": match_id, "status": "FINISHED", "winner": winner, "endReason": "FLEET_DESTROYED",
        "startedAt": f"2026-10-10T10:00:{match_id}Z", "finishedAt": f"2026-10-10T10:01:{match_id}Z",
        "sides": [side("A", "REMOTE_BOT", name="Kraken", botName="Squid", botId="bot-a", rating=rating),
                  side("B", "BUILT_IN_BOT", builtInBot="HARD")],
        "events": [
            {"type": "MATCH_STARTED", "seat": None, "rules": rules()},
            {"type": "PLACEMENT_ACCEPTED", "seat": "A", "ships": [{"cells": [cell(0, 0), cell(0, 1)]}, {"cells": [cell(5, 5)]}]},
            {"type": "PLACEMENT_ACCEPTED", "seat": "B", "ships": [{"cells": [cell(4, 4), cell(4, 5)]}, {"cells": [cell(9, 9)]}]},
            {"type": "SHOT", "seat": "A", "cell": cell(9, 9), "result": "SUNK"},
            {"type": "SHOT", "seat": "A", "cell": cell(4, 4), "result": "HIT"},
            {"type": "SHOT", "seat": "A", "cell": cell(3, 3), "result": "MISS"},
            {"type": "SHOT", "seat": "B", "cell": cell(5, 4), "result": "MISS"},
            {"type": "MATCH_FINISHED", "seat": None},
        ],
    }


class ScoutingTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Path(self.directory.name)
        (self.store / "matches").mkdir()
        for match_id, winner in (("01", "A"), ("02", "B")):
            (self.store / "matches" / f"{match_id}.json").write_text(json.dumps(match(match_id, winner)), encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_profile_collects_results_placement_and_shooting(self):
        profile = build_profiles(self.store)["bot:bot-a"]
        self.assertEqual((profile["matches"], profile["wins"], profile["losses"]), (2, 1, 1))
        self.assertEqual((profile["botName"], profile["team"], profile["rating"]), ("Squid", "Kraken", 1510.5))
        self.assertEqual((profile["placements"], profile["uniquePlacements"]), (2, 1))
        self.assertEqual((profile["longShipsOnBorder"], profile["singlesOnBorderShare"]), (1.0, 0.0))
        self.assertEqual(profile["placementCells"]["0,0"], 2)
        self.assertEqual((profile["shots"], profile["hits"], profile["accuracy"]), (6, 4, 0.667))
        self.assertEqual(profile["openingOnBorder"], 0.333)
        self.assertEqual(profile["firstShots"], {"9,9": 2})
        self.assertEqual((profile["shotsToDestroyMean"], profile["shotsToDestroyGames"]), (3.0, 1))

    def test_built_in_bots_and_head_to_head(self):
        profiles = build_profiles(self.store, own_team="Kraken")
        hard = profiles["builtin:HARD"]
        self.assertEqual((hard["wins"], hard["losses"], hard["longShipsOnBorder"]), (1, 1, 0.0))
        self.assertEqual(hard["vsUs"], {"matches": 2, "wins": 1, "losses": 1, "draws": 0})
        self.assertEqual((hard["vsBots"]["matches"], hard["vsBots"]["openingOnBorder"]), (2, 0.0))
        self.assertEqual((profiles["bot:bot-a"]["vsBots"]["matches"], profiles["bot:bot-a"]["vsBots"]["openingOnBorder"]), (0, None))

    def test_leaderboard_gives_latest_rating_and_unseen_bots(self):
        snapshot = {"fetchedAt": "2026-10-10T11:00:00+00:00", "bots": [
            {"botId": "bot-a", "teamName": "Kraken", "botName": "Squid 2", "rating": 1620.0, "rankingMatchCount": 7},
            {"botId": "bot-z", "teamName": "Newcomers", "botName": "Zed", "rating": 1500.0, "rankingMatchCount": 0}]}
        (self.store / "leaderboard.json").write_text(json.dumps(snapshot), encoding="utf-8")
        profiles = build_profiles(self.store)
        self.assertEqual((profiles["bot:bot-a"]["rating"], profiles["bot:bot-a"]["botName"]), (1620.0, "Squid 2"))
        self.assertEqual((profiles["bot:bot-z"]["matches"], profiles["bot:bot-z"]["rating"]), (0, 1500.0))

    def test_plan_records_are_joined_with_match_outcomes(self):
        log = self.store / "plans.jsonl"
        record = lambda at, opponent: json.dumps({"at": at, "strategy": "ProbabilityBot", "opponent": opponent, "opponentRating": None,
                                                  "modifier": None, "placement": "edge_placement", "edgeBias": None, "reason": "r"})
        log.write_text("\n".join([record("2026-10-10T10:00:01.300000+00:00", "builtin:HARD"),  # матч 01, победа Kraken
                                  record("2026-10-10T10:00:02.300000+00:00", "builtin:HARD"),  # матч 02, поражение
                                  record("2026-10-10T12:00:00+00:00", "builtin:HARD")]),    # матча в реплеях нет
                       encoding="utf-8")
        results = plan_results(self.store, log, team="Kraken")
        self.assertEqual([r["match"] and r["match"]["outcome"] for r in results], ["win", "loss", None])
        self.assertIn("по краям", plans_table(results))

    def test_lookup_by_match_started_opponent(self):
        path = self.store / "shared" / "profiles.json"
        save_profiles(build_profiles(self.store), path)
        profiles = load_profiles(path)
        self.assertEqual(list(profiles), sorted(profiles))
        self.assertEqual(profile_for(profiles, {"kind": "REMOTE_BOT", "botId": "bot-a", "builtInBot": None})["team"], "Kraken")
        self.assertEqual(profile_for(profiles, {"kind": "BUILT_IN_BOT", "botId": None, "builtInBot": "HARD"})["key"], "builtin:HARD")
        self.assertEqual(profile_for(profiles, OpponentInfo(key="bot:bot-a", bot_id="bot-a"))["team"], "Kraken")
        self.assertIsNone(profile_for(profiles, OpponentInfo()))
        self.assertIsNone(profile_for(profiles, {"kind": "REMOTE_BOT", "botId": "unknown", "builtInBot": None}))
        self.assertIsNone(profile_for(profiles, {}))


if __name__ == "__main__":
    unittest.main()
