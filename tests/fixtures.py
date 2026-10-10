import time
from battleships.strategy import BaselineStrategy
from battleships.model import Action, Cell


class ContextStrategy(BaselineStrategy):
    def place_ships_for_match(self, state):
        assert state.opponent_info.rating == 1642.375
        assert state.opponent_info.bot_id == "3f2b8c1e-6a4d-4f0b-9c5e-2d7a1b8e4c90"
        # A deterministic valid fleet proves the optional hook was actually used.
        return [[Cell(0, c) for c in range(4)], [Cell(2, c) for c in range(3)],
                [Cell(4, c) for c in range(3)], [Cell(6, c) for c in range(2)],
                [Cell(8, c) for c in range(2)], [Cell(0, c) for c in range(6, 8)],
                [Cell(3, 9)], [Cell(5, 9)], [Cell(7, 9)], [Cell(9, 9)]]

    def choose_action(self, state, time_budget_ms):
        assert state.opponent_info.rating == 1642.375
        assert state.opponent_info.key == "bot:3f2b8c1e-6a4d-4f0b-9c5e-2d7a1b8e4c90"
        return Action.shoot(Cell(4, 5))


class SlowStrategy(BaselineStrategy):
    def choose_action(self, state, time_budget_ms):
        time.sleep(5)
        return super().choose_action(state, time_budget_ms)


class CrashingStrategy(BaselineStrategy):
    def choose_action(self, state, time_budget_ms):
        raise RuntimeError("Deliberate test failure")


def rules(modifier=None):
    return {
        "columns": 10, "rows": 10,
        "fleet": [{"length": 4, "count": 1}, {"length": 3, "count": 2},
                  {"length": 2, "count": 3}, {"length": 1, "count": 4}],
        "touchRule": "NO_TOUCHING_EVEN_DIAGONALLY",
        "turnAfterHitRule": "SHOOTER_KEEPS_TURN",
        "matchDurationSeconds": 210, "shotLimitPerPlayer": 100,
        "moveTimeoutSeconds": 1, "placementWindowSeconds": 10,
        "modifier": modifier, "shotsPerTurn": 1,
    }


def started(modifier=None):
    return {"type": "MATCH_STARTED", "rules": rules(modifier), "bonuses": [],
            "opponent": {"kind": "BUILT_IN_BOT", "builtInBot": "EASY"}}
