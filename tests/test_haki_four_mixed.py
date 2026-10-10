import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from battleships import model
from battleships.rules import Rules
from battleships.strategies.contract import core_rules
from battleships.strategies.haki import HakiEndgameStrategy, LegacyHakiObservationBot
from battleships.strategies.probability import EdgeBias


def fixture():
    return json.loads((Path(__file__).parent / 'haki_four_mixed.json').read_text(encoding='utf-8'))


def shooter(search_ms=1000):
    state = model.GameState.from_message(fixture())
    s = HakiEndgameStrategy(core_rules(state.rules), random.Random(71), search_ms=search_ms, four_mixed=True)
    for cell, result in state.shots:
        s.on_shot_result((cell.row, cell.column), result)
    return s


class FourMixedTest(unittest.TestCase):
    def test_experiment_is_disabled_by_default(self):
        s = shooter()
        s.four_mixed = False
        self.assertIsNone(s._endgame_configs())

    def test_observed_fleet_retained_and_no_duplicate_equal_ship_permutations(self):
        s = shooter()
        self.assertEqual(s.remaining, [2, 1, 1, 1])
        self.assertFalse(s.hits)
        configs = s._endgame_configs()
        actual = sum(1 << (r * 10 + c) for ship in fixture()['actualRemaining'] for r, c in ship)
        self.assertIn(actual, {mask for mask, _, _ in configs})
        self.assertEqual(len(configs), len({chosen for _, _, chosen in configs}))

    def test_priors_and_distance_constraints_preserved(self):
        s = shooter()
        s.edge_bias = EdgeBias(edge=2, corner=3, single_edge=.5)
        s.on_distance((0, 8), 0)
        configs = s._endgame_configs()
        self.assertTrue(configs)
        for mask, weight, chosen in configs:
            self.assertTrue(mask & (1 << 8))
            expected = 1.0
            for length, number in chosen:
                expected *= s._priors(length)[number]
            self.assertAlmostEqual(weight, expected)

    def test_old_reading_explained_by_sunk_ship_keeps_actual_fleet(self):
        s = shooter()
        q = next(i for i in range(100) if s.sunk >> i & 1)
        s.distances.append((q, 0))
        actual = sum(1 << (r * 10 + c) for ship in fixture()['actualRemaining'] for r, c in ship)
        self.assertIn(actual, {m for m, _, _ in s._endgame_configs()})

    def test_node_and_time_limits_discard_partial_search(self):
        s = shooter()
        s.MAX_NODES = 500
        self.assertIsNone(s._endgame_configs())
        s = shooter()
        with patch('battleships.strategies.haki.time.monotonic', side_effect=[0, 0, 2]):
            self.assertIsNone(s._endgame_configs())
        self.assertIsNone(shooter(0)._endgame_configs())

    def test_unknown_hits_and_touching_rules_do_not_enable_four_ship_query(self):
        for touching in (True, False):
            s = HakiEndgameStrategy(Rules(rows=5, columns=5, fleet=(2,1,1,1),
                                         no_touching=touching, modifier='SCHRODINGER_SHIP'),
                                     distance_uses=2, four_mixed=True)
            s.on_shot_result((2,2), 'HIT')
            self.assertIsNone(s._endgame_configs())
            self.assertEqual(s.next_action()[0], 'SHOOT')

    def test_tiny_action_budget_falls_back_legally(self):
        state = model.GameState.from_message(fixture())
        action = LegacyHakiObservationBot().choose_action(state, 40)
        self.assertEqual(action.type, 'SHOOT')
        self.assertTrue(model.valid_action(action, state, allow_bonuses=True))


class FourMixedWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_production_fallback_within_150_ms_worker_budget(self):
        from battleships.worker import StrategyWorker
        worker = StrategyWorker('battleships.strategies.haki:LegacyHakiObservationBot')
        try:
            await worker.start()
            state = model.GameState.from_message(fixture())
            action = await worker.call('choose_action', (state, 150), .15)
            self.assertTrue(model.valid_action(action, state, allow_bonuses=True))
            # The unproven four-mixed experiment is disabled in production.
        finally:
            await worker.close()
