import unittest
from battleships import model
from battleships.strategies.haki import HakiObservationBot
from battleships.strategies.contract import ProbabilityBot
from battleships.worker import StrategyWorker
from tests import test_contract as contract_tests


class HakiDistanceTest(unittest.TestCase):
    def state(self):
        return contract_tests.ProbabilityBotTest().endgame_state()

    def test_available_distance_is_valid_and_shared_bot_does_not_spend(self):
        state = self.state()
        action = HakiObservationBot().choose_action(state, 150)
        self.assertEqual((action.type, action.bonus), ('USE_BONUS', 'DISTANCE'))
        self.assertTrue(model.valid_action(action, state, allow_bonuses=True))
        self.assertEqual(ProbabilityBot().choose_action(state, 150).type, 'SHOOT')

    def test_answer_changes_target_and_two_answers_stop_spending(self):
        state = self.state()
        target = model.Cell(5, 7)
        state.bonus_results = [{'bonus': 'DISTANCE', 'requestedCell': {'row': 5, 'column': 7},
                                'distance': 0}, {'bonus': 'DISTANCE', 'distance': None}]
        self.assertEqual(HakiObservationBot().choose_action(state, 150), model.Action.shoot(target))

    def test_no_bonus_when_unavailable_ahead_low_budget_or_last_shot(self):
        for change in ('none', 'ahead', 'last', 'budget', 'malformed'):
            state = self.state()
            if change == 'none': state.bonuses = {}
            if change == 'ahead': state.opponent_shots = []
            if change == 'last': state.shots_left = 1
            if change == 'malformed':
                state.bonus_results = [{'bonus': 'DISTANCE', 'distance': 'invalid'}] * 2
            action = HakiObservationBot().choose_action(state, 30 if change == 'budget' else 150)
            self.assertEqual(action.type, 'SHOOT', change)

    def test_shield_retry_and_no_inherited_mines(self):
        state = self.state()
        state.shots.append((model.Cell(5, 7), 'DEFENDED'))
        state.bonuses['MINE'] = 3
        self.assertEqual(HakiObservationBot().choose_action(state, 150), model.Action.shoot(model.Cell(5, 7)))


class HakiDistanceWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_distance_through_worker_budget(self):
        worker = StrategyWorker('battleships.strategies.haki:HakiObservationBot')
        try:
            await worker.start()
            action = await worker.call('choose_action', (contract_tests.ProbabilityBotTest().endgame_state(), 150), 0.15)
            self.assertEqual(action.bonus, 'DISTANCE')
        finally:
            await worker.close()
