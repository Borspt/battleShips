"""Запуск: python -m unittest discover"""

import random
import unittest

from battleships.geometry import border_mask
from battleships.placement import edge_placement, random_placement, validate_placement
from battleships.rules import DEFAULT_FLEET, Rules
from battleships.simulator import Board, play_match, play_solo
from battleships.strategies import STRATEGIES, BaselineStrategy, ProbabilityStrategy
from battleships.strategies.probability import HARD_BIAS

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

    def test_edge_placement_keeps_long_ships_on_border_and_singles_inside(self):
        rng = random.Random(9)
        rules = Rules()
        on_border = lambda cell: bool(border_mask(10, 10) >> (cell[0] * 10 + cell[1]) & 1)
        for _ in range(300):
            ships = edge_placement(rules, rng)
            self.assertIsNone(validate_placement(rules, ships))
            for ship in ships:
                self.assertTrue(all(on_border(c) for c in ship) if len(ship) > 1 else not on_border(ship[0]))

    def test_edge_placement_falls_back_when_border_is_too_small(self):
        rules = Rules(rows=5, columns=5, fleet=(3, 2, 1, 1))
        rng = random.Random(2)
        for _ in range(100):
            self.assertIsNone(validate_placement(rules, edge_placement(rules, rng)))

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

    def test_edge_bias_finds_border_fleet_faster(self):
        rules = Rules()
        mean = {}
        for name, bias in (("neutral", None), ("biased", HARD_BIAS)):
            rng = random.Random(21)
            shots = [play_solo(rules, ProbabilityStrategy(rules, random.Random(rng.random()), bias),
                               edge_placement(rules, rng)).shots for _ in range(150)]
            mean[name] = sum(shots) / len(shots)
        self.assertLess(mean["biased"], mean["neutral"] - 5)

    def test_edge_bias_opens_in_a_corner(self):
        shooter = ProbabilityStrategy(Rules(), random.Random(1), HARD_BIAS)
        self.assertIn(shooter.next_shot(), {(0, 0), (0, 9), (9, 0), (9, 9)})

    def test_board_reports_distance_to_nearest_afloat_ship(self):
        board = Board([[(0, 0), (0, 1)], [(5, 5)]])
        self.assertEqual((board.distance((0, 3)), board.distance((4, 4)), board.distance((0, 0))), (2, 2, 0))
        board.receive((5, 5))
        self.assertEqual(board.distance((4, 4)), 7)
        board.receive((0, 0))
        self.assertEqual(board.distance((0, 0)), 0)  # подбитая клетка непотопленного корабля — всё ещё корабль

    def test_endgame_counts_exact_configurations(self):
        """Один однопалубный на поле 3×3: после промаха в центр остаётся 8 равновероятных клеток."""
        shooter = ProbabilityStrategy(Rules(rows=3, columns=3, fleet=(1,)), random.Random(1))
        shooter.on_shot_result((1, 1), "MISS")
        configs = shooter._endgame_configs()
        self.assertEqual(len(configs), 8)
        self.assertEqual(shooter._endgame_weights(configs), [1, 1, 1, 1, 0, 1, 1, 1, 1])

    def test_distance_answer_narrows_configurations(self):
        rules = Rules(rows=5, columns=5, fleet=(1,))
        shooter = ProbabilityStrategy(rules, random.Random(1), distance_uses=1)
        kind, cell = shooter.next_action()
        self.assertEqual(kind, "DISTANCE")
        shooter.on_distance((0, 0), 3)
        self.assertEqual(shooter.distance_uses, 0)
        cells = {divmod(next(iter([i for i in range(25) if mask >> i & 1])), 5) for mask, _, _ in shooter._endgame_configs()}
        self.assertEqual(cells, {(0, 3), (1, 2), (2, 1), (3, 0)})
        self.assertEqual(shooter.next_action()[0], "SHOOT")
        self.assertIn(shooter.next_shot(), cells)

    def test_distance_explained_by_since_sunk_ship_does_not_constrain_the_rest(self):
        rules = Rules(rows=5, columns=5, fleet=(1, 1))
        shooter = ProbabilityStrategy(rules, random.Random(1))
        shooter.on_distance((0, 0), 2)          # ближайший корабль был в двух клетках от угла...
        shooter.on_shot_result((0, 2), "SUNK")  # ...и это он, теперь потоплен
        cells = {divmod(next(i for i in range(25) if mask >> i & 1), 5) for mask, _, _ in shooter._endgame_configs()}
        self.assertIn((4, 4), cells)      # второй корабль может быть дальше двух клеток
        self.assertNotIn((1, 0), cells)   # но ближе двух клеток его не было и нет

    def test_distance_is_not_spent_before_endgame_or_with_unfinished_ship(self):
        shooter = ProbabilityStrategy(Rules(), random.Random(1), distance_uses=2)
        self.assertEqual(shooter.next_action()[0], "SHOOT")
        late = ProbabilityStrategy(Rules(rows=5, columns=5, fleet=(2, 1)), random.Random(1), distance_uses=2)
        late.on_shot_result((2, 2), "HIT")
        self.assertEqual(late.next_action()[0], "SHOOT")

    def test_distance_saves_shots_and_respects_shot_limit(self):
        rules = Rules()
        mean = {}
        for uses in (0, 2):
            rng = random.Random(33)
            games = [play_solo(rules, ProbabilityStrategy(rules, random.Random(rng.random()), distance_uses=uses),
                               random_placement(rules, rng)) for _ in range(150)]
            mean[uses] = sum(g.shots for g in games) / len(games)
            self.assertTrue(all(g.destroyed and g.queries <= uses and g.shots + g.queries <= rules.shot_limit for g in games))
        self.assertLess(mean[2], mean[0] - 3)

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
