"""ProbabilityBot под контрактом оболочки: docs/strategy-contract.md."""

import random
import unittest

from pathlib import Path

from battleships.model import Action, Cell, GameState, OpponentInfo, Rules, valid_action, valid_placement
from battleships.simulator import Board
from battleships.strategies.adaptation import ProfileBook
from battleships.strategies.contract import ProbabilityBot
from battleships.worker import StrategyWorker
from tests.fixtures import started

PATH = "battleships.strategies.contract:ProbabilityBot"


def state_for(modifier=None) -> GameState:
    message = started()
    message["rules"]["modifier"] = modifier
    return GameState.from_message(message)


class ProbabilityBotTest(unittest.TestCase):
    def test_placements_pass_shell_validation(self):
        bot = ProbabilityBot()
        rules = Rules.parse(started()["rules"])
        for _ in range(200):
            self.assertTrue(valid_placement(bot.place_ships(rules), rules))

    def test_every_action_is_valid_until_fleet_is_destroyed(self):
        """Полная партия через GameState оболочки: каждый ход проходит её проверку."""
        for modifier in (None, "SCHRODINGER_SHIP"):
            for seed in range(30):
                bot = ProbabilityBot()
                bot.rng = random.Random(seed)
                state = state_for(modifier)
                state.shots_left = state.rules.shot_limit_per_player
                target = Board([[(c.row, c.column) for c in ship] for ship in bot.place_ships(state.rules)])
                while not target.destroyed:
                    action = bot.choose_action(state, 150)
                    self.assertTrue(valid_action(action, state), f"{modifier}, seed {seed}, {action}")
                    result = target.receive((action.cell.row, action.cell.column))
                    if result == "SUNK" and modifier:
                        result = "HIT"
                    state.shots.append((action.cell, result))
                    state.shots_left -= 1

    def test_shooting_and_placement_follow_opponent(self):
        """Без профилей: HARD — края, «Спидран» — края в стрельбе, остальные — нейтрально, каким бы ни был рейтинг."""
        border = lambda cell: cell.row in (0, 9) or cell.column in (0, 9)
        hard = state_for()
        hard.opponent_info = OpponentInfo(key="builtin:HARD", built_in_bot="HARD")
        speedrun = state_for("SPEEDRUN")
        remote = state_for()
        remote.opponent_info = OpponentInfo(key="bot:x", bot_id="x")
        strong = state_for()
        strong.opponent_info = OpponentInfo(key="bot:y", bot_id="y", rating=1700.0)
        recovered = state_for()
        bot = ProbabilityBot()
        bot.book = ProfileBook(Path("no-such-profiles.json"))
        for _ in range(20):
            self.assertTrue(border(bot.choose_action(hard, 150).cell))
            self.assertTrue(border(bot.choose_action(speedrun, 150).cell))
            self.assertFalse(border(bot.choose_action(strong, 150).cell))
            self.assertFalse(border(bot.choose_action(remote, 150).cell))
            self.assertFalse(border(bot.choose_action(recovered, 150).cell))
        long_on_border = lambda ships: all(border(c) for ship in ships if len(ship) > 1 for c in ship)
        self.assertTrue(all(long_on_border(bot.place_ships_for_match(hard)) for _ in range(20)))
        self.assertFalse(all(long_on_border(bot.place_ships_for_match(remote)) for _ in range(20)))
        for state in (hard, remote, strong, recovered):
            self.assertTrue(valid_placement(bot.place_ships_for_match(state), state.rules))

    def test_defended_cell_is_shot_again(self):
        state = state_for()
        state.shots = [(Cell(4, 4), "DEFENDED")]
        self.assertEqual(ProbabilityBot().choose_action(state, 150), Action.shoot(Cell(4, 4)))

    def test_out_of_board_and_repeated_shots_in_history_are_harmless(self):
        state = state_for()
        state.shots = [(Cell(-1, 50), "MISS"), (Cell(2, 2), "HIT"), (Cell(2, 2), "MISS")]
        action = ProbabilityBot().choose_action(state, 150)
        self.assertTrue(valid_action(action, state))
        self.assertIn(action.cell, {Cell(1, 2), Cell(3, 2), Cell(2, 1), Cell(2, 3)})


class ProbabilityBotWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_runs_in_shell_worker_process(self):
        worker = StrategyWorker(PATH)
        try:
            await worker.start()
            state = state_for()
            ships = await worker.call("place_ships_for_match", (state,), 1.0)
            self.assertTrue(valid_placement(ships, state.rules))
            action = await worker.call("choose_action", (state, 150), 0.15)
            self.assertTrue(valid_action(action, state))
        finally:
            await worker.close()


if __name__ == "__main__":
    unittest.main()
