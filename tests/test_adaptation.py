"""Выбор расстановки и поправки по профилю соперника."""

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from battleships.model import OpponentInfo
from battleships.placement import edge_placement, interior_placement, random_placement, validate_placement
from battleships.strategies.adaptation import MEDIUM_BIAS, ProfileBook, choose_plan
from battleships.strategies.probability import HARD_BIAS

REMOTE = OpponentInfo(key="bot:a", bot_id="a")
BUILT_IN = OpponentInfo(key="builtin:MEDIUM", built_in_bot="MEDIUM")


def profile(**values):
    return {"placements": 30, "matches": 30, "longShipsOnBorder": 0.35, "singlesOnBorderShare": 0.4,
            "openingOnBorder": 0.4} | values


def versus_bots(**values):
    return {"matches": 12, "placements": 12, "longShipsOnBorder": 0.3, "singlesOnBorderShare": 0.4,
            "openingOnBorder": 0.4} | values


class ChoosePlanTest(unittest.TestCase):
    def test_unknown_opponent_gets_neutral_plan(self):
        plan = choose_plan(None, REMOTE, None)
        self.assertEqual((plan.placement, plan.edge_bias), (random_placement, None))
        self.assertEqual(choose_plan(None, OpponentInfo(), None).edge_bias, None)

    def test_opponent_without_habits_gets_neutral_plan(self):
        plan = choose_plan(None, REMOTE, profile())
        self.assertEqual((plan.placement, plan.edge_bias), (random_placement, None))

    def test_bias_grows_with_share_of_ships_on_border(self):
        medium = choose_plan(None, REMOTE, profile(vsBots=versus_bots(longShipsOnBorder=0.7))).edge_bias
        strong = choose_plan(None, REMOTE, profile(vsBots=versus_bots(longShipsOnBorder=1.0))).edge_bias
        self.assertEqual((medium.edge, medium.corner), (MEDIUM_BIAS.edge, MEDIUM_BIAS.corner))
        self.assertEqual((strong.edge, strong.corner), (HARD_BIAS.edge, HARD_BIAS.corner))
        self.assertIsNone(choose_plan(None, REMOTE, profile(vsBots=versus_bots(longShipsOnBorder=0.4))).edge_bias)
        self.assertIsNotNone(choose_plan(None, REMOTE, profile(vsBots=versus_bots(longShipsOnBorder=0.5))).edge_bias)

    def test_singles_weight_follows_where_opponent_keeps_them(self):
        weight = lambda share: choose_plan(None, REMOTE, profile(vsBots=versus_bots(
            longShipsOnBorder=0.7, singlesOnBorderShare=share))).edge_bias.single_edge
        self.assertEqual(weight(0.36), 1.0)      # как при случайной расстановке
        self.assertLess(weight(0.1), 1.0)        # однопалубные внутри поля
        self.assertGreater(weight(0.7), 3.0)     # однопалубные у края
        self.assertEqual(weight(0.99), 4.0)      # вес ограничен

    def test_singles_on_border_are_used_even_when_long_ships_are_placed_anywhere(self):
        plan = choose_plan(None, REMOTE, profile(vsBots=versus_bots(longShipsOnBorder=0.36, singlesOnBorderShare=0.7)))
        self.assertEqual((plan.edge_bias.edge, plan.edge_bias.corner), (1.0, 1.0))
        self.assertGreater(plan.edge_bias.single_edge, 3.0)
        self.assertIn("однопалубных у края", plan.reason)
        for share in (0.47, 0.55):  # случайная расстановка и рядом с ней — без поправки
            self.assertIsNone(choose_plan(None, REMOTE, profile(vsBots=versus_bots(singlesOnBorderShare=share))).edge_bias)

    def test_hunger_games_fleet_hides_from_opponents_first_shots(self):
        import random
        from battleships.rules import Rules
        left = {f"{row},{column}": 0.9 for row in range(10) for column in range(5)}  # бьёт только в левую половину
        recent = {"matches": 25, "placements": 25, "longShipsOnBorder": 0.3, "singlesOnBorderShare": 0.4, "earlyCells": left}
        plan = choose_plan("HUNGER_GAMES", REMOTE, profile(recent=recent))
        self.assertIn("спрятан", plan.reason)
        self.assertEqual(plan.placement.__name__, "evasive_placement")
        rules, rng = Rules.for_modifier("HUNGER_GAMES"), random.Random(1)
        for _ in range(5):
            ships = plan.placement(rules, rng)
            self.assertIsNone(validate_placement(rules, ships))
            self.assertLessEqual(sum(column < 5 for ship in ships for _, column in ship), 9)  # в среднем при случайной — 10
        self.assertEqual(choose_plan("HUNGER_GAMES", REMOTE, None).placement, random_placement)  # данных нет совсем
        typical = choose_plan("HUNGER_GAMES", REMOTE, None, {"4,4": 0.5})
        self.assertIn("ботов в среднем", typical.reason)
        self.assertEqual(choose_plan(None, REMOTE, profile(recent=recent)).placement, random_placement)  # другие режимы

    def test_hunger_games_shooting_follows_fleet_map_of_a_bot_with_favourite_cells(self):
        import random
        from battleships.rules import Rules
        from battleships.strategies.probability import ProbabilityStrategy
        corner = {f"{row},{column}": 0.9 for row in range(3) for column in range(3)}
        recent = {"matches": 25, "placements": 25, "longShipsOnBorder": 0.6, "singlesOnBorderShare": 0.4,
                  "fleetCells": corner, "fleetSpread": 0.2, "fleetLift": 1.6}
        self.assertIsNone(choose_plan("HUNGER_GAMES", REMOTE, profile(recent=recent)).cell_prior)  # выключена по умолчанию
        patcher = mock.patch("battleships.strategies.adaptation.FLEET_MAP_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        plan = choose_plan("HUNGER_GAMES", REMOTE, profile(recent=recent))
        self.assertEqual(len(plan.cell_prior), 100)
        self.assertIsNone(plan.edge_bias)
        self.assertIn("карте его расстановок", plan.reason)
        rules = Rules.for_modifier("HUNGER_GAMES")
        row, column = ProbabilityStrategy(rules, random.Random(1), cell_prior=plan.cell_prior).next_shot()
        self.assertTrue(row < 3 and column < 3)
        # случайная расстановка, мало партий или бот уходит с прежних мест
        for other in (dict(recent, fleetSpread=0.08), dict(recent, placements=10), dict(recent, fleetLift=0.8),
                      dict(recent, fleetLift=None)):
            self.assertIsNone(choose_plan("HUNGER_GAMES", REMOTE, profile(recent=other)).cell_prior)
        self.assertIsNone(choose_plan("DUEL", REMOTE, profile(recent=recent)).cell_prior)

    def test_hunger_games_placement_turns_random_against_a_bot_that_learned_us(self):
        cells = {f"{row},{column}": 0.9 for row in range(10) for column in range(5)}
        recent = {"matches": 25, "placements": 25, "longShipsOnBorder": 0.3, "singlesOnBorderShare": 0.4,
                  "earlyCells": cells, "earlyHits": 6.5}
        with_us = dict(recent, matches=8, placements=8)
        learned = choose_plan("HUNGER_GAMES", REMOTE, profile(recent=recent, recentVsUs=dict(with_us, earlyHits=8.2)))
        self.assertEqual(learned.placement, random_placement)
        self.assertIn("случайная", learned.reason)
        for hits in (6.9, 5.0, None):  # попадает как по всем или реже — продолжаем прятаться
            plan = choose_plan("HUNGER_GAMES", REMOTE, profile(recent=recent, recentVsUs=dict(with_us, earlyHits=hits)))
            self.assertEqual(plan.placement.__name__, "evasive_placement")

    def test_recent_games_override_whole_day_and_games_with_us_override_both(self):
        day = versus_bots(longShipsOnBorder=0.31)
        recent = {"matches": 25, "placements": 25, "longShipsOnBorder": 0.6, "singlesOnBorderShare": 0.5}
        with_us = {"matches": 6, "placements": 6, "longShipsOnBorder": 0.2, "singlesOnBorderShare": 0.3}
        self.assertIsNone(choose_plan(None, REMOTE, profile(vsBots=day)).edge_bias)
        plan = choose_plan(None, REMOTE, profile(vsBots=day, recent=recent))
        self.assertIsNotNone(plan.edge_bias)
        self.assertIn("в последних партиях", plan.reason)
        self.assertIsNone(choose_plan(None, REMOTE, profile(vsBots=day, recent=recent, recentVsUs=with_us)).edge_bias)
        few = dict(with_us, placements=2)
        self.assertIsNotNone(choose_plan(None, REMOTE, profile(vsBots=day, recent=recent, recentVsUs=few)).edge_bias)

    def test_fleet_on_border_seen_only_against_built_in_bots_gives_no_bias(self):
        self.assertIsNone(choose_plan(None, REMOTE, profile(longShipsOnBorder=1.0)).edge_bias)
        self.assertIsNone(choose_plan(None, REMOTE, profile(longShipsOnBorder=1.0, vsBots=versus_bots(placements=2, longShipsOnBorder=1.0))).edge_bias)

    def test_placement_adapts_only_to_built_in_bots(self):
        self.assertEqual(choose_plan(None, BUILT_IN, profile(openingOnBorder=0.05)).placement, edge_placement)
        self.assertEqual(choose_plan(None, BUILT_IN, profile(openingOnBorder=0.95)).placement, interior_placement)
        self.assertEqual(choose_plan(None, BUILT_IN, profile(openingOnBorder=0.6)).placement, random_placement)

    def test_placement_against_team_bots_is_always_random(self):
        """Боты команд видят наши реплеи и подстраиваются: предсказуемая расстановка против них проигрывает."""
        for opening in (0.0, 0.05, 0.5, 0.95, 1.0):
            for versus in (None, versus_bots(openingOnBorder=opening)):
                plan = choose_plan(None, REMOTE, profile(openingOnBorder=opening, vsBots=versus))
                self.assertEqual(plan.placement, random_placement)

    def test_small_samples_are_ignored(self):
        small = versus_bots(placements=3, matches=3, longShipsOnBorder=1.0, openingOnBorder=1.0)
        plan = choose_plan(None, REMOTE, profile(placements=3, matches=3, openingOnBorder=0.0, vsBots=small))
        self.assertEqual((plan.placement, plan.edge_bias), (random_placement, None))

    def test_fallbacks_without_profile(self):
        hard = choose_plan(None, OpponentInfo(key="builtin:HARD", built_in_bot="HARD"), None)
        self.assertEqual((hard.placement, hard.edge_bias), (edge_placement, HARD_BIAS))
        strong = choose_plan(None, OpponentInfo(key="bot:b", bot_id="b", rating=1600.0), None)
        self.assertEqual((strong.placement, strong.edge_bias), (random_placement, None))
        self.assertEqual(choose_plan("SPEEDRUN", REMOTE, None).edge_bias, HARD_BIAS)
        singles = choose_plan("SPEEDRUN", REMOTE, None).single_prior
        self.assertEqual(len(singles), 100)
        self.assertLess(singles[0], singles[11])            # на краю HARD однопалубные не ставит
        self.assertGreater(singles[23], 2 * singles[44])    # (2,3) втрое чаще центра
        self.assertIsNone(choose_plan(None, REMOTE, None).single_prior)
        self.assertEqual(choose_plan("SPEEDRUN", REMOTE, profile()).edge_bias, HARD_BIAS)


class ProfileBookTest(unittest.TestCase):
    def test_reads_profiles_and_survives_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            book = ProfileBook(path)
            self.assertIsNone(book.lookup(REMOTE))
            path.write_text(json.dumps({"bot:a": {"team": "Kraken"}}), encoding="utf-8")
            self.assertEqual(book.lookup(REMOTE)["team"], "Kraken")
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(book.lookup(REMOTE)["team"], "Kraken")
            self.assertIsNone(book.lookup(OpponentInfo()))


if __name__ == "__main__":
    unittest.main()
