"""AchievementBot: бонусы на ачивки, все действия проходят проверку оболочки во всех режимах."""

import random
import time
import unittest
from pathlib import Path

from battleships.model import Cell, GameState, OpponentInfo, valid_action
from battleships.simulator import Board
from battleships.strategies.achiever import AchievementBot
from battleships.strategies.adaptation import ProfileBook
from tests.fixtures import started

MODES = (None, "DUEL", "THREE_MUSKETEERS", "SCHRODINGER_SHIP", "BIRD_BOX", "OLD_SCHOOL", "HUNGER_GAMES")


def state_for(modifier, bot):
    message = started()
    message["rules"]["modifier"] = modifier
    if modifier == "THREE_MUSKETEERS":
        message["rules"]["shotsPerTurn"] = 3
    if modifier == "DUEL":
        message["rules"]["turnAfterHitRule"] = "HIT_SPENDS_SHOT"
    state = GameState.from_message(message)
    state.ships = bot.place_ships(state.rules)
    state.bonuses = {"DISTANCE": 3, "SONAR": 1, "SALVO": 1, "SHIELD": 3, "MINE": 3}
    state.shots_left = state.rules.shot_limit_per_player
    state.opponent_info = OpponentInfo(key="bot:x", bot_id="x")
    return state


def new_bot(seed=0):
    bot = AchievementBot()
    bot.rng = random.Random(seed)
    bot.book = ProfileBook(Path("no-such-profiles.json"))
    return bot


def hunter(achievements):
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, {"ACHIEVER_FOR": achievements}):
        return new_bot()


