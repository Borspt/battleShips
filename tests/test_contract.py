"""ProbabilityBot под контрактом оболочки: docs/strategy-contract.md."""

import random
import unittest

import json
import tempfile
from pathlib import Path
from unittest import mock

from battleships.model import Action, Cell, GameState, OpponentInfo, Rules, valid_action, valid_placement
from battleships.simulator import Board
from battleships.strategies.adaptation import ProfileBook
from battleships.strategies import contract
from battleships.strategies.contract import ProbabilityBonusBot, ProbabilityBot
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

    def test_chosen_plan_is_recorded_once_per_match(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "logs" / "plans.jsonl"
            with mock.patch.object(contract, "PLAN_LOG", log):
                bot = ProbabilityBot()
                bot.book = ProfileBook(Path("no-such-profiles.json"))
                hard = state_for()
                hard.opponent_info = OpponentInfo(key="builtin:HARD", built_in_bot="HARD", rating=1430.5)
                hard.ships = bot.place_ships_for_match(hard)
                bot.choose_action(hard, 150)
                hard.shots = [(Cell(0, 0), "MISS")]
                bot.choose_action(hard, 150)
                speedrun = state_for("SPEEDRUN")
                bot.choose_action(speedrun, 150)
                speedrun.shots = [(Cell(0, 0), "MISS")]
                bot.choose_action(speedrun, 150)
            records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["opponent"], r["placement"], r["modifier"]) for r in records],
                         [("builtin:HARD", "edge_placement", None), ("builtin:EASY", None, "SPEEDRUN")])
        self.assertEqual((records[0]["strategy"], records[0]["opponentRating"], records[0]["edgeBias"]["corner"]),
                         ("ProbabilityBot", 1430.5, 12.0))
        self.assertIn("HARD", records[0]["reason"])

    def test_unwritable_plan_log_does_not_break_the_move(self):
        with tempfile.NamedTemporaryFile() as blocker:
            with mock.patch.object(contract, "PLAN_LOG", Path(blocker.name) / "logs" / "plans.jsonl"):
                state = state_for()
                self.assertTrue(valid_placement(ProbabilityBot().place_ships_for_match(state), state.rules))

    def endgame_state(self, opponent_hits=12):
        """Поле соперника обстреляно так, что остались два однопалубных в двух несвязанных зонах."""
        state = state_for()
        state.ships = ProbabilityBot().place_ships(state.rules)
        own = [cell for ship in state.ships for cell in ship]
        state.opponent_shots = [(cell, "HIT") for cell in own[:opponent_hits]]
        state.bonuses = {"DISTANCE": 3}
        state.shots_left = 60
        sunk = [[(0, 0), (0, 1), (0, 2), (0, 3)], [(2, 0), (2, 1), (2, 2)], [(4, 0), (4, 1), (4, 2)],
                [(6, 0), (6, 1)], [(8, 0), (8, 1)], [(0, 5), (0, 6)], [(0, 8)], [(2, 8)]]
        for ship in sunk:
            state.shots += [(Cell(*cell), "HIT") for cell in ship[:-1]] + [(Cell(*ship[-1]), "SUNK")]
        return state

    def test_bonus_bot_asks_distance_in_endgame_and_uses_the_answer(self):
        state = self.endgame_state()
        bot = ProbabilityBonusBot()
        bot.book = ProfileBook(Path("no-such-profiles.json"))
        action = bot.choose_action(state, 150)
        self.assertEqual((action.type, action.bonus), ("USE_BONUS", "DISTANCE"))
        self.assertTrue(valid_action(action, state, allow_bonuses=True))
        self.assertEqual(ProbabilityBot().choose_action(state, 150).type, "SHOOT")

        state.bonuses["DISTANCE"] = 2
        state.bonus_results.append({"type": "BONUS_RESOLVED", "bonus": "DISTANCE", "distance": 0,
                                    "requestedCell": {"row": 5, "column": 7}})
        state.bonus_results.append({"type": "BONUS_RESOLVED", "bonus": "DISTANCE", "distance": 1,
                                    "requestedCell": {"row": 9, "column": 8}})
        final = bot.choose_action(state, 150)  # два запроса уже потрачены — третий не берём
        self.assertEqual(final.type, "SHOOT")
        self.assertIn(final.cell, {Cell(5, 7), Cell(9, 7), Cell(9, 9), Cell(8, 8)})

    def test_bonus_bot_keeps_rating_when_it_is_safely_ahead_or_out_of_shots(self):
        bot = ProbabilityBonusBot()
        bot.book = ProfileBook(Path("no-such-profiles.json"))
        self.assertEqual(bot.choose_action(self.endgame_state(opponent_hits=4), 150).type, "SHOOT")
        last_shot = self.endgame_state()
        last_shot.shots_left = 1
        self.assertEqual(bot.choose_action(last_shot, 150).type, "SHOOT")
        no_uses = self.endgame_state()
        no_uses.bonuses = {}
        self.assertEqual(bot.choose_action(no_uses, 150).type, "SHOOT")

    def musketeers_state(self):
        state = state_for("THREE_MUSKETEERS")
        state.ships = ProbabilityBot().place_ships(state.rules)
        state.bonuses = {"MINE": 3, "DISTANCE": 3}
        state.shots_left = 100
        state.opponent_info = OpponentInfo(key="bot:a", bot_id="a")
        return state

    def book_with(self, directory, **profiles):
        path = Path(directory) / "profiles.json"
        path.write_text(json.dumps(profiles), encoding="utf-8")
        return ProfileBook(path)

    def far_cells(self, state):
        own = {cell for ship in state.ships for cell in ship}
        return [c for c in state.rules.cells() if all(abs(c.row - o.row) > 1 or abs(c.column - o.column) > 1 for o in own)]

    def test_mines_go_only_where_this_opponent_shoots_in_most_recent_games(self):
        state = self.musketeers_state()
        far = self.far_cells(state)
        sure, likely, rare = far[0], far[len(far) // 2], far[-1]
        key = lambda c: f"{c.row},{c.column}"
        habits = {"matches": 20, "shotCells": {key(sure): 0.95, key(likely): 0.8, key(rare): 0.4}}
        with tempfile.TemporaryDirectory() as directory:
            bot = ProbabilityBonusBot()
            bot.book = self.book_with(directory, **{"bot:a": {"recent": habits}})
            laid = []
            for _ in range(2):
                action = bot.choose_action(state, 150)
                self.assertEqual((action.type, action.bonus), ("USE_BONUS", "MINE"))
                self.assertTrue(valid_action(action, state, allow_bonuses=True))
                laid.append(action.cell)
                state.mines.add(action.cell)
                state.bonuses["MINE"] -= 1
            self.assertEqual(laid, [sure, likely])
            self.assertEqual(bot.choose_action(state, 150).type, "SHOOT")   # клетка с 40% мины не стоит

            state.mines.clear()
            state.opponent_shots = [(sure, "MISS")]                          # туда он уже выстрелил
            self.assertEqual(bot.choose_action(state, 150).cell, likely)

    def test_mines_are_not_spent_without_fresh_data_outside_three_musketeers_or_beyond_the_limit(self):
        state = self.musketeers_state()
        far = self.far_cells(state)
        habits = {"matches": 20, "shotCells": {f"{c.row},{c.column}": 0.9 for c in far}}
        with tempfile.TemporaryDirectory() as directory:
            bot = ProbabilityBonusBot()
            bot.book = self.book_with(directory, **{"bot:a": {"recent": habits}, "bot:b": {"recent": dict(habits, matches=3)}})
            self.assertEqual(bot.choose_action(state, 150).bonus, "MINE")
            for modifier in (None, "DUEL", "HUNGER_GAMES"):
                other = self.musketeers_state()
                other.rules = state_for(modifier).rules
                self.assertEqual(bot.choose_action(other, 150).type, "SHOOT", modifier)
            unknown = self.musketeers_state()
            unknown.opponent_info = OpponentInfo(key="bot:zzz", bot_id="zzz")
            self.assertEqual(bot.choose_action(unknown, 150).type, "SHOOT")
            few_games = self.musketeers_state()
            few_games.opponent_info = OpponentInfo(key="bot:b", bot_id="b")
            self.assertEqual(bot.choose_action(few_games, 150).type, "SHOOT")
            spent = self.musketeers_state()
            spent.opponent_shots = [(Cell(0, 0), "MINE"), (Cell(0, 1), "MINE")]
            spent.mines = {far[0]}
            self.assertNotEqual(bot.choose_action(spent, 150).bonus, "MINE")
            no_uses = self.musketeers_state()
            no_uses.bonuses = {"DISTANCE": 3}
            self.assertEqual(bot.choose_action(no_uses, 150).type, "SHOOT")
            plain = ProbabilityBot()
            plain.book = bot.book
            self.assertEqual(plain.choose_action(self.musketeers_state(), 150).type, "SHOOT")

    def test_no_bonuses_against_much_weaker_opponent(self):
        with tempfile.TemporaryDirectory() as directory:
            far = self.far_cells(self.musketeers_state())
            bot = ProbabilityBonusBot()
            mines = self.musketeers_state()
            habits = {"matches": 20, "shotCells": {f"{c.row},{c.column}": 0.9 for c in self.far_cells(mines)}}
            bot.book = self.book_with(directory, **{"bot:a": {"recent": habits}, "bot:me": {"botName": bot.own_name, "rating": 1600.0}})
            endgame = self.endgame_state()
            for rating, spends in ((1580.0, True), (None, True), (1400.0, False)):
                mines.opponent_info = OpponentInfo(key="bot:a", bot_id="a", rating=rating)
                endgame.opponent_info = OpponentInfo(key="bot:a", bot_id="a", rating=rating)
                self.assertEqual(bot.choose_action(mines, 150).type == "USE_BONUS", spends, rating)
                self.assertEqual(bot.choose_action(endgame, 150).type == "USE_BONUS", spends, rating)

    def test_shots_are_spread_while_opponent_has_mines_we_have_not_stepped_on(self):
        bot = ProbabilityBot()
        bot.book = ProfileBook(Path("no-such-profiles.json"))
        def openings(opponent_bonuses, shots=()):
            state = state_for()
            state.opponent = {"kind": "REMOTE_BOT", "botId": "x", "bonuses": opponent_bonuses}
            state.shots = list(shots)
            return {bot.choose_action(state, 150).cell for _ in range(60)}
        self.assertLessEqual(len(openings([])), 16)
        self.assertLessEqual(len(openings([{"bonus": "SHIELD", "uses": 3}])), 16)
        self.assertGreater(len(openings([{"bonus": "MINE", "uses": 2}])), 20)
        stepped = [(Cell(9, 9), "MINE"), (Cell(9, 0), "MINE")]
        self.assertLessEqual(len(openings([{"bonus": "MINE", "uses": 2}], stepped)), 16)

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
    async def test_bonus_bot_answers_within_shell_budget(self):
        worker = StrategyWorker("battleships.strategies.contract:ProbabilityBonusBot")
        try:
            await worker.start()
            state = ProbabilityBotTest().endgame_state()
            action = await worker.call("choose_action", (state, 150), 0.15)
            self.assertEqual((action.type, action.bonus), ("USE_BONUS", "DISTANCE"))
        finally:
            await worker.close()

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
