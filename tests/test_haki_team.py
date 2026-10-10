import random
import unittest
from unittest.mock import patch

from battleships.model import GameState, Cell, valid_action, valid_placement
from battleships.haki_session import HAKI_STRATEGY
from battleships.strategies.haki import HakiObservationBot
from battleships.strategies.contract import ProbabilityBonusBot
from battleships.worker import StrategyWorker
from tests.fixtures import started


class HakiTeamTests(unittest.TestCase):
    def test_same_team_actions_and_placements_with_same_random_seed(self):
        for modifier in ('HUNGER_GAMES', 'SPEEDRUN', 'SCHRODINGER_SHIP'):
            message = started(modifier)
            message['rules']['moveTimeoutSeconds'] = 3
            if modifier == 'HUNGER_GAMES':
                message['rules']['shotLimitPerPlayer'] = 20
            state = GameState.from_message(message)
            haki, team = HakiObservationBot(), ProbabilityBonusBot()
            haki.rng, team.rng = random.Random(17), random.Random(17)
            with patch.object(haki, '_record'), patch.object(team, '_record'):
                if modifier != 'SPEEDRUN':
                    ours, theirs = haki.place_ships_for_match(state), team.place_ships_for_match(state)
                    self.assertEqual(ours, theirs)
                    self.assertTrue(valid_placement(ours, state.rules))
                for index in range(10):
                    state.shots_left = state.rules.shot_limit_per_player - index
                    ours, theirs = haki.choose_action(state, 150), team.choose_action(state, 150)
                    self.assertEqual(ours, theirs)
                    self.assertTrue(valid_action(ours, state, allow_bonuses=True))
                    state.shots.append((ours.cell, 'MISS'))

    def test_own_rating_identity_and_current_bonus_price(self):
        bot = HakiObservationBot()
        self.assertEqual(bot.own_name, 'Хаки Наблюдения')
        self.assertEqual(bot.distance_price, 2)
        self.assertIs(HakiObservationBot.choose_action, ProbabilityBonusBot.choose_action)
        self.assertIs(HakiObservationBot.plan, ProbabilityBonusBot.plan)


class HakiTeamWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_entrypoint_handles_speedrun_within_budget(self):
        worker = StrategyWorker(HAKI_STRATEGY)
        state = GameState.from_message(started('SPEEDRUN'))
        try:
            await worker.start()
            action = await worker.call('choose_action', (state, 150), .15)
            self.assertTrue(valid_action(action,state,allow_bonuses=True))
        finally:
            await worker.close()
