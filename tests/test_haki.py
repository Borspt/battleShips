import itertools
import random
import unittest
from dataclasses import replace
from unittest import mock

from battleships import model
from battleships.placement import random_placement
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy, HakiObservationBot
from battleships.strategies.probability import EdgeBias, ProbabilityStrategy
from battleships.worker import StrategyWorker
from tests.fixtures import started


class HakiTest(unittest.TestCase):
    def test_joint_search_counts_only_non_touching_fleets(self):
        # На строке из 5 клеток 2 одиночки: (0,2),(0,3),(0,4),(1,3),(1,4),(2,4).
        rules = Rules(rows=1, columns=5, fleet=(1, 1))
        shooter = HakiEndgameStrategy(rules)
        self.assertEqual(shooter._weights(), [3, 2, 2, 2, 3])
        self.assertEqual(ProbabilityStrategy(rules)._weights(), [2, 2, 2, 2, 2])

    def test_joint_weights_match_independent_small_board_oracle(self):
        for remaining in (1, 2, 3):
            for seed in range(8):
                rules = Rules(rows=4, columns=5, fleet=(1,) * remaining)
                rng = random.Random(seed)
                shooter = HakiEndgameStrategy(rules, edge_bias=EdgeBias(single_edge=0.5))
                blocked = rng.sample(range(20), 6)
                shooter.empty = sum(1 << i for i in blocked)
                expected = [0.0] * 20
                for combo in itertools.combinations([i for i in range(20) if i not in blocked], remaining):
                    cells = [divmod(i, 5) for i in combo]
                    if any(max(abs(a[0]-b[0]), abs(a[1]-b[1])) <= 1
                           for a, b in itertools.combinations(cells, 2)):
                        continue
                    weight = 0.5 ** sum(r in (0, 3) or c in (0, 4) for r, c in cells)
                    for i in combo:
                        expected[i] += weight
                self.assertEqual(shooter._weights(), expected)

    def test_limits_discard_partial_results_and_use_baseline(self):
        rules = Rules(rows=4, columns=5, fleet=(1, 1, 1))
        expected = ProbabilityStrategy(rules)._weights()
        for attribute, value in [('search_ms', 0), ('MAX_NODES', 2), ('MAX_COMBINATIONS', 1)]:
            shooter = HakiEndgameStrategy(rules)
            setattr(shooter, attribute, value)
            self.assertEqual(shooter._weights(), expected)
        with mock.patch('battleships.strategies.haki.time.monotonic', side_effect=[0, 1]):
            self.assertEqual(HakiEndgameStrategy(rules)._weights(), expected)

    def test_impossible_evidence_and_other_phases_fall_back(self):
        for rules in (Rules(), Rules(no_touching=False, fleet=(1, 1)),
                      Rules(rows=1, columns=2, fleet=(1, 1))):
            self.assertEqual(HakiEndgameStrategy(rules)._weights(), ProbabilityStrategy(rules)._weights())
        shooter = HakiEndgameStrategy(Rules(fleet=(1,)))
        shooter.on_shot_result((4, 4), 'DEFENDED')
        self.assertEqual(shooter.next_shot(), (4, 4))

    def test_full_games_through_shell_contract(self):
        for modifier in (None, 'DUEL', 'SCHRODINGER_SHIP'):
            for seed in range(8):
                state = model.GameState.from_message(started())
                state.rules = replace(state.rules, modifier=modifier)
                state.ships = []  # placement already accepted; avoid opening-plan log here
                bot = HakiObservationBot()
                bot.rng = random.Random(seed)
                board = Board(random_placement(Rules(), random.Random(seed + 123)))
                seen = set()
                while not board.destroyed:
                    action = bot.choose_action(state, 150)
                    self.assertTrue(model.valid_action(action, state))
                    self.assertNotIn(action.cell, seen)
                    seen.add(action.cell)
                    result = board.receive((action.cell.row, action.cell.column))
                    if modifier == 'SCHRODINGER_SHIP' and result == 'SUNK':
                        result = 'HIT'
                    state.shots.append((action.cell, result))
                    state.shots_left -= 1


class HakiWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_specific_entrypoint_fits_worker_budget(self):
        worker = StrategyWorker('battleships.strategies.haki:HakiObservationBot')
        try:
            await worker.start()
            state = model.GameState.from_message(started())
            state.rules = replace(state.rules, modifier='DUEL', fleet=(1, 1, 1, 1))
            state.ships = []
            # 30 открытых клеток, 27 405 сочетаний — реалистичная тяжёлая концовка.
            state.shots = [(model.Cell(i // 10, i % 10), 'MISS') for i in range(30, 100)]
            action = await worker.call('choose_action', (state, 150), 0.15)
            self.assertTrue(model.valid_action(action, state))
        finally:
            await worker.close()