class AchievementBotTest(unittest.TestCase):
    def play_all_modes(self, env=None):
        """Полные партии во всех режимах: (какие бонусы где применены, самый долгий ход)."""
        import os
        from unittest import mock
        used = {}
        slowest = 0.0
        for modifier in MODES:
            for seed in range(12):
                with mock.patch.dict(os.environ, env or {}):
                    bot = new_bot(seed)
                state = state_for(modifier, bot)
                target = Board([[(c.row, c.column) for c in s] for s in bot.place_ships(state.rules)])
                own = Board([[(c.row, c.column) for c in s] for s in state.ships])
                rng = random.Random(seed)
                steps = 0
                while not target.destroyed and state.shots_left > 0 and steps < 300:
                    steps += 1
                    started_at = time.perf_counter()
                    action = bot.choose_action(state, 150)
                    slowest = max(slowest, time.perf_counter() - started_at)
                    self.assertTrue(valid_action(action, state, allow_bonuses=True), f"{modifier} {seed} {action}")
                    if action.type == "USE_BONUS":
                        used[(modifier, action.bonus)] = used.get((modifier, action.bonus), 0) + 1
                        state.bonuses[action.bonus] -= 1
                        result = {"type": "BONUS_RESOLVED", "bonus": action.bonus}
                        if action.bonus == "SHIELD":
                            state.shield_active = True
                        elif action.bonus == "MINE":
                            state.mines.add(action.cell)
                        elif action.bonus == "DISTANCE":
                            result["distance"] = target.distance((action.cell.row, action.cell.column)) or 0
                            result["requestedCell"] = action.cell.wire()
                        state.bonus_results.append(result)
                        if action.bonus != "SALVO":
                            state.shots_left -= 1
                        continue
                    outcome = target.receive((action.cell.row, action.cell.column))
                    if outcome == "SUNK" and modifier == "SCHRODINGER_SHIP":
                        outcome = "HIT"
                    state.shots.append((action.cell, outcome))
                    state.shots_left -= 1
                    # соперник иногда бьёт по нашему флоту — чтобы сработали щит и мины
                    if modifier not in ("BIRD_BOX",) and rng.random() < 0.5:
                        shot_at = {c for c, _ in state.opponent_shots}
                        free = [c for c in state.rules.cells() if c not in shot_at]
                        cell = rng.choice(free)
                        if cell in state.mines:
                            state.mines.discard(cell)
                            state.opponent_shots.append((cell, "MINE"))
                        else:
                            res = own.receive((cell.row, cell.column))
                            if res in ("HIT", "SUNK") and state.shield_active:
                                state.shield_active = False
                                res = "DEFENDED"
                            state.opponent_shots.append((cell, res))
                self.assertTrue(target.destroyed or state.shots_left == 0, f"{modifier} {seed}")
        return used, slowest

    def test_rating_policy_spends_only_paying_bonuses(self):
        used, slowest = self.play_all_modes()
        self.assertLess(slowest, 0.12, f"самый долгий ход {slowest:.3f} с")
        for modifier in ("OLD_SCHOOL", "HUNGER_GAMES"):
            self.assertFalse(any(m == modifier for m, _ in used), used)
        self.assertFalse(any(b in ("SHIELD", "SALVO", "SONAR") for _, b in used), used)
        self.assertEqual({m for m, b in used if b == "MINE"}, {"THREE_MUSKETEERS"}, used)
        self.assertTrue(used.get(("SCHRODINGER_SHIP", "DISTANCE")), used)  # раньше в «Шрёдингере» не спрашивал
        self.assertTrue(used.get((None, "DISTANCE")), used)

    def test_achievement_hunting_on_request(self):
        used, slowest = self.play_all_modes({"ACHIEVER_FOR": "SHIELD,MINE,SALVO,BLITZ"})
        self.assertLess(slowest, 0.12, f"самый долгий ход {slowest:.3f} с")
        for modifier in ("OLD_SCHOOL", "HUNGER_GAMES"):
            self.assertFalse(any(m == modifier for m, _ in used), used)
        self.assertTrue(used.get(("THREE_MUSKETEERS", "MINE")), used)
        self.assertTrue(used.get((None, "MINE")), used)
        self.assertTrue(any(b == "SHIELD" for _, b in used), used)

    def test_no_shield_for_rating(self):
        bot = new_bot()
        state = state_for(None, bot)
        ship = next(s for s in state.ships if len(s) == 4)
        state.opponent_shots = [(c, "HIT") for c in ship[:3]]
        self.assertEqual(bot.choose_action(state, 150).type, "SHOOT")

    def test_shield_when_one_cell_left_on_a_ship(self):
        bot = hunter("SHIELD")
        state = state_for(None, bot)
        state.bonuses["MINE"] = 0
        ship = next(s for s in state.ships if len(s) == 4)
        state.opponent_shots = [(c, "HIT") for c in ship[:3]]
        action = bot.choose_action(state, 150)
        self.assertEqual((action.type, action.bonus), ("USE_BONUS", "SHIELD"))
        state.shield_active = True
        self.assertNotEqual(bot.choose_action(state, 150).bonus, "SHIELD")

    def test_salvo_when_two_ships_left_and_nothing_wounded(self):
        bot = hunter("SALVO")
        state = state_for("THREE_MUSKETEERS", bot)
        state.bonuses["MINE"] = 0
        fleet = [s for s in bot.place_ships(state.rules)]
        # потоплены все корабли, кроме двух однопалубных
        singles = [s for s in fleet if len(s) == 1][:2]
        for ship in fleet:
            if ship in singles:
                continue
            for cell in ship[:-1]:
                state.shots.append((cell, "HIT"))
            state.shots.append((ship[-1], "SUNK"))
        state.shots_left_in_turn = 3
        self.assertNotEqual(bot.choose_action(state, 150).bonus, "SALVO")  # ждём последний выстрел хода
        state.shots_left_in_turn = 1
        action = bot.choose_action(state, 150)
        self.assertEqual((action.type, action.bonus), ("USE_BONUS", "SALVO"))
        self.assertTrue(valid_action(action, state, allow_bonuses=True))

    def test_skip_env_disables_modules(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"ACHIEVER_SKIP": "MINE,SHIELD"}):
            bot = new_bot()
        state = state_for("THREE_MUSKETEERS", bot)
        self.assertEqual(bot.choose_action(state, 150).type, "SHOOT")


if __name__ == "__main__":
    unittest.main()
