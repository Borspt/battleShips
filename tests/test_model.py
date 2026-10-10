import unittest
from battleships.model import Action, Cell, GameState, Rules, valid_action, valid_placement
from battleships.strategy import BaselineStrategy
from tests.fixtures import rules, started


class ModelTests(unittest.TestCase):
    def test_random_fleets_are_legal(self):
        strategy = BaselineStrategy()
        parsed = Rules.parse(rules())
        for _ in range(100):
            self.assertTrue(valid_placement(strategy.place_ships(parsed), parsed))

    def test_placement_rejects_touching_bent_and_duplicate(self):
        data = rules()
        data["fleet"] = [{"length": 3, "count": 1}, {"length": 1, "count": 1}]
        parsed = Rules.parse(data)
        self.assertFalse(valid_placement([[Cell(0, 0), Cell(0, 1), Cell(1, 1)], [Cell(9, 9)]], parsed))
        self.assertFalse(valid_placement([[Cell(0, 0), Cell(0, 1), Cell(0, 2)], [Cell(1, 3)]], parsed))
        self.assertFalse(valid_placement([[Cell(0, 0), Cell(0, 0), Cell(0, 2)], [Cell(9, 9)]], parsed))

    def test_sunk_perimeter_inferred_from_hits(self):
        state = GameState.from_message(started())
        state.shots = [(Cell(2, 2), "HIT"), (Cell(2, 3), "HIT"), (Cell(2, 4), "SUNK")]
        targets = set(state.legal_targets())
        for row in (1, 2, 3):
            for column in range(1, 6):
                self.assertNotIn(Cell(row, column), targets)
        self.assertIn(Cell(2, 6), targets)

    def test_defended_is_retriable_and_priority(self):
        state = GameState.from_message(started())
        state.shots = [(Cell(4, 4), "DEFENDED")]
        self.assertIn(Cell(4, 4), state.legal_targets())
        self.assertEqual(BaselineStrategy().choose_action(state, 10), Action.shoot(Cell(4, 4)))

    def test_schrodinger_does_not_assume_sunk(self):
        state = GameState.from_message(started("SCHRODINGER_SHIP"))
        state.shots = [(Cell(4, 4), "HIT")]
        self.assertIn(Cell(4, 5), state.legal_targets())
        self.assertNotIn(Cell(5, 5), state.legal_targets())

    def test_repeated_miss_does_not_erase_hit(self):
        state = GameState.from_message(started())
        state.shots = [(Cell(4, 4), "HIT"), (Cell(4, 4), "MISS")]
        self.assertEqual(state.observations()[Cell(4, 4)], "HIT")

    def test_mine_and_unknown_are_not_repeated(self):
        state = GameState.from_message(started())
        state.shots = [(Cell(4, 4), "MINE"), (Cell(0, 0), "FUTURE_RESULT")]
        self.assertNotIn(Cell(4, 4), state.legal_targets())
        self.assertNotIn(Cell(0, 0), state.legal_targets())
        self.assertIn(Cell(0, 1), state.legal_targets())

    def test_bad_actions_and_bonus_opt_in(self):
        state = GameState.from_message(started())
        state.bonuses = {"SONAR": 1, "SHIELD": 1}
        for action in (None, {}, Action.shoot(Cell(-1, 0)), Action.shoot(Cell(True, 0)),
                       Action("USE_BONUS", Cell(2, 2), "SONAR")):
            self.assertFalse(valid_action(action, state))
        sonar = Action("USE_BONUS", Cell(2, 2), "SONAR")
        self.assertTrue(valid_action(sonar, state, allow_bonuses=True))
        self.assertFalse(valid_action(Action("USE_BONUS", bonus=[]), state, allow_bonuses=True))
        state.shield_active = True
        self.assertFalse(valid_action(Action("USE_BONUS", bonus="SHIELD"), state, allow_bonuses=True))

    def test_snapshot_and_future_fields(self):
        msg = started("BIRD_BOX")
        msg.update(type="MATCH_SNAPSHOT", shots=[{"cell": {"row": 1, "column": 2}, "result": "HIT"}],
                   shotsLeft=45, ships=None, shieldActive=True, futureField="ignored")
        state = GameState.from_message(msg)
        self.assertEqual(state.shots_left, 45)
        self.assertTrue(state.shield_active)
        self.assertEqual(state.opponent_shots, [])

    def test_parameterized_rectangular_board(self):
        data = rules()
        data.update(rows=5, columns=7, fleet=[{"length": 2, "count": 2}])
        parsed = Rules.parse(data)
        self.assertTrue(valid_placement(BaselineStrategy().place_ships(parsed), parsed))
