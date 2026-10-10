"""Запуск: python -m unittest discover"""

import random
import unittest

from battleships.placement import random_placement, validate_placement
from battleships.rules import DEFAULT_FLEET, Rules
from battleships.simulator import Board, play_match, play_solo
from battleships.strategies import STRATEGIES, BaselineStrategy, ProbabilityStrategy

# Пример rules из сообщения MATCH_STARTED в справочнике протокола.
MATCH_STARTED_RULES = {
    "columns": 10, "rows": 10,
    "fleet": [{"length": 4, "count": 1}, {"length": 3, "count": 2}, {"length": 2, "count": 3}, {"length": 1, "count": 4}],
    "touchRule": "NO_TOUCHING_EVEN_DIAGONALLY", "turnAfterHitRule": "SHOOTER_KEEPS_TURN",
    "matchDurationSeconds": 210, "shotLimitPerPlayer": 100, "moveTimeoutSeconds": 1,
    "placementWindowSeconds": 10, "modifier": None, "shotsPerTurn": 1,
}


class RulesTest(unittest.TestCase):
    def test_from_message_matches_defaults(self):
        self.assertEqual(Rules.from_message(MATCH_STARTED_RULES), Rules())

    def test_from_message_reads_changed_rules(self):
        message = dict(MATCH_STARTED_RULES, rows=8, columns=12, shotLimitPerPlayer=40, shotsPerTurn=3,
                       modifier="HUNGER_GAMES", fleet=[{"length": 5, "count": 1}, {"length": 2, "count": 2}],
                       turnAfterHitRule="HIT_SPENDS_SHOT", someNewField={"x": 1})
        rules = Rules.from_message(message)
        self.assertEqual((rules.rows, rules.columns, rules.fleet), (8, 12, (5, 2, 2)))
        self.assertEqual((rules.shot_limit, rules.shots_per_turn, rules.hit_keeps_turn), (40, 3, False))

    def test_from_message_survives_empty_rules(self):
        self.assertEqual(Rules.from_message({}).fleet, DEFAULT_FLEET)


class PlacementTest(unittest.TestCase):
    def test_random_placement_is_always_valid(self):
        rng = random.Random(7)
        for rules in (Rules(), Rules(rows=8, columns=12, fleet=(5, 4, 3, 3, 2)), Rules(no_touching=False)):
            for _ in range(300):
                self.assertIsNone(validate_placement(rules, random_placement(rules, rng)))

    def test_validation_reports_platform_reasons(self):
        rules = Rules(fleet=(2, 1))
        self.assertEqual(validate_placement(rules, [[(0, 0), (0, 1)], [(1, 2)]]), "SHIPS_TOUCH")
        self.assertEqual(validate_placement(rules, [[(0, 0), (0, 1)], [(0, 1)]]), "SHIPS_OVERLAP")
        self.assertEqual(validate_placement(rules, [[(0, 0), (1, 1)], [(5, 5)]]), "SHIP_NOT_STRAIGHT_LINE")
        self.assertEqual(validate_placement(rules, [[(0, 0), (0, 2)], [(5, 5)]]), "SHIP_NOT_STRAIGHT_LINE")
        self.assertEqual(validate_placement(rules, [[(0, 9), (0, 10)], [(5, 5)]]), "SHIP_OUT_OF_BOARD")
        self.assertEqual(validate_placement(rules, [[(0, 0), (0, 1)]]), "FLEET_MISMATCH")
        self.assertIsNone(validate_placement(rules, [[(0, 0), (0, 1)], [(2, 2)]]))


class ShootingTest(unittest.TestCase):
    def test_strategies_never_waste_shots(self):
        """Ни повторных выстрелов, ни выстрелов вне поля; флот тонет в пределах поля."""
        rng = random.Random(3)
        for modifier in (None, "SCHRODINGER_SHIP"):
            rules = Rules.for_modifier(modifier)
            for name, strategy in STRATEGIES.items():
                for _ in range(100):
                    shooter = strategy(rules, random.Random(rng.random()))
                    board = Board(random_placement(rules, rng))
                    seen = set()
                    while not board.destroyed:
                        cell = shooter.next_shot()
                        self.assertNotIn(cell, seen, f"{name}, {modifier}")
                        self.assertTrue(0 <= cell[0] < rules.rows and 0 <= cell[1] < rules.columns)
                        seen.add(cell)
                        result = board.receive(cell)
                        shooter.on_shot_result(cell, "HIT" if result == "SUNK" and modifier else result)

    def test_probability_beats_baseline(self):
        rng = random.Random(5)
        rules = Rules()
        wins = sum(play_match(rules, ProbabilityStrategy, BaselineStrategy, rng).winner == 0 for _ in range(100))
        self.assertGreater(wins, 90)

    def test_probability_shot_count_does_not_regress(self):
        rng = random.Random(11)
        rules = Rules()
        shots = [play_solo(rules, ProbabilityStrategy(rules, random.Random(rng.random())),
                           random_placement(rules, rng)).shots for _ in range(300)]
        self.assertLess(sum(shots) / len(shots), 57)

    def test_defended_cell_is_shot_again(self):
        shooter = ProbabilityStrategy(Rules(), random.Random(1))
        shooter.on_shot_result((4, 4), "DEFENDED")
        self.assertEqual(shooter.next_shot(), (4, 4))

    def test_sunk_ship_blocks_surrounding_cells(self):
        rules = Rules()
        shooter = ProbabilityStrategy(rules, random.Random(1))
        shooter.on_shot_result((0, 0), "HIT")
        shooter.on_shot_result((0, 1), "SUNK")
        self.assertEqual(sorted(shooter.remaining), sorted([4, 3, 3, 2, 2, 1, 1, 1, 1]))
        around = {(0, 2), (1, 0), (1, 1), (1, 2)}
        for _ in range(96 - len(around)):
            cell = shooter.next_shot()
            self.assertNotIn(cell, around)
            shooter.on_shot_result(cell, "MISS")


if __name__ == "__main__":
    unittest.main()
